import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
)

# 能量求值与核对的全局常量(可随配置版本演进,见 ConfigVersion)
MAX_TRACTION_RATE_KWH_PER_MIN = 40.0   # 重启/失连空窗的牵引上限速率,用于不确定区间上界
MAX_AUX_RATE_KWH_PER_MIN = 3.0
LINE_LOSS_RATE = 0.06                  # 线网传输损耗经验比例(相对供电侧受电)
COVERAGE_THRESHOLD = 0.95              # 可闭合核对的最低时间覆盖率
GRID_BUCKET_MINUTES = 15               # 网格对齐路径的固定时间桶
LATE_ARRIVAL_SECONDS = 300             # 采样-到达超过该值视为迟到采样

# ---- 馈线负荷与容量余量情景 ----
SCENARIO_TIME_STEP_SECONDS = 10        # 功率轨迹回放/叠加的固定时间步长
ENVELOPE_MIN_SAMPLES = 3               # 包络路线视为样本充足的最少参考轨迹数
SCARCE_SAMPLE_COUNT = 2                # 参考轨迹数 <= 该值判定为样本稀缺
MC_RUNS_DEFAULT = 40                   # 包络路线联合抽样默认次数
MC_RUNS_MAX = 200
PEAK_WINDOW_RATIO = 0.95               # 峰值区间:净负荷不低于峰值 95% 的连续时间窗
OVERLOAD_EPS_KW = 1.0                  # 超限/受纳拒绝判定的功率死区(kW)
# 轨迹匹配降级时的不确定区间拓宽系数(按匹配层级取最大值,样本稀缺再追加)
WIDEN_LOAD_FALLBACK = 0.10             # 同线路/区段/车型/编组,仅载荷档不同
WIDEN_CONSIST_FALLBACK = 0.15          # 编组不同
WIDEN_SECTION_FALLBACK = 0.25          # 同线路其他区段
WIDEN_NO_REFERENCE = 0.40              # 新车型无任何参考轨迹
WIDEN_SCARCE_EXTRA = 0.15              # 样本稀缺追加拓宽
