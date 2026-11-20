"""馈线情景演示数据:功率轨迹库、供电拓扑版本、运行图版本与验收情景。

覆盖验收样例:发车间隔压缩、多车同步加速、反送受限、跨馈线运行、
车型样本稀缺、拓扑切换,以及历史情景确定性复算与增量重算。
所有时刻为 naive UTC,与主库种子数据口径一致。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import (Feeder, PowerTrace, TimetableTrip, TimetableVersion,
                     TopologyVersion)
from .services.scenario import compute_scenario

STEP = 10
D7 = datetime(2026, 11, 20, 7, 0)  # 规划窗口起点


# ---------------- 功率轨迹形状 ----------------

def _shape(n: int, peak: float, regen_peak: float, jitter: float = 0.0) -> list[float]:
    """合成实测风格的牵引-巡航-惰行-再生制动形状(正牵引/负再生, kW)。"""
    n_ramp = max(2, int(n * 0.20))
    n_plateau = max(2, int(n * 0.40))
    n_brake = max(2, int(n * 0.25))
    p = [0.0] * n
    for i in range(n):
        wave = 1.0 + jitter * math.sin(i * 0.7 + jitter * 5)
        if i < n_ramp:                                   # 加速斜坡
            p[i] = peak * wave * math.sin((i + 1) / n_ramp * math.pi / 2)
        elif i < n_ramp + n_plateau:                     # 高功率牵引平台
            p[i] = peak * wave
        elif i < n - n_brake:                            # 惰行/低牵引
            p[i] = peak * 0.35 * wave
        else:                                            # 再生制动
            j = i - (n - n_brake)
            p[i] = -regen_peak * math.sin((j + 1) / n_brake * math.pi / 2)
    return [round(x, 2) for x in p]


def _energies(p: list[float]) -> tuple[float, float]:
    tr = sum(max(0.0, x) for x in p) * STEP / 3600.0
    rg = sum(max(0.0, -x) for x in p) * STEP / 3600.0
    return round(tr, 3), round(rg, 3)


def _add_traces(db: Session, run_by_key: dict):
    """条件 -> (时长s, 牵引峰值, 再生峰值, 条数)。同条件多条构成包络路线的轨迹池。"""
    spec = [
        # line section model consist load duration peak regen count source
        ("L1", "S1", "M1", "6B", "MEDIUM", 600, 1800, 1000, 4, "RUN-1001"),
        ("L1", "S1", "M1", "6B", "HEAVY", 600, 2100, 900, 2, "RUN-1004"),
        ("L1", "S2", "M1", "6B", "MEDIUM", 600, 1700, 950, 4, "RUN-1001"),
        ("L1", "S2", "M1", "6B", "HEAVY", 600, 2000, 850, 1, "RUN-1004"),
        ("L1", "S2", "M1", "4B", "MEDIUM", 600, 1300, 750, 2, "RUN-1002"),
        ("L2", "S3", "M2", "6A", "MEDIUM", 480, 1400, 1100, 4, "RUN-1003"),
        ("L2", "S3", "M2", "6A", "HEAVY", 480, 1650, 1000, 1, "RUN-1006"),
    ]
    idx = 0
    for line, sec, model, consist, load, dur, peak, regen_peak, count, src_key in spec:
        n = dur // STEP
        for v in range(count):
            idx += 1
            jitter = (v - count / 2) * 0.04
            p = _shape(n, peak * (1 + jitter), regen_peak * (1 + jitter), jitter)
            tr_e, rg_e = _energies(p)
            db.add(PowerTrace(
                code=f"PT-{idx:03d}", line_code=line, section_code=sec,
                vehicle_model=model, consist_code=consist, load_band=load,
                duration_s=dur, traction_energy_kwh=tr_e, regen_energy_kwh=rg_e,
                sample_count=count, source_run_id=run_by_key.get(src_key),
                series={"step_seconds": STEP, "p_kw": p,
                        "traction_kw": [max(0.0, x) for x in p],
                        "regen_kw": [max(0.0, -x) for x in p]}))


# ---------------- 供电拓扑 ----------------

def _add_topologies(db: Session):
    v1 = TopologyVersion(
        code="TOPO-V1", version=1, status="ACTIVE",
        valid_from=datetime(2026, 11, 1),
        note="现状供电拓扑:F-S3 回馈装置检修,反送边界为 0")
    v1.feeders = [
        Feeder(code="F-S1", substation_serial="SUB-S1", capacity_kw=2500,
               max_regen_accept_kw=1500,
               sections=[{"section_code": "S1", "direction": "*"}]),
        Feeder(code="F-S2", substation_serial="SUB-S2", capacity_kw=2600,
               max_regen_accept_kw=1200,
               sections=[{"section_code": "S2", "direction": "*"}]),
        Feeder(code="F-S3", substation_serial="SUB-S3", capacity_kw=2400,
               max_regen_accept_kw=0,
               sections=[{"section_code": "S3", "direction": "*"}]),
    ]
    v2 = TopologyVersion(
        code="TOPO-V2", version=2, status="ACTIVE",
        valid_from=datetime(2026, 11, 25),
        note="修订版:F-S1 容量下调(主变更换窗口),F-S2 容量恢复,F-S3 回馈装置恢复")
    v2.feeders = [
        Feeder(code="F-S1", substation_serial="SUB-S1", capacity_kw=2200,
               max_regen_accept_kw=1500,
               sections=[{"section_code": "S1", "direction": "*"}]),
        Feeder(code="F-S2", substation_serial="SUB-S2", capacity_kw=3000,
               max_regen_accept_kw=1200,
               sections=[{"section_code": "S2", "direction": "*"}]),
        Feeder(code="F-S3", substation_serial="SUB-S3", capacity_kw=2400,
               max_regen_accept_kw=800,
               sections=[{"section_code": "S3", "direction": "*"}]),
    ]
    db.add_all([v1, v2]); db.flush()
    return v1, v2


# ---------------- 运行图 ----------------

def _leg(section: str, direction: str, start: datetime, minutes: float) -> dict:
    return {"section_code": section, "direction": direction,
            "enter_at": start.isoformat(),
            "exit_at": (start + timedelta(minutes=minutes)).isoformat(),
            "stop_seconds": 0}


def _trip(key, train, model, consist, load, legs):
    return TimetableTrip(trip_key=key, train_code=train, vehicle_model=model,
                         consist_code=consist, load_band=load, legs=legs)


def _add_timetables(db: Session):
    # 现状运行图:10 min 发车间隔,同向班次在同一馈线上不重叠
    base = TimetableVersion(code="TT-BASE", version=1, kind="BASELINE",
                            status="ACTIVE", line_code="L1",
                            valid_from=datetime(2026, 11, 15),
                            headway_minutes=10, note="现状运行图(已发布对标基线)")
    for i, h in enumerate([0, 10, 20, 30]):
        t = D7 + timedelta(minutes=h)
        base.trips.append(_trip(
            f"TR-B{i+1}", "T01", "M1", "6B", "MEDIUM",
            [_leg("S1", "UP", t, 10), _leg("S2", "UP", t + timedelta(minutes=10), 10)]))
    # L2 现状班次(F-S3 反送边界 0,再生无法受纳)
    base.trips.append(_trip(
        "TR-B5", "T03", "M2", "6A", "MEDIUM",
        [_leg("S3", "UP", D7 + timedelta(minutes=40), 8)]))

    # 拟议 A:发车间隔压缩到 5 min,同窗始终两班,加速平台叠加冲击馈线峰值
    compress = TimetableVersion(
        code="TT-PROP-COMPRESS", version=1, kind="PROPOSED", status="DRAFT",
        line_code="L1", valid_from=datetime(2026, 11, 26),
        headway_minutes=5, note="拟议:发车间隔 10->5 min,多车同窗运行")
    for i, h in enumerate(range(0, 30, 5)):
        t = D7 + timedelta(minutes=h)
        compress.trips.append(_trip(
            f"TR-P{i+1}", "T01", "M1", "6B", "MEDIUM",
            [_leg("S1", "UP", t, 10), _leg("S2", "UP", t + timedelta(minutes=10), 10)]))

    # 拟议 B:反送受限叠加 —— S2 两班制动同窗(超过 F-S2 受纳边界),
    # S3 两班再生同窗而 F-S3 边界为 0
    regen = TimetableVersion(
        code="TT-PROP-REGEN", version=1, kind="PROPOSED", status="DRAFT",
        line_code="L1", valid_from=datetime(2026, 11, 26),
        headway_minutes=2, note="拟议:制动时刻重叠,检验再生受纳边界")
    regen.trips.append(_trip(
        "TR-Q1", "T01", "M1", "6B", "MEDIUM",
        [_leg("S2", "UP", D7 + timedelta(minutes=30), 10)]))
    regen.trips.append(_trip(
        "TR-Q2", "T02", "M1", "6B", "MEDIUM",
        [_leg("S2", "UP", D7 + timedelta(minutes=31), 10)]))
    regen.trips.append(_trip(
        "TR-R1", "T03", "M2", "6A", "MEDIUM",
        [_leg("S3", "UP", D7 + timedelta(minutes=30), 8)]))
    regen.trips.append(_trip(
        "TR-R2", "T03", "M2", "6A", "HEAVY",
        [_leg("S3", "UP", D7 + timedelta(minutes=31), 8)]))

    # 拟议 C:跨馈线运行 —— 单班在实际切换时点由 F-S1 进入 F-S2(不双计)
    cross = TimetableVersion(
        code="TT-PROP-CROSS", version=1, kind="PROPOSED", status="DRAFT",
        line_code="L1", valid_from=datetime(2026, 11, 26),
        headway_minutes=10, note="拟议:跨供电分区班次,切换时点拆开")
    cross.trips.append(_trip(
        "TR-X1", "T01", "M1", "6B", "MEDIUM",
        [_leg("S1", "UP", D7, 7), _leg("S2", "UP", D7 + timedelta(minutes=7), 7)]))
    cross.trips.append(_trip(
        "TR-X2", "T02", "M1", "4B", "MEDIUM",
        [_leg("S1", "UP", D7 + timedelta(minutes=10), 7),
         _leg("S2", "UP", D7 + timedelta(minutes=17), 7)]))

    # 拟议 D:样本稀缺 —— 新车型 M3 无参考轨迹;HEAVY 组合只有 1~2 条样本
    scarce = TimetableVersion(
        code="TT-PROP-SCARCE", version=1, kind="PROPOSED", status="DRAFT",
        line_code="L1", valid_from=datetime(2026, 11, 26),
        headway_minutes=10, note="拟议:新车型 M3 与小样本载荷组合")
    scarce.trips.append(_trip(
        "TR-N1", "T04", "M3", "6B", "MEDIUM",
        [_leg("S1", "UP", D7, 10), _leg("S2", "UP", D7 + timedelta(minutes=10), 10)]))
    scarce.trips.append(_trip(
        "TR-N2", "T01", "M1", "6B", "HEAVY",
        [_leg("S1", "UP", D7 + timedelta(minutes=5), 10)]))
    db.add_all([base, compress, regen, cross, scarce]); db.flush()
    return {"BASE": base, "COMPRESS": compress, "REGEN": regen,
            "CROSS": cross, "SCARCE": scarce}


# ---------------- 情景 ----------------

def seed_scenarios(db: Session, run_by_key: dict) -> dict:
    _add_traces(db, run_by_key)
    v1, v2 = _add_topologies(db)
    tt = _add_timetables(db)
    db.flush()

    made = {}
    made["BASE_RP"] = compute_scenario(
        db, code="SC-BASE-RP", name="现状运行图馈线负荷(轨迹回放)",
        method="TRACE_REPLAY", topology=v1, timetable=tt["BASE"],
        note="已发布现状对标情景,后续拟议情景不得替换")
    made["BASE_MC"] = compute_scenario(
        db, code="SC-BASE-MC", name="现状运行图馈线负荷(包络抽样)",
        method="ENVELOPE_MC", topology=v1, timetable=tt["BASE"], mc_runs=40,
        note="与轨迹回放同输入的独立路线,非事后检查")
    made["COMPRESS_RP"] = compute_scenario(
        db, code="SC-COMPRESS-RP", name="发车间隔压缩(轨迹回放)",
        method="TRACE_REPLAY", topology=v1, timetable=tt["COMPRESS"],
        baseline_scenario=made["BASE_RP"],
        changed_trip_keys=[tr.trip_key for tr in tt["COMPRESS"].trips],
        note="5min 间隔:多车同步加速推高 F-S1/F-S2 峰值")
    made["COMPRESS_MC"] = compute_scenario(
        db, code="SC-COMPRESS-MC", name="发车间隔压缩(包络抽样)",
        method="ENVELOPE_MC", topology=v1, timetable=tt["COMPRESS"], mc_runs=40,
        baseline_scenario=made["BASE_MC"],
        changed_trip_keys=[tr.trip_key for tr in tt["COMPRESS"].trips])
    made["REGEN_RP"] = compute_scenario(
        db, code="SC-REGEN-RP", name="反送受限与再生无法受纳(轨迹回放)",
        method="TRACE_REPLAY", topology=v1, timetable=tt["REGEN"],
        note="F-S3 边界 0 全拒;F-S2 同窗再生超出 1200kW 受纳边界")
    made["CROSS_RP"] = compute_scenario(
        db, code="SC-CROSS-RP", name="跨馈线运行(轨迹回放)",
        method="TRACE_REPLAY", topology=v1, timetable=tt["CROSS"],
        note="供电分区切换时点拆开,同一时刻不双计")
    made["SCARCE_RP"] = compute_scenario(
        db, code="SC-SCARCE-RP", name="车型样本稀缺(轨迹回放)",
        method="TRACE_REPLAY", topology=v1, timetable=tt["SCARCE"],
        note="M3 无参考轨迹,按车队级形状并扩大预测区间")
    made["TOPO_V2_RP"] = compute_scenario(
        db, code="SC-TOPO-V2-RP", name="压缩运行图 × 拓扑修订 V2(轨迹回放)",
        method="TRACE_REPLAY", topology=v2, timetable=tt["COMPRESS"],
        baseline_scenario=made["COMPRESS_RP"],
        changed_trip_keys=[tr.trip_key for tr in tt["COMPRESS"].trips],
        note="拓扑/容量修订生成新情景版本,与 V1 保留差异")
    return made
