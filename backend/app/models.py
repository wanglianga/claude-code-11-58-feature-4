"""领域模型:采集、运行切分、能量求值、账实核对、指标与复盘。

约定:
- 所有时间戳为 naive UTC(datetime),由写入方保证。
- 能量单位统一为 kWh;累计表读数为表码原值,倍率单独记录,求值时相乘。
- 带不确定范围的能量用 {"v": 估值, "lo": 下界, "hi": 上界} 的 JSON 表示;
  lo/hi 为 None 表示该侧未知(不闭合),禁止用估值冒充闭合计量。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base, engine


# ---------------- 基础档案 ----------------

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password = Column(String(128), nullable=False)  # 演示环境明文,生产应哈希
    role = Column(String(32), nullable=False, default="viewer")  # admin|viewer
    token = Column(String(64), unique=True, nullable=False)


class Line(Base):
    __tablename__ = "lines"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(64), nullable=False)


class Section(Base):
    __tablename__ = "sections"
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    code = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False)
    seq = Column(Integer, nullable=False)           # 区内顺序
    length_km = Column(Float, nullable=False)
    multiplier_zone = Column(String(32), nullable=False, default="Z1")  # 计量倍率区间
    substation_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    line = relationship("Line")


class Train(Base):
    __tablename__ = "trains"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    vehicle_model = Column(String(32), nullable=False)  # 车型


class Consist(Base):
    """编组版本:同一列车换编组后必须切断能量片段。"""
    __tablename__ = "consists"
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    code = Column(String(32), nullable=False)       # 如 6B / 6A
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    train = relationship("Train")


class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True)
    serial = Column(String(64), unique=True, nullable=False)  # 设备序列号
    kind = Column(String(16), nullable=False)       # ONBOARD|SUBSTATION
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=True)
    station_name = Column(String(64), nullable=True)  # 变电所名
    train = relationship("Train")


# ---------------- 采集接入 ----------------

class MeterReading(Base):
    """原始累计值采样。车载表填 traction/regen/aux;变电所表填 import/export。"""
    __tablename__ = "meter_readings"
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    sampled_at = Column(DateTime, nullable=False, index=True)   # 采样时刻(表侧)
    received_at = Column(DateTime, nullable=False)              # 平台到达时刻
    seq = Column(Integer, nullable=False)                       # 设备采样序列号
    traction_cum = Column(Float, nullable=True)
    regen_cum = Column(Float, nullable=True)
    aux_cum = Column(Float, nullable=True)
    import_cum = Column(Float, nullable=True)   # 变电所受电累计
    export_cum = Column(Float, nullable=True)   # 变电所返送(回馈上网)累计
    multiplier = Column(Float, nullable=False, default=1.0)     # 计量倍率
    comm_status = Column(String(16), nullable=False, default="OK")  # OK|DEGRADED|LOST
    device = relationship("Device")
    __table_args__ = (UniqueConstraint("device_id", "seq", name="uq_reading_device_seq"),)


# ---------------- 运行切分 ----------------

class Run(Base):
    """一趟可计量运行。迟到补传会产生 version+1 的新行,旧版本保留供复盘。"""
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    run_key = Column(String(64), nullable=False, index=True)  # 业务键,跨版本稳定
    version = Column(Integer, nullable=False, default=1)
    supersedes_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    driver_mode = Column(String(16), nullable=False, default="AM")  # AM|CM 司机模式
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=False)
    distance_km = Column(Float, nullable=False)
    passengers = Column(Integer, nullable=False, default=0)
    shift = Column(String(32), nullable=False, default="")          # 班次
    status = Column(String(16), nullable=False, default="OPEN")     # OPEN|EVALUATED|PUBLISHED
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    train = relationship("Train")
    line = relationship("Line")


class RunEvent(Base):
    """切分输入:停车/起动/折返/进出区段/换编组等原始记录。"""
    __tablename__ = "run_events"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    ts = Column(DateTime, nullable=False)
    type = Column(String(24), nullable=False)
    # ENTER_SECTION|EXIT_SECTION|STOP|START_MOVE|TURNBACK|CONSIST_CHANGE
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=True)
    direction = Column(String(8), nullable=True)  # UP|DOWN
    payload = Column(JSON, nullable=True)         # 如 {"consist_id": 2}
    run = relationship("Run")


class RunConsist(Base):
    """运行内的编组有效期(换编组切断)。"""
    __tablename__ = "run_consists"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    consist = relationship("Consist")


class RunSegment(Base):
    """可计量运行片段:同车、同编组、同倍率区间,由停车/折返/换编组/换倍率/设备重启切断。"""
    __tablename__ = "run_segments"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    direction = Column(String(8), nullable=False)
    start_ts = Column(DateTime, nullable=False)
    end_ts = Column(DateTime, nullable=False)
    seq = Column(Integer, nullable=False)
    cut_reason = Column(String(24), nullable=False)  # START|STOP|TURNBACK|SECTION_EXIT|CONSIST_CHANGE|MULTIPLIER_CHANGE|DEVICE_RESET|END
    run = relationship("Run")
    section = relationship("Section")


# ---------------- 能量求值 ----------------

class EnergyFragment(Base):
    """片段级能量事实。quantities 示例:
    车载: {"traction": {"v":..,"lo":..,"hi":..}, "regen": {...}, "aux": {...}}
    供电: {"import": {...}, "export": {...}}
    """
    __tablename__ = "energy_fragments"
    id = Column(Integer, primary_key=True)
    run_segment_id = Column(Integer, ForeignKey("run_segments.id"), nullable=False, index=True)
    source = Column(String(8), nullable=False)      # ONBOARD|SUPPLY
    quantities = Column(JSON, nullable=False)
    quality = Column(String(16), nullable=False)    # GOOD|ESTIMATED|UNKNOWN
    coverage = Column(Float, nullable=False)        # 有效计量时间覆盖率 0..1
    detail = Column(JSON, nullable=True)            # resets/gaps/notes 等解释
    segment = relationship("RunSegment")


class GridCell(Base):
    """网格对齐路径:固定 区段×时间桶 双侧能量累计。"""
    __tablename__ = "grid_cells"
    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False, index=True)
    bucket_start = Column(DateTime, nullable=False, index=True)
    bucket_minutes = Column(Integer, nullable=False)
    onboard = Column(JSON, nullable=False)  # {"traction": {...}, "regen": {...}, "aux": {...}}
    supply = Column(JSON, nullable=False)   # {"import": {...}, "export": {...}}
    run_ids = Column(JSON, nullable=False)  # 落入该桶的运行 id 列表
    section = relationship("Section")


# ---------------- 账实核对 ----------------

class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    run_version = Column(Integer, nullable=False)
    method = Column(String(16), nullable=False)     # RUN_ALIGNED|GRID_ALIGNED
    range_start = Column(DateTime, nullable=False)
    range_end = Column(DateTime, nullable=False)
    comparable = Column(String(8), nullable=False)  # FULL|PARTIAL|NONE
    coverage_onboard = Column(Float, nullable=False)
    coverage_supply = Column(Float, nullable=False)
    onboard_net = Column(JSON, nullable=False)      # {"v","lo","hi"} 车载净能耗(牵引+辅助-回馈)
    supply_net = Column(JSON, nullable=False)       # {"v","lo","hi"} 供电净受电(受电-返送)
    diff = Column(JSON, nullable=False)             # supply_net - onboard_net 的区间
    attribution = Column(JSON, nullable=False)      # {line_loss, regen_not_returned, metering_delay, unknown}
    undetermined = Column(JSON, nullable=False)     # 不可判定原因列表
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    run = relationship("Run")


# ---------------- 指标与复盘 ----------------

class Metric(Base):
    """已发布指标。迟到数据只产生新版本指标,旧指标保留原水位与差异说明。"""
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False)      # RUN|SECTION|SHIFT|VEHICLE_MODEL
    scope_key = Column(String(64), nullable=False)  # 如 run_key / section code / 班次 / 车型
    name = Column(String(32), nullable=False)       # KWH_PER_KM|KWH_PER_PASSENGER|REGEN_RATIO|NET_KWH
    value = Column(Float, nullable=True)            # None 表示不可判定
    coverage = Column(Float, nullable=False)
    method = Column(String(16), nullable=False)
    run_version = Column(Integer, nullable=False)
    published_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    supersedes_id = Column(Integer, ForeignKey("metrics.id"), nullable=True)
    diff_note = Column(Text, nullable=True)         # 与上一已发布水位的差异说明
    source_run_ids = Column(JSON, nullable=False)   # 构成该指标的运行版本 id


class ConfigVersion(Base):
    """计量配置版本:倍率、设备映射、回馈上网是否被电网接受等,供复盘追溯。"""
    __tablename__ = "config_versions"
    id = Column(Integer, primary_key=True)
    kind = Column(String(24), nullable=False)       # MULTIPLIER|DEVICE_MAP|REGEN_ACCEPT|LOSS_RATE
    payload = Column(JSON, nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    note = Column(String(255), nullable=True)


# ---------------- 馈线负荷情景:功率轨迹 / 供电拓扑 / 运行图 ----------------

class PowerTrace(Base):
    """实测(或标定)功率轨迹库:相近线路/车型/载荷条件下的牵引与再生功率形状。

    series 为固定步长的归一化序列,长度由 duration_s / step_seconds 决定:
    {"step_seconds": 10, "p_kw": [..正=牵引, 负=再生..],
     "traction_kw": [..], "regen_kw": [..]}
    能量侧来自已核对的运行能量片段(source_run_id/segment_id),保证轨迹可复核。
    """
    __tablename__ = "power_traces"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    line_code = Column(String(32), nullable=False, index=True)
    section_code = Column(String(32), nullable=False, index=True)
    vehicle_model = Column(String(32), nullable=False, index=True)
    consist_code = Column(String(32), nullable=False)
    load_band = Column(String(16), nullable=False)       # LIGHT|MEDIUM|HEAVY
    duration_s = Column(Integer, nullable=False)
    traction_energy_kwh = Column(Float, nullable=False)  # 与主服务能量片段同口径
    regen_energy_kwh = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=False, default=1)  # 该条件下实测样本数
    source_run_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    source_segment_id = Column(Integer, ForeignKey("run_segments.id"), nullable=True)
    series = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TopologyVersion(Base):
    """供电拓扑生效版本。修订拓扑/馈线容量/反送边界生成新版本,旧版本保留。"""
    __tablename__ = "topology_versions"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)   # 如 TOPO-V1
    version = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")  # ACTIVE|SUPERSEDED
    valid_from = Column(DateTime, nullable=False)
    note = Column(String(255), nullable=True)
    feeders = relationship("Feeder", back_populates="topology",
                           cascade="all, delete-orphan")


class Feeder(Base):
    """牵引变电所馈线:容量上限与可接受反送功率边界,及其电气连接的区段列表。"""
    __tablename__ = "feeders"
    id = Column(Integer, primary_key=True)
    topology_version_id = Column(Integer, ForeignKey("topology_versions.id"),
                                 nullable=False, index=True)
    code = Column(String(32), nullable=False)               # 如 F-S1
    substation_serial = Column(String(64), nullable=False)  # 对应 SUBSTATION 设备
    capacity_kw = Column(Float, nullable=False)
    max_regen_accept_kw = Column(Float, nullable=False)     # 可接受反送功率边界(>=0)
    # [{"section_code": "S1", "direction": "UP|DOWN|*"}]
    sections = Column(JSON, nullable=False)
    topology = relationship("TopologyVersion", back_populates="feeders")


class TimetableVersion(Base):
    """运行图生效版本。拟议运行图独立成版,不替换已发布现状对标数据。"""
    __tablename__ = "timetable_versions"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)  # 如 TT-BASE / TT-PROP-A
    version = Column(Integer, nullable=False)
    kind = Column(String(16), nullable=False)               # BASELINE|PROPOSED
    status = Column(String(16), nullable=False, default="DRAFT")  # DRAFT|ACTIVE|SUPERSEDED
    line_code = Column(String(32), nullable=False)
    valid_from = Column(DateTime, nullable=False)
    headway_minutes = Column(Float, nullable=True)          # 发车间隔(规划输入,可空)
    note = Column(String(255), nullable=True)
    trips = relationship("TimetableTrip", back_populates="timetable",
                         cascade="all, delete-orphan")


class TimetableTrip(Base):
    """运行图班次:锁定列车/车型/编组、载荷与区段行程(含供电分区切换时刻)。"""
    __tablename__ = "timetable_trips"
    id = Column(Integer, primary_key=True)
    timetable_version_id = Column(Integer, ForeignKey("timetable_versions.id"),
                                  nullable=False, index=True)
    trip_key = Column(String(64), nullable=False)
    train_code = Column(String(32), nullable=False)
    vehicle_model = Column(String(32), nullable=False)
    consist_code = Column(String(32), nullable=False)
    load_band = Column(String(16), nullable=False)
    # [{"section_code":"S1","direction":"UP","enter_at":iso,"exit_at":iso,
    #   "stop_seconds":0}] —— 跨供电分区在实际切换时点拆开
    legs = Column(JSON, nullable=False)
    timetable = relationship("TimetableVersion", back_populates="trips")


class Scenario(Base):
    """拟议运行图的馈线负荷与容量余量情景。

    情景结果必须引用同一组供电拓扑与运行图版本;method 区分两条独立预测路线:
    TRACE_REPLAY(实测轨迹回放叠加) 与 ENVELOPE_MC(功率包络联合抽样),
    二者对同一拟议运行图独立给出峰值与余量,互不为事后检查。
    """
    __tablename__ = "scenarios"
    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    method = Column(String(16), nullable=False)          # TRACE_REPLAY|ENVELOPE_MC
    topology_version_id = Column(Integer, ForeignKey("topology_versions.id"), nullable=False)
    timetable_version_id = Column(Integer, ForeignKey("timetable_versions.id"), nullable=False)
    baseline_scenario_id = Column(Integer, ForeignKey("scenarios.id"), nullable=True)
    mc_runs = Column(Integer, nullable=False, default=1)
    uncertainty_policy = Column(String(16), nullable=False, default="PLANNING")
    status = Column(String(16), nullable=False, default="COMPUTED")  # COMPUTED|STALE
    range_start = Column(DateTime, nullable=False)
    range_end = Column(DateTime, nullable=False)
    assumptions = Column(JSON, nullable=False)           # 受纳边界/拓宽/样本覆盖等假设
    summary = Column(JSON, nullable=False)               # 全情景汇总(峰值馈线/总超限/总拒纳)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    note = Column(String(255), nullable=True)
    results = relationship("FeederScenarioResult", back_populates="scenario",
                           cascade="all, delete-orphan")


class FeederScenarioResult(Base):
    """单馈线情景结果:峰值区间、容量余量、超限时间窗、再生无法受纳范围与贡献班次。"""
    __tablename__ = "feeder_scenario_results"
    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, ForeignKey("scenarios.id"), nullable=False, index=True)
    feeder_code = Column(String(32), nullable=False)
    substation_serial = Column(String(32), nullable=False)
    capacity_kw = Column(Float, nullable=False)
    max_regen_accept_kw = Column(Float, nullable=False)
    peak = Column(JSON, nullable=False)          # {"v","lo","hi"} kW
    peak_window = Column(JSON, nullable=False)   # [{"start","end","peak_kw"}]
    margin = Column(JSON, nullable=False)        # {"v","lo","hi"} kW 容量余量
    overload_windows = Column(JSON, nullable=False)
    rejected_regen = Column(JSON, nullable=False)  # {"energy_kwh": {"v","lo","hi"}, "windows":[...]}
    trace_coverage = Column(JSON, nullable=False)  # 匹配层级/样本数/拓宽系数/稀缺标记
    contributing_trips = Column(JSON, nullable=False)  # 峰值贡献班次组合
    series = Column(JSON, nullable=True)         # 稀疏化后的净负荷/牵引/再生序列
    scenario = relationship("Scenario", back_populates="results")


def create_all():
    from . import models as _  # noqa
    Base.metadata.create_all(engine)


def drop_all():
    Base.metadata.drop_all(engine)
