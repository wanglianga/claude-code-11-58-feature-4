"""功率轨迹库:实测轨迹选择、时间轴重采样与功率包络拟合。

两条预测路线共用同一套轨迹匹配与不确定度拓宽规则:
- 匹配层级 EXACT > LOAD_FALLBACK > CONSIST_FALLBACK > SECTION_FALLBACK > NO_REFERENCE,
  层级越远、样本越少,功率区间拓宽越多;新车型无参考轨迹时明确给出大区间。
- 轨迹回放(TRACE_REPLAY)取单条最佳实测轨迹按拟议行程时长拉伸,保留实际加速/制动形状;
- 包络抽样(ENVELOPE_MC)在同条件轨迹池上逐时刻拟合包络,再对多个班次联合抽样,
  覆盖未完全重复的运行图。
"""
from __future__ import annotations

from ..config import (ENVELOPE_MIN_SAMPLES, SCARCE_SAMPLE_COUNT,
                      WIDEN_CONSIST_FALLBACK, WIDEN_LOAD_FALLBACK,
                      WIDEN_NO_REFERENCE, WIDEN_SCARCE_EXTRA,
                      WIDEN_SECTION_FALLBACK)
from ..models import PowerTrace

LOAD_RANK = {"LIGHT": 0, "MEDIUM": 1, "HEAVY": 2}

# 匹配层级 -> 默认拓宽系数
LEVEL_WIDEN = {
    "EXACT": 0.0,
    "LOAD_FALLBACK": WIDEN_LOAD_FALLBACK,
    "CONSIST_FALLBACK": WIDEN_CONSIST_FALLBACK,
    "SECTION_FALLBACK": WIDEN_SECTION_FALLBACK,
    "NO_REFERENCE": WIDEN_NO_REFERENCE,
}


def _pick(pool: list[PowerTrace]) -> PowerTrace:
    """样本数多、时长信息量大者优先(同为条件匹配时)。"""
    return sorted(pool, key=lambda t: (t.sample_count, t.duration_s), reverse=True)[0]


def select_trace(db, line_code: str, section_code: str, vehicle_model: str,
                 consist_code: str, load_band: str):
    """按层级返回 (最佳轨迹或 None, 匹配层级, 池内轨迹数, 拓宽系数, 是否样本稀缺)。"""
    base = db.query(PowerTrace).filter(
        PowerTrace.line_code == line_code, PowerTrace.section_code == section_code,
        PowerTrace.vehicle_model == vehicle_model)
    exact = base.filter(PowerTrace.consist_code == consist_code,
                        PowerTrace.load_band == load_band).all()
    if exact:
        return _pick(exact), "EXACT", len(exact), 0.0, len(exact) <= SCARCE_SAMPLE_COUNT

    same_consist = base.filter(PowerTrace.consist_code == consist_code).all()
    if same_consist:
        pool = sorted(same_consist,
                      key=lambda t: abs(LOAD_RANK[t.load_band] - LOAD_RANK[load_band]))
        scarce = len(same_consist) <= SCARCE_SAMPLE_COUNT
        return pool[0], "LOAD_FALLBACK", len(same_consist), \
            WIDEN_LOAD_FALLBACK + (WIDEN_SCARCE_EXTRA if scarce else 0), scarce

    same_section = base.all()
    if same_section:
        scarce = len(same_section) <= SCARCE_SAMPLE_COUNT
        return _pick(same_section), "CONSIST_FALLBACK", len(same_section), \
            WIDEN_CONSIST_FALLBACK + (WIDEN_SCARCE_EXTRA if scarce else 0), scarce

    same_line = (db.query(PowerTrace)
                 .filter(PowerTrace.line_code == line_code,
                         PowerTrace.vehicle_model == vehicle_model).all())
    if same_line:
        scarce = len(same_line) <= SCARCE_SAMPLE_COUNT
        return _pick(same_line), "SECTION_FALLBACK", len(same_line), \
            WIDEN_SECTION_FALLBACK + (WIDEN_SCARCE_EXTRA if scarce else 0), scarce

    # 新车型无任何参考轨迹:退回同线路/区段其他车型的车队级形状,并明确扩大区间
    fleet = (db.query(PowerTrace)
             .filter(PowerTrace.line_code == line_code,
                     PowerTrace.section_code == section_code).all())
    return (_pick(fleet) if fleet else None), "NO_REFERENCE", len(fleet), \
        WIDEN_NO_REFERENCE + (WIDEN_SCARCE_EXTRA if len(fleet) <= SCARCE_SAMPLE_COUNT else 0), \
        True


def select_pool(db, line_code: str, section_code: str, vehicle_model: str,
                consist_code: str, load_band: str):
    """包络路线:返回 (轨迹池, 匹配层级, 拓宽系数, 稀缺标记),池内轨迹同条件优先。"""
    trace, level, pool_size, widen, scarce = select_trace(
        db, line_code, section_code, vehicle_model, consist_code, load_band)
    if trace is None:
        return [], level, widen, scarce
    base = db.query(PowerTrace).filter(
        PowerTrace.line_code == line_code, PowerTrace.section_code == section_code,
        PowerTrace.vehicle_model == vehicle_model)
    if level == "EXACT":
        pool = base.filter(PowerTrace.consist_code == consist_code,
                           PowerTrace.load_band == load_band).all()
    elif level == "LOAD_FALLBACK":
        pool = base.filter(PowerTrace.consist_code == consist_code).all()
    elif level == "CONSIST_FALLBACK":
        pool = base.all()
    elif level == "SECTION_FALLBACK":
        pool = (db.query(PowerTrace)
                .filter(PowerTrace.line_code == line_code,
                        PowerTrace.vehicle_model == vehicle_model).all())
    else:
        pool = (db.query(PowerTrace)
                .filter(PowerTrace.line_code == line_code,
                        PowerTrace.section_code == section_code).all())
    return pool or [trace], level, widen, scarce


def resample(series: dict, target_duration_s: int, step_seconds: int) -> list[float]:
    """把归一化功率序列线性重采样到目标时长(拉伸/压缩保留加速-制动形状)。"""
    src = series["p_kw"]
    src_step = series.get("step_seconds", step_seconds)
    n = max(1, round(target_duration_s / step_seconds))
    if len(src) == 1:
        return [src[0]] * n
    out = []
    last = len(src) - 1
    for i in range(n):
        pos = (i * step_seconds) / src_step
        j = int(pos)
        if j >= last:
            out.append(src[last])
            continue
        frac = pos - j
        out.append(src[j] * (1 - frac) + src[j + 1] * frac)
    return out


def fit_envelope(pool: list[PowerTrace], target_duration_s: int, step_seconds: int):
    """逐时刻拟合功率包络:返回 (均值线, 下界, 上界),长度对齐目标时长。"""
    arrays = [resample(t.series, target_duration_s, step_seconds) for t in pool]
    n = len(arrays[0])
    mean, lo, hi = [], [], []
    for i in range(n):
        vals = [a[i] for a in arrays]
        mean.append(sum(vals) / len(vals))
        lo.append(min(vals))
        hi.append(max(vals))
    return mean, lo, hi


def envelope_sufficient(pool_size: int) -> bool:
    """包络路线样本充足性判定,供情景覆盖说明使用。"""
    return pool_size >= ENVELOPE_MIN_SAMPLES
