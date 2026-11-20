"""馈线负荷与容量余量情景引擎。

输入(均带生效版本,情景结果引用同一组版本):
- 供电拓扑版本 TopologyVersion:馈线容量、可接受反送功率边界、馈线-区段电气连接;
- 运行图版本 TimetableVersion:锁定班次、车型/编组、区段行程与供电分区切换时刻;
- 功率轨迹库 PowerTrace:实测牵引/再生功率形状。

两条独立预测路线(互不为事后检查):
- TRACE_REPLAY:相近条件实测轨迹按拟议时刻回放并叠加,保留实际加速/制动形状;
- ENVELOPE_MC:按区段/车型/停站/载荷拟合功率包络,对多个班次联合抽样,
  输出依赖训练数据代表性的预测区间。

硬规则:
- 跨越供电分区的班次在实际切换时点(leg 边界)拆开,同一时刻只计入一个馈线,不双计;
- 同窗再生功率仅在有电气连接(同一馈线)且未超过受纳边界时抵扣牵引,超出部分记为无法受纳;
- 新车型/稀缺组合按匹配层级扩大预测区间;
- 拓扑、容量或运行图修订生成新情景版本并保留差异;已发布现状对标数据不被替换。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..config import (MC_RUNS_MAX, OVERLOAD_EPS_KW, PEAK_WINDOW_RATIO,
                      SCENARIO_TIME_STEP_SECONDS)
from ..models import (Feeder, FeederScenarioResult, Scenario, Section,
                      TimetableTrip, TopologyVersion)
from .powerlib import envelope_sufficient, fit_envelope, resample, select_pool, select_trace

STEP_S = SCENARIO_TIME_STEP_SECONDS


# ---------------- 数据结构 ----------------

@dataclass
class LegWork:
    trip_key: str
    feeder_code: str
    section_code: str
    direction: str
    start: datetime
    end: datetime
    slot0: int = 0
    n: int = 0
    # 牵引/再生功率的 v/lo/hi 数组(kW,再生为幅值),回放路线直接带拓宽区间
    tr_v: list = field(default_factory=list)
    tr_lo: list = field(default_factory=list)
    tr_hi: list = field(default_factory=list)
    rg_v: list = field(default_factory=list)
    rg_lo: list = field(default_factory=list)
    rg_hi: list = field(default_factory=list)
    level: str = "EXACT"
    pool_size: int = 0
    widen: float = 0.0
    scarce: bool = False


@dataclass
class FeederAgg:
    code: str
    feeder: Feeder
    n: int
    tr_v: list
    tr_lo: list
    tr_hi: list
    rg_v: list
    rg_lo: list
    rg_hi: list
    acc_v: list
    acc_lo: list
    acc_hi: list
    rej_v: list
    rej_lo: list
    rej_hi: list
    net_v: list
    net_lo: list
    net_hi: list
    legs: list           # 落入该馈线的 LegWork
    slot_trips: dict     # slot -> [(trip_key, tr_kw, rg_kw)]


# ---------------- 辅助 ----------------

def _feeder_for(topology: TopologyVersion, section_code: str, direction: str) -> Feeder | None:
    for f in topology.feeders:
        for s in (f.sections or []):
            if s.get("section_code") == section_code and s.get("direction", "*") in ("*", direction):
                return f
    return None


def _widen_bands(v: float, widen: float) -> tuple[float, float, float]:
    return v * (1 - widen), v, v * (1 + widen)


def _signed_to_bands(p: list[float], widen: float):
    """带符号功率(正牵引/负再生) -> 牵引、再生幅值三套区间。"""
    tr_v, tr_lo, tr_hi, rg_v, rg_lo, rg_hi = [], [], [], [], [], []
    for x in p:
        if x >= 0:
            lo, v, hi = _widen_bands(x, widen)
            tr_v.append(v); tr_lo.append(max(0.0, lo)); tr_hi.append(hi)
            rg_v.append(0.0); rg_lo.append(0.0); rg_hi.append(0.0)
        else:
            lo, v, hi = _widen_bands(-x, widen)
            tr_v.append(0.0); tr_lo.append(0.0); tr_hi.append(0.0)
            rg_v.append(v); rg_lo.append(max(0.0, lo)); rg_hi.append(hi)
    return tr_v, tr_lo, tr_hi, rg_v, rg_lo, rg_hi


def _merge_windows(slots: list[int], t0: datetime, value_getter,
                   extra_getter=None) -> list[dict]:
    """把命中槽位合并为连续时间窗。"""
    if not slots:
        return []
    windows, cur = [], [slots[0]]
    for k in slots[1:]:
        if k == cur[-1] + 1:
            cur.append(k)
        else:
            windows.append(cur); cur = [k]
    windows.append(cur)
    out = []
    for grp in windows:
        w = {"start": (t0 + timedelta(seconds=grp[0] * STEP_S)).isoformat(),
             "end": (t0 + timedelta(seconds=(grp[-1] + 1) * STEP_S)).isoformat(),
             "duration_minutes": round(len(grp) * STEP_S / 60.0, 2)}
        w.update(value_getter(grp) or {})
        if extra_getter:
            w.update(extra_getter(grp) or {})
        out.append(w)
    return out


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos); frac = pos - lo
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ---------------- leg 功率构造 ----------------

def _build_replay_leg(db: Session, line_code: str, leg: dict, trip: TimetableTrip,
                      duration_s: int) -> tuple[list, str, int, float, bool]:
    trace, level, pool_size, widen, scarce = select_trace(
        db, line_code, leg["section_code"], trip.vehicle_model,
        trip.consist_code, trip.load_band)
    if trace is None:  # 连车队级形状都没有:零值名义轨迹 + 最大拓宽,由覆盖说明显式标注
        n = max(1, round(duration_s / STEP_S))
        return [0.0] * n, "NO_REFERENCE", 0, widen, True
    p = resample(trace.series, duration_s, STEP_S)
    return p, level, pool_size, widen, scarce


def _build_leg_works(db: Session, topology: TopologyVersion, line_code: str,
                     trips: list[TimetableTrip], t0: datetime, t1: datetime,
                     method: str, mc_runs: int, scenario_code: str,
                     section_line: dict):
    n_total = max(1, round((t1 - t0).total_seconds() / STEP_S))
    works: list[LegWork] = []
    uncovered = []
    for trip in trips:
        for li, leg in enumerate(trip.legs or []):
            feeder = _feeder_for(topology, leg["section_code"], leg.get("direction", "*"))
            if feeder is None:
                uncovered.append({"trip_key": trip.trip_key,
                                  "section_code": leg["section_code"],
                                  "reason": "NO_FEEDER_CONNECTION"})
                continue
            # 跨线运行图:轨迹匹配按行程区段实际所属线路,而非运行图表头线路
            leg_line = section_line.get(leg["section_code"], line_code)
            start, end = leg["enter_at"], leg["exit_at"]
            if isinstance(start, str):
                start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
            duration_s = int((end - start).total_seconds())
            n = max(1, round(duration_s / STEP_S))
            slot0 = round((start - t0).total_seconds() / STEP_S)
            w = LegWork(trip_key=trip.trip_key, feeder_code=feeder.code,
                        section_code=leg["section_code"],
                        direction=leg.get("direction", "*"),
                        start=start, end=end, slot0=slot0, n=n)
            if method == "TRACE_REPLAY":
                p, w.level, w.pool_size, w.widen, w.scarce = _build_replay_leg(
                    db, leg_line, leg, trip, duration_s)
                (w.tr_v, w.tr_lo, w.tr_hi,
                 w.rg_v, w.rg_lo, w.rg_hi) = _signed_to_bands(p, w.widen)
            else:
                pool, level, widen, scarce = select_pool(
                    db, leg_line, leg["section_code"], trip.vehicle_model,
                    trip.consist_code, trip.load_band)
                w.level, w.widen, w.scarce = level, widen, scarce
                w.pool_size = len(pool)
                if not pool:
                    w.tr_v = [0.0] * n; w.tr_lo = w.tr_hi = w.tr_v
                    w.rg_v = [0.0] * n; w.rg_lo = w.rg_hi = w.rg_v
                    w.level = "NO_REFERENCE"
                    continue
                mean, lo_env, hi_env = fit_envelope(pool, duration_s, STEP_S)
                # 每个班次行程一次联合抽样(leg 内形状相关),多次运行覆盖联合不确定性
                tr_v_runs, rg_v_runs = [], []
                for r in range(mc_runs):
                    # 种子只依赖运行图/班次/抽样序号:同输入跨情景编号也可确定性复算
                    rng = random.Random(f"mc|{r}|{trip.trip_key}|{li}")
                    u = rng.uniform(-1, 1)
                    tr_run, rg_run = [], []
                    for m, lo, hi in zip(mean, lo_env, hi_env):
                        samp = hi + (m - hi) * u if u >= 0 else m + (lo - m) * (-u)
                        (tr_run.append(max(0.0, samp)) if samp >= 0
                         else rg_run.append(-samp))
                        if samp >= 0:
                            rg_run.append(0.0)
                        else:
                            tr_run.append(0.0)
                    tr_v_runs.append(tr_run); rg_v_runs.append(rg_run)
                # 逐时刻 5%/95% 预测区间,再叠加匹配层级拓宽
                for i in range(n):
                    tv = sorted(r[i] for r in tr_v_runs)
                    rv = sorted(r[i] for r in rg_v_runs)
                    tv_mid = sum(tv) / len(tv); rv_mid = sum(rv) / len(rv)
                    tlo, thi = _quantile(tv, 0.05), _quantile(tv, 0.95)
                    rlo, rhi = _quantile(rv, 0.05), _quantile(rv, 0.95)
                    w.tr_v.append(tv_mid)
                    w.tr_lo.append(max(0.0, tlo * (1 - widen)))
                    w.tr_hi.append(thi * (1 + widen))
                    w.rg_v.append(rv_mid)
                    w.rg_lo.append(max(0.0, rlo * (1 - widen)))
                    w.rg_hi.append(rhi * (1 + widen))
            works.append(w)
    return works, uncovered, n_total


# ---------------- 馈线叠加 ----------------

def _aggregate(topology: TopologyVersion, works: list[LegWork], n_total: int) -> dict:
    aggs: dict[str, FeederAgg] = {}
    for feeder in topology.feeders:
        z = lambda: [0.0] * n_total
        aggs[feeder.code] = FeederAgg(
            code=feeder.code, feeder=feeder, n=n_total,
            tr_v=z(), tr_lo=z(), tr_hi=z(), rg_v=z(), rg_lo=z(), rg_hi=z(),
            acc_v=z(), acc_lo=z(), acc_hi=z(), rej_v=z(), rej_lo=z(), rej_hi=z(),
            net_v=z(), net_lo=z(), net_hi=z(), legs=[], slot_trips={})
    for w in works:
        agg = aggs[w.feeder_code]
        agg.legs.append(w)
        for i in range(w.n):
            k = w.slot0 + i
            if k >= n_total:
                break
            agg.tr_v[k] += w.tr_v[i]; agg.tr_lo[k] += w.tr_lo[i]; agg.tr_hi[k] += w.tr_hi[i]
            agg.rg_v[k] += w.rg_v[i]; agg.rg_lo[k] += w.rg_lo[i]; agg.rg_hi[k] += w.rg_hi[i]
            agg.slot_trips.setdefault(k, []).append((w.trip_key, w.tr_v[i], w.rg_v[i]))
    for agg in aggs.values():
        cap_r = w_feeder_cap(agg.feeder)
        for k in range(n_total):
            # 同窗再生:仅同一馈线(电气连接)且不超过受纳边界时抵扣
            agg.acc_v[k] = min(agg.rg_v[k], cap_r)
            agg.acc_lo[k] = min(agg.rg_lo[k], cap_r)
            agg.acc_hi[k] = min(agg.rg_hi[k], cap_r)
            agg.rej_v[k] = max(0.0, agg.rg_v[k] - cap_r)
            agg.rej_lo[k] = max(0.0, agg.rg_lo[k] - cap_r)
            agg.rej_hi[k] = max(0.0, agg.rg_hi[k] - cap_r)
            agg.net_v[k] = agg.tr_v[k] - agg.acc_v[k]
            agg.net_lo[k] = agg.tr_lo[k] - agg.acc_hi[k]
            agg.net_hi[k] = agg.tr_hi[k] - agg.acc_lo[k]
    return aggs


def w_feeder_cap(feeder: Feeder) -> float:
    return feeder.max_regen_accept_kw


# ---------------- 结果归并 ----------------

def _contributors(agg: FeederAgg, slot: int, top: int = 5):
    trips = agg.slot_trips.get(slot, [])
    total_tr = sum(t for _, t, _ in trips) or 1.0
    out = [{"trip_key": k, "traction_kw": round(t, 2), "regen_kw": round(r, 2),
            "share": round(t / total_tr, 4)}
           for k, t, r in sorted(trips, key=lambda x: x[1], reverse=True)[:top]]
    return out


def _build_result(agg: FeederAgg, t0: datetime) -> FeederScenarioResult:
    f = agg.feeder
    n = agg.n
    net_v, net_lo, net_hi = agg.net_v, agg.net_lo, agg.net_hi
    # 峰值与峰值区间
    peak_k = max(range(n), key=lambda k: net_v[k])
    peak = {"v": round(net_v[peak_k], 2),
            "lo": round(net_lo[peak_k], 2),
            "hi": round(net_hi[peak_k], 2),
            "at": (t0 + timedelta(seconds=peak_k * STEP_S)).isoformat()}
    threshold = PEAK_WINDOW_RATIO * max(net_v[peak_k], 1e-9)
    peak_slots = [k for k in range(n) if net_v[k] >= threshold]
    peak_window = _merge_windows(
        peak_slots, t0,
        lambda grp: {"peak_kw": round(max(net_v[k] for k in grp), 2)},
        lambda grp: {"trips": sorted({tk for k in grp for tk, _, _ in agg.slot_trips.get(k, [])})})
    # 容量余量(负值即余量不足)
    margin = {"v": round(f.capacity_kw - peak["v"], 2),
              "lo": round(f.capacity_kw - peak["hi"], 2),
              "hi": round(f.capacity_kw - peak["lo"], 2)}
    # 超限时间窗(净负荷超过馈线容量)
    overload_slots = [k for k in range(n) if net_v[k] > f.capacity_kw + OVERLOAD_EPS_KW]
    overload_windows = _merge_windows(
        overload_slots, t0,
        lambda grp: {
            "peak_kw": {"v": round(max(net_v[k] for k in grp), 2),
                        "lo": round(max(net_lo[k] for k in grp), 2),
                        "hi": round(max(net_hi[k] for k in grp), 2)},
            "excess_kw": round(max(net_v[k] for k in grp) - f.capacity_kw, 2)},
        lambda grp: {
            "trips": sorted({tk for k in grp for tk, _, _ in agg.slot_trips.get(k, [])}),
            "contributors": _contributors(agg, max(grp, key=lambda k: net_v[k]))})
    # 再生能量无法受纳的范围
    rej_slots = [k for k in range(n) if agg.rej_v[k] > OVERLOAD_EPS_KW]
    kwh = lambda arr: round(sum(arr[k] for k in rej_slots) * STEP_S / 3600.0, 3)
    rejected_energy = {"v": kwh(agg.rej_v), "lo": kwh(agg.rej_lo), "hi": kwh(agg.rej_hi)}
    rej_windows = _merge_windows(
        rej_slots, t0,
        lambda grp: {
            "peak_rejected_kw": round(max(agg.rej_v[k] for k in grp), 2),
            "energy_kwh": round(sum(agg.rej_v[k] for k in grp) * STEP_S / 3600.0, 3)},
        lambda grp: {
            "trips": sorted({tk for k in grp for tk, _, _ in agg.slot_trips.get(k, [])}),
            "contributors": _contributors(
                agg, max(grp, key=lambda k: agg.rej_v[k]))})
    # 轨迹覆盖说明:取最差匹配层级
    level_order = {"EXACT": 0, "LOAD_FALLBACK": 1, "CONSIST_FALLBACK": 2,
                   "SECTION_FALLBACK": 3, "NO_REFERENCE": 4}
    worst = max(agg.legs, key=lambda w: level_order[w.level], default=None)
    trace_coverage = {
        "worst_level": worst.level if worst else "NO_REFERENCE",
        "scarce": any(w.scarce for w in agg.legs),
        "sufficient": envelope_sufficient(min((w.pool_size for w in agg.legs), default=0)),
        "matched": [{"trip_key": w.trip_key, "section_code": w.section_code,
                     "level": w.level, "pool_size": w.pool_size,
                     "widen": w.widen, "scarce": w.scarce,
                     "start": w.start.isoformat(), "end": w.end.isoformat()}
                    for w in agg.legs],
    }
    contributing = _contributors(agg, peak_k)
    overload_set, rej_set = set(overload_slots), set(rej_slots)
    # 紧凑稠密序列(供前端绘图与增量复算逐槽比对)
    series = {
        "t0": t0.isoformat(), "step_seconds": STEP_S,
        "net_v": [round(x, 2) for x in net_v],
        "net_lo": [round(x, 2) for x in net_lo],
        "net_hi": [round(x, 2) for x in net_hi],
        "traction_v": [round(x, 2) for x in agg.tr_v],
        "regen_v": [round(x, 2) for x in agg.rg_v],
        "accepted_v": [round(x, 2) for x in agg.acc_v],
        "rejected_v": [round(x, 2) for x in agg.rej_v],
        "state": [("OVERLOAD" if k in overload_set
                   else "REJECT" if k in rej_set else "OK") for k in range(n)],
    }
    return FeederScenarioResult(
        feeder_code=agg.code, substation_serial=f.substation_serial,
        capacity_kw=f.capacity_kw, max_regen_accept_kw=f.max_regen_accept_kw,
        peak=peak, peak_window=peak_window, margin=margin,
        overload_windows=overload_windows,
        rejected_regen={"energy_kwh": rejected_energy, "windows": rej_windows,
                        "accept_boundary_kw": f.max_regen_accept_kw},
        trace_coverage=trace_coverage, contributing_trips=contributing, series=series)


# ---------------- 增量复算校验 ----------------

def _recomputed_windows(works: list[LegWork], changed: set[str] | None) -> dict:
    """受班次改动影响的馈线供电窗口(只重算这些窗口,其余自基线沿用)。"""
    out: dict[str, list[dict]] = {}
    if not changed:
        return out
    for w in works:
        if w.trip_key in changed:
            out.setdefault(w.feeder_code, []).append(
                {"start": w.start.isoformat(), "end": w.end.isoformat(),
                 "trip_key": w.trip_key})
    return out


def _verify_carried_windows(base_scenario: Scenario | None, results: list[FeederScenarioResult],
                            recomputed: dict, t0: datetime):
    """校验增量重算:未受影响供电窗口的净负荷与基线逐槽一致。

    - 基线与候选共享班次身份(发车时刻调整)时为 INCREMENTAL:
      未改动班次的行程窗口必须逐槽复现基线,改动班次窗口登记为重算;
    - 运行图整体替换(班次身份不同)时为 FULL_RECOMPUTE,不做逐槽约束;
    - 无基线时为 FULL。
    """
    if base_scenario is None:
        return {"mode": "FULL", "carried_slots": 0, "recomputed_slots": 0, "mismatches": []}
    base_by_code = {r.feeder_code: r for r in base_scenario.results}
    carried = recomputed_slots = 0
    mismatches = []
    identity_shared = False
    for r in results:
        base = base_by_code.get(r.feeder_code)
        if not base or not base.series or base.series.get("step_seconds") != STEP_S:
            continue

        def _ranges(matched):
            out = {}
            for m in matched:
                out.setdefault(m["trip_key"], []).append(
                    (datetime.fromisoformat(m["start"]),
                     datetime.fromisoformat(m["end"])))
            return out

        cand_legs = _ranges(r.trace_coverage["matched"])
        base_legs = _ranges(base.trace_coverage["matched"])
        shared = set(cand_legs) & set(base_legs)
        if shared:
            identity_shared = True
        changed_trips = {w["trip_key"] for w in recomputed.get(r.feeder_code, [])}
        blocked = [(a, b) for w in recomputed.get(r.feeder_code, [])
                   for a, b in [(datetime.fromisoformat(w["start"]),
                                 datetime.fromisoformat(w["end"]))]]
        # 沿用窗口:共享且未改动班次的行程
        carried_ranges = [(a, b) for k in shared if k not in changed_trips
                          for a, b in cand_legs[k]]
        bv, b_t0 = base.series["net_v"], datetime.fromisoformat(base.series["t0"])
        for k, v in enumerate(r.series["net_v"]):
            ts = t0 + timedelta(seconds=k * STEP_S)
            if any(a <= ts < b for a, b in blocked):
                recomputed_slots += 1
                continue
            if not any(a <= ts < b for a, b in carried_ranges):
                continue  # 非共享班次窗口属于全量重算范围
            bk = round((ts - b_t0).total_seconds() / STEP_S)
            if 0 <= bk < len(bv) and abs(bv[bk] - v) > 0.01:
                mismatches.append({"feeder": r.feeder_code, "at": ts.isoformat(),
                                   "baseline": bv[bk], "recomputed": v})
            else:
                carried += 1
    return {"mode": "INCREMENTAL" if identity_shared else "FULL_RECOMPUTE",
            "carried_slots": carried, "recomputed_slots": recomputed_slots,
            "mismatches": mismatches}


# ---------------- 主入口 ----------------

def compute_scenario(db: Session, *, code: str, name: str, method: str,
                     topology: TopologyVersion, timetable, mc_runs: int = 1,
                     baseline_scenario: Scenario | None = None,
                     changed_trip_keys: list[str] | None = None,
                     note: str | None = None) -> Scenario:
    if method not in ("TRACE_REPLAY", "ENVELOPE_MC"):
        raise ValueError("method 必须为 TRACE_REPLAY 或 ENVELOPE_MC")
    mc_runs = max(1, min(mc_runs, MC_RUNS_MAX)) if method == "ENVELOPE_MC" else 1
    trips = (db.query(TimetableTrip)
             .filter(TimetableTrip.timetable_version_id == timetable.id).all())
    if not trips:
        raise ValueError("运行图无班次,无法生成情景")
    t0 = min(datetime.fromisoformat(leg["enter_at"]) if isinstance(leg["enter_at"], str)
             else leg["enter_at"] for tr in trips for leg in tr.legs)
    t1 = max(datetime.fromisoformat(leg["exit_at"]) if isinstance(leg["exit_at"], str)
             else leg["exit_at"] for tr in trips for leg in tr.legs)

    works, uncovered, n_total = _build_leg_works(
        db, topology, timetable.line_code, trips, t0, t1, method, mc_runs, code,
        section_line={s.code: s.line.code for s in db.query(Section).all()})
    aggs = _aggregate(topology, works, n_total)
    results = [_build_result(agg, t0) for agg in aggs.values()]

    recomputed = _recomputed_windows(works, set(changed_trip_keys or []))
    carry = _verify_carried_windows(baseline_scenario, results, recomputed, t0)
    if carry["mismatches"]:
        raise RuntimeError(f"增量复算校验失败:未受影响窗口出现差异 {carry['mismatches'][:3]}")

    # 全情景汇总
    peak_result = max(results, key=lambda r: r.peak["v"]) if results else None
    summary = {
        "method": method,
        "scenario_count": mc_runs,
        "peak_feeder": peak_result.feeder_code if peak_result else None,
        "peak_kw": peak_result.peak if peak_result else None,
        "feeders_over_capacity": [r.feeder_code for r in results
                                  if r.margin["v"] < -OVERLOAD_EPS_KW],
        "feeders_with_rejected_regen": [r.feeder_code for r in results
                                        if r.rejected_regen["energy_kwh"]["v"] > 0],
        "total_overload_minutes": round(
            sum(w["duration_minutes"] for r in results for w in r.overload_windows), 2),
        "total_rejected_regen_kwh": {
            "v": round(sum(r.rejected_regen["energy_kwh"]["v"] for r in results), 3),
            "lo": round(sum(r.rejected_regen["energy_kwh"]["lo"] for r in results), 3),
            "hi": round(sum(r.rejected_regen["energy_kwh"]["hi"] for r in results), 3)},
        "scarce_feeders": [r.feeder_code for r in results
                           if r.trace_coverage["scarce"]
                           or r.trace_coverage["worst_level"] == "NO_REFERENCE"],
    }
    assumptions = {
        "step_seconds": STEP_S,
        "mc_runs": mc_runs,
        "topology_code": topology.code,
        "topology_version": topology.version,
        "timetable_code": timetable.code,
        "timetable_version": timetable.version,
        "timetable_kind": timetable.kind,
        "baseline_scenario_id": baseline_scenario.id if baseline_scenario else None,
        "changed_trip_keys": changed_trip_keys or [],
        "recomputed_windows": recomputed,
        "incremental": carry,
        "uncovered_legs": uncovered,
        "peak_window_ratio": PEAK_WINDOW_RATIO,
    }
    scenario = Scenario(
        code=code, name=name, method=method,
        topology_version_id=topology.id, timetable_version_id=timetable.id,
        baseline_scenario_id=baseline_scenario.id if baseline_scenario else None,
        mc_runs=mc_runs, range_start=t0, range_end=t1,
        assumptions=assumptions, summary=summary, note=note)
    for r in results:
        scenario.results.append(r)
    db.add(scenario)
    db.flush()
    return scenario


def diff_scenarios(base: Scenario, cand: Scenario) -> dict:
    """两个情景版本的差异(拓扑/容量/运行图修订后保留差异)。"""
    b = {r.feeder_code: r for r in base.results}
    c = {r.feeder_code: r for r in cand.results}
    feeders = sorted(set(b) | set(c))
    rows = []
    for code in feeders:
        rb, rc = b.get(code), c.get(code)
        if rb is None or rc is None:
            rows.append({"feeder_code": code, "status": "ADDED" if rc else "REMOVED"})
            continue
        rows.append({
            "feeder_code": code,
            "status": "CHANGED",
            "peak_kw_base": rb.peak["v"], "peak_kw_candidate": rc.peak["v"],
            "peak_delta_kw": round(rc.peak["v"] - rb.peak["v"], 2),
            "margin_base": rb.margin["v"], "margin_candidate": rc.margin["v"],
            "margin_delta_kw": round(rc.margin["v"] - rb.margin["v"], 2),
            "overload_minutes_base": round(
                sum(w["duration_minutes"] for w in rb.overload_windows), 2),
            "overload_minutes_candidate": round(
                sum(w["duration_minutes"] for w in rc.overload_windows), 2),
            "rejected_regen_base_kwh": rb.rejected_regen["energy_kwh"]["v"],
            "rejected_regen_candidate_kwh": rc.rejected_regen["energy_kwh"]["v"],
            "rejected_delta_kwh": round(
                rc.rejected_regen["energy_kwh"]["v"] - rb.rejected_regen["energy_kwh"]["v"], 3),
        })
    return {
        "base": {"scenario_code": base.code, "method": base.method,
                 "topology_code": base.assumptions.get("topology_code"),
                 "timetable_code": base.assumptions.get("timetable_code")},
        "candidate": {"scenario_code": cand.code, "method": cand.method,
                      "topology_code": cand.assumptions.get("topology_code"),
                      "timetable_code": cand.assumptions.get("timetable_code")},
        "feeders": rows,
    }
