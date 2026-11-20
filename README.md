# 轨道交通牵引能耗运行事实与对标平台

判断一趟列车的**牵引取电、制动回馈、辅助负荷**与**站场供电表**是否描述了同一段可计量运行,
形成可用于线路对标、节能评估和事故复盘的能耗事实;并在此基础上,对**拟议运行图**生成
各牵引变电所馈线的**负荷峰值、容量余量、超限时间窗与再生无法受纳范围**,
支撑发车间隔调整与车辆编组替换的规划决策。

技术栈:Vue 3 + Element Plus(前端)/ FastAPI + SQLAlchemy(后端)/ PostgreSQL(存储)。

## 核心规则

| 环节 | 规则 |
| --- | --- |
| 采集接入 | 保存原始累计值、采样时刻、设备序列号、通信状态、计量倍率;迟到采样按到达时间识别 |
| 运行切分 | 按列车、编组、线路、区段、停车与折返记录切成可计量片段;**换编组、进入不同倍率区间、设备重启/读数倒退必须切断** |
| 能量求值 | 累计值重启或倒退**不直接相减**,只累加单调段,空窗按上限速率扩展不确定区间上界;供电表缺失或覆盖不足时输出**未知范围**,不以车载值冒充闭合计量 |
| 账实核对 | 计算列车侧与供电侧可比较范围、差异去向(线损/回馈未上网/计量延迟/不可知)与不可判定原因 |
| 双路径 | 路径A 运行对齐:逐片段对齐两侧时间边界再汇总;路径B 网格对齐:区段×15min 固定网格累计双侧能量、运行映射进网格,跨格按时间比例分摊为区间。两者对同一运行范围独立给出可比较能耗与差异边界 |
| 指标服务 | 按运行/区段/班次/车型发布单位里程、单位载客能耗与回馈比例,带来源覆盖率 |
| 迟到补传 | 只生成新运行版本;**已发布指标保留原水位**,新指标带差异说明,复盘可回查完整链路 |
| 馈线情景 | 供电拓扑与运行图均有生效版本,情景结果**引用同一组版本**;两条独立路线(实测轨迹回放/功率包络联合抽样)对同一拟议运行图给出馈线峰值与余量;跨供电分区班次在切换时点拆开不双计;同窗再生仅在电气连接且未超受纳边界时抵扣;新车型样本稀缺时扩大预测区间;拓扑/容量/运行图修订生成新情景版本并保留差异 |

## 一键启动(Docker Compose)

```bash
docker compose up --build
```

- 前端:http://localhost:8080
- 后端 API:http://localhost:8000(Swagger 文档 `/docs`,FastAPI 自带)
- 首次启动自动建库并注入五类验收场景与馈线情景演示数据;`SEED_ON_START=0` 可关闭重建

## 本地开发

前置:PostgreSQL 运行于 127.0.0.1:5432(库/用户/密码均为 `ccdev`)。

```bash
# 后端(端口 8000)
cd backend
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
.venv/bin/python -m app.seed          # 建表 + 注入验收场景数据
.venv/bin/uvicorn app.main:app --port 8000

# 前端(端口 5173,代理 /api -> 8000)
cd frontend
npm install
npm run dev                           # 打开 http://localhost:5173
```

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| admin | admin123 | 管理(可采集写入) |
| viewer | viewer123 | 只读 |

## 验证方式

### 自动化测试(已实际执行,29 项全部通过)

```bash
cd backend
.venv/bin/python -m pytest tests/ -q    # 使用独立测试库 ccdev_test
```

覆盖:计数器重启/倒退不相减、倍率切断、未知范围不冒充 0、五类场景的端到端结论、
迟到补传触发新版本且旧指标保留、双路径同范围结果一致、复盘链路完整;
以及馈线情景的发车间隔压缩、多车同步加速、反送受限、跨馈线不双计、样本稀缺展宽、
拓扑切换差异、双路线独立估计、历史情景确定性复算与增量重算(共 11 项)。

### 五类验收场景(种子数据自带,可在页面直接核对)

| 运行 | 场景 | 页面验证点 |
| --- | --- | --- |
| RUN-1001 | 跨区段折返 | 运行详情:折返/停车切断、方向 UP→DOWN 翻转;核对 FULL,差异=线损+不可知 |
| RUN-1002 | 设备重启+换编组+倍率切换 | 片段切断原因含 DEVICE_RESET/CONSIST_CHANGE/MULTIPLIER_CHANGE;重启段质量 ESTIMATED、上界>下界 |
| RUN-1003 | 回馈受限 | 核对归因"回馈未上网">0,假设中 regen_accepted=false |
| RUN-1004 | 车载与供电表差异 | 差异 10.4 kWh 分解为线损/回馈未上网/不可知,归因之和=差异 |
| RUN-1005 | 补传读数 | v1 指标为"未知"(覆盖 37.5%),v2 闭合;指标页 diff_note 含"迟到补传";复盘页见历史水位 |
| RUN-1006 | 供电表缺失 | 核对 NONE,供电净受电"未知",不可判定原因 SUPPLY_COVERAGE_INSUFFICIENT |

页面路径:总览 → 运行与能量(点运行进详情,可切换 路径A/路径B)→ 指标服务 → 复盘回查。

## 馈线负荷与容量余量情景

线路规划人员调整发车间隔或替换车辆编组前,先估计新运行图下各牵引变电所的负荷峰值与馈线余量。
把总电量按 15 分钟均分无法表达多车同时加速、再生制动恰逢受电限制的叠加,本能力基于主服务
已核对的运行能量片段,在 10 秒级时间轴上叠加功率轨迹。

### 两条独立预测路线(互不为事后检查)

| 路线 | 做法 | 适用与代价 |
| --- | --- | --- |
| 实测轨迹回放 `TRACE_REPLAY` | 相近线路/车型/载荷的实测功率轨迹按拟议时刻回放并拉伸叠加,保留真实加速/制动形状 | 可复核性强;少样本车型/载荷组合覆盖不足 |
| 功率包络联合抽样 `ENVELOPE_MC` | 按区段/车型/停站/载荷拟合逐时刻功率包络,对多个班次联合抽样(默认 40 次),输出 5%/95% 预测区间 | 覆盖未重复运行图;需模型校准,区间依赖训练数据代表性 |

轨迹匹配层级 `EXACT → LOAD_FALLBACK → CONSIST_FALLBACK → SECTION_FALLBACK → NO_REFERENCE`,
层级越远、样本越少,功率区间拓宽越大(最高 +40% 并追加稀缺拓宽);新车型无参考轨迹时明确标注
`NO_REFERENCE` 并扩大区间,不给出虚假精确点估计。

### 关键规则

- 情景锁定同一组**供电拓扑版本**(`TOPO-V1/V2`,馈线容量、可接受反送功率边界、电气连接区段)
  与**运行图版本**(`TT-BASE` 现状基线 / `TT-PROP-*` 拟议);拟议运行图独立成版,不替换已发布现状对标数据。
- 跨供电分区班次在**实际切换时点(leg 边界)拆开**,同一时刻只计入一个馈线,不双计。
- 同窗再生只在**同一馈线(电气连接)且未超过受纳边界**时抵扣牵引,超出部分计入"再生无法受纳"
  能量区间与时间窗;受纳边界为 0 时该馈线再生全部拒纳。
- 输出:各馈线峰值区间(点估计/lo/hi)、峰值时间窗(≥95% 峰值)、容量余量、超限时间窗、
  再生拒纳范围、峰值贡献班次组合、轨迹匹配覆盖说明与逐槽负荷序列。
- 改动发车时刻派生新运行图后可做**增量重算**:仅重算受影响班次的供电窗口,未受影响窗口与基线
  逐槽校验一致(不一致直接报错);拓扑/容量修订经 `topologies/derive` 生成新版本并保留逐馈线差异。

### 种子自带的馈线验收情景

| 情景 | 验收点 |
| --- | --- |
| `SC-BASE-RP` / `SC-BASE-MC` | 现状运行图两条独立路线;作为不可被替换的对标基线 |
| `SC-COMPRESS-RP` / `SC-COMPRESS-MC` | 发车间隔 10→5min:F-S1/F-S2 峰值抬升、出现超限窗,峰值贡献为同窗两班组合 |
| `SC-REGEN-RP` | 反送受限:F-S3 边界 0 再生全部拒纳;F-S2 同窗再生超 1200kW 边界部分拒纳,受纳+拒纳=总再生 |
| `SC-CROSS-RP` | 跨馈线:TR-X1 在切换时点拆给 F-S1/F-S2,时间窗不重叠、不双计 |
| `SC-SCARCE-RP` | 新车型 M3 无参考轨迹 → NO_REFERENCE,峰值区间显著展宽 |
| `SC-TOPO-V2-RP` | 拓扑修订(容量 3000→2200 等)生成新情景版本,与 V1 逐馈线差异保留 |

页面路径:**馈线负荷情景**(列表/新建/版本派生)→ 点情景进详情(负荷曲线、峰值余量、
超限与拒纳窗口、贡献班次、匹配层级、版本差异、增量复算说明)。

### 手工 API 验证示例

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 双路径核对结论对比(同一运行范围)
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=RUN_ALIGNED"  -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=GRID_ALIGNED" -H "Authorization: Bearer $TOKEN"

# 补传一条迟到读数 -> 自动生成新运行版本并重发指标
curl -s -X POST http://localhost:8000/api/readings -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '[{"device_serial":"OB-T01","sampled_at":"2026-10-21T14:05:00",
        "received_at":"2026-10-21T18:00:00","seq":99001,
        "traction_cum":0,"regen_cum":0,"aux_cum":0,"multiplier":1.0}]'

# 馈线情景:查看拓扑/运行图版本与功率轨迹库
curl -s http://localhost:8000/api/topologies  -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/timetables  -H "Authorization: Bearer $TOKEN"
curl -s http://localhost:8000/api/power-traces -H "Authorization: Bearer $TOKEN"

# 派生拟议运行图(仅平移指定班次发车时刻,基线版本保留)
curl -s -X POST http://localhost:8000/api/timetables/derive \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"TT-PLAN-1","source_timetable_code":"TT-BASE",
       "headway_minutes":8,"trip_offsets_minutes":{"TR-B1":3.0}}'

# 派生供电拓扑(修订馈线容量/反送边界,旧拓扑标记 SUPERSEDED)
curl -s -X POST http://localhost:8000/api/topologies/derive \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"TOPO-V3","source_topology_code":"TOPO-V1",
       "feeder_overrides":{"F-S1":{"capacity_kw":2200,"max_regen_accept_kw":1500}}}'

# 对拟议运行图生成情景(两条路线二选一;增量重算时带基线与改动班次)
curl -s -X POST http://localhost:8000/api/scenarios -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"压缩间隔-轨迹回放","method":"TRACE_REPLAY",
       "topology_code":"TOPO-V1","timetable_code":"TT-PROP-COMPRESS",
       "baseline_scenario_code":"SC-BASE-RP",
       "changed_trip_keys":["TR-P1","TR-P2"]}'
curl -s -X POST http://localhost:8000/api/scenarios -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"压缩间隔-包络抽样","method":"ENVELOPE_MC","mc_runs":40,
       "topology_code":"TOPO-V1","timetable_code":"TT-PROP-COMPRESS"}'

# 情景详情(峰值/余量/超限窗/拒纳/贡献班次)与逐馈线版本差异
curl -s http://localhost:8000/api/scenarios -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/scenarios/<id>/diff?against=<baseline_id>" \
  -H "Authorization: Bearer $TOKEN"
```

## 目录结构

```
backend/app/
  models.py              # 领域模型:读数/运行/片段/能量/网格/核对/指标/配置版本
                         #   +功率轨迹/供电拓扑/运行图/馈线情景
  services/energy.py     # 计数器求差:重启倒退、倍率切断、不确定区间
  services/segmentation.py  # 运行切分(停车/折返/换编组/倍率/重启)
  services/evaluate.py   # 片段求值(路径A) + 网格重建(路径B)
  services/reconcile.py  # 账实核对:可比范围、差异归因、不可判定
  services/metrics.py    # 指标发布与迟到版本化
  services/powerlib.py   # 功率轨迹匹配/重采样/包络拟合/区间拓宽
  services/scenario.py   # 馈线情景引擎:双路线叠加、受纳边界、增量复算、版本差异
  scenario_seed.py       # 馈线情景演示数据(轨迹库/拓扑/运行图/八情景)
  api/routes.py          # REST API(登录鉴权)
  seed.py                # 五类验收场景 + 馈线情景演示数据
backend/tests/           # pytest:单元 + 场景端到端 + 馈线情景(29 项)
frontend/src/views/      # 总览/采集/运行/指标/复盘/馈线情景 页面
docker-compose.yml       # 一键启动(db + backend + frontend)
```

## 已执行的验证结论

- `pytest`:29 项全部通过(测试库 ccdev_test,与开发库隔离;含馈线情景 11 项)。
- 本地开发模式全链路冒烟:后端 uvicorn 下,登录、运行核对、指标、复盘接口,以及
  拓扑/运行图/轨迹查询、运行图与拓扑版本派生、两类情景生成、情景详情与版本差异、
  未登录 401 均返回预期结果。
- 前端 `vite build` 构建通过;馈线负荷情景列表/详情(含 SVG 负荷曲线、超限/拒纳窗口、
  贡献班次、匹配层级、版本差异)页面已接入路由与菜单。
- 本交付容器内无 docker 命令,Docker 镜像未实际构建;Compose 与 Dockerfile 沿用标准
  多阶段构建且入口会自动建库注入演示数据,本地开发验证均使用原生运行时完成。
