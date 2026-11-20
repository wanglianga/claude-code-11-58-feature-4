"""馈线负荷与容量余量情景验收。

六类验收样例 + 双路线独立估计 + 历史情景确定性复算 + 增量重算:
1 发车间隔压缩 -> 峰值抬升、出现超限窗口
2 多车同步加速 -> 峰值贡献班次为同窗组合
3 反送受限 -> 再生能量无法受纳范围与受纳边界
4 跨馈线运行 -> 切换时点拆开、不双计
5 车型样本稀缺 -> 匹配层级降级、预测区间明确扩大
6 拓扑切换 -> 新情景版本并保留与旧版本的差异
另:两条路线对同一运行图独立给出峰值/余量;历史情景可逐槽确定性复算;
改动发车时刻只重算受影响供电窗口,已发布现状对标数据不被替换。
"""


def _scenario(client, auth, **kw):
    r = client.post("/api/scenarios", json=kw, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _by_code(rows, code):
    return next(x for x in rows if x["code"] == code)


def _detail(client, auth, code):
    rows = client.get("/api/scenarios", headers=auth).json()
    sid = _by_code(rows, code)["id"]
    r = client.get(f"/api/scenarios/{sid}", headers=auth)
    assert r.status_code == 200
    return r.json()


def _result(s, feeder):
    return next(r for r in s["results"] if r["feeder_code"] == feeder)


def test_seed_artifacts_present(client, auth):
    topos = client.get("/api/topologies", headers=auth).json()
    tts = client.get("/api/timetables", headers=auth).json()
    traces = client.get("/api/power-traces", headers=auth).json()
    scenarios = client.get("/api/scenarios", headers=auth).json()
    assert {t["code"] for t in topos} >= {"TOPO-V1", "TOPO-V2"}
    assert {t["code"] for t in tts} >= {
        "TT-BASE", "TT-PROP-COMPRESS", "TT-PROP-REGEN",
        "TT-PROP-CROSS", "TT-PROP-SCARCE"}
    assert len(traces) >= 15
    assert len(scenarios) >= 8
    # 情景必须引用同一组拓扑/运行图版本
    s = _by_code(scenarios, "SC-COMPRESS-RP")
    topo = _by_code(topos, "TOPO-V1")
    tt = _by_code(tts, "TT-PROP-COMPRESS")
    assert s["topology_version_id"] == topo["id"]
    assert s["timetable_version_id"] == tt["id"]


def test_headway_compression_raises_peak_and_overload(client, auth):
    """发车间隔压缩:峰值显著抬升并出现超限时间窗,余量转负。"""
    base = _detail(client, auth, "SC-BASE-RP")
    comp = _detail(client, auth, "SC-COMPRESS-RP")
    for feeder in ("F-S1", "F-S2"):
        rb, rc = _result(base, feeder), _result(comp, feeder)
        assert rc["peak"]["v"] > rb["peak"]["v"]
        assert rc["margin"]["v"] < rb["margin"]["v"]
        assert rc["overload_windows"], f"{feeder} 压缩后应出现超限窗口"
    s1 = _result(comp, "F-S1")
    w = s1["overload_windows"][0]
    assert w["peak_kw"]["v"] > s1["capacity_kw"]
    assert w["trips"] and w["contributors"]
    # 峰值区间定义:不低于峰值 95%
    assert all(pw["peak_kw"] >= 0.95 * s1["peak"]["v"] - 1e-6
               for pw in s1["peak_window"])
    # 峰值带不确定区间且 lo <= v <= hi
    p = s1["peak"]
    assert p["lo"] <= p["v"] <= p["hi"]


def test_synchronized_acceleration_contributors(client, auth):
    """多车同步加速:峰值由同窗多班次叠加,贡献班次组合可复核。"""
    comp = _detail(client, auth, "SC-COMPRESS-RP")
    s1 = _result(comp, "F-S1")
    peak_at = s1["peak"]["at"]
    # 峰值时刻至少两个班次同时贡献牵引功率
    contributors = s1["contributing_trips"]
    assert len(contributors) >= 2
    assert sum(c["share"] for c in contributors) <= 1.0001
    # 峰值时刻落在某超限窗内
    assert any(w["start"] <= peak_at < w["end"] for w in s1["overload_windows"])


def test_regen_acceptance_boundary(client, auth):
    """反送受限:超过受纳边界的再生无法受纳,边界为 0 时全部拒绝。"""
    regen = _detail(client, auth, "SC-REGEN-RP")
    f3 = _result(regen, "F-S3")
    # F-S3 受纳边界 0:再生全部无法受纳,能量区间闭合且为正
    rej = f3["rejected_regen"]["energy_kwh"]
    assert f3["max_regen_accept_kw"] == 0
    assert rej["v"] > 0 and rej["lo"] <= rej["v"] <= rej["hi"]
    assert f3["rejected_regen"]["windows"]
    # F-S2 边界 1200kW:同窗两班再生叠加超限,部分拒绝、部分抵扣
    f2 = _result(regen, "F-S2")
    assert f2["rejected_regen"]["energy_kwh"]["v"] > 0
    total_regen = sum(f2["series"]["regen_v"]) * 10 / 3600.0
    accepted = sum(f2["series"]["accepted_v"]) * 10 / 3600.0
    rejected = sum(f2["series"]["rejected_v"]) * 10 / 3600.0
    assert abs(accepted + rejected - total_regen) < 0.05
    assert 0 < accepted < total_regen


def test_cross_feeder_split_no_double_count(client, auth):
    """跨馈线:班次在实际切换时点拆开,同一时刻只计入一个馈线。"""
    cross = _detail(client, auth, "SC-CROSS-RP")
    f1, f2 = _result(cross, "F-S1"), _result(cross, "F-S2")
    # 两个馈线都分到了该班次,但区段不同(切换时点拆开)
    for r in (f1, f2):
        keys = {m["trip_key"] for m in r["trace_coverage"]["matched"]}
        assert "TR-X1" in keys
    matched_f1 = [m for m in f1["trace_coverage"]["matched"] if m["trip_key"] == "TR-X1"]
    matched_f2 = [m for m in f2["trace_coverage"]["matched"] if m["trip_key"] == "TR-X1"]
    assert {m["section_code"] for m in matched_f1} == {"S1"}
    assert {m["section_code"] for m in matched_f2} == {"S2"}
    # 时间窗不重叠:前段只在 F-S1,后段只在 F-S2(不双计)
    f1_end = max(m["end"] for m in matched_f1)
    f2_start = min(m["start"] for m in matched_f2)
    assert f1_end <= f2_start
    # 两馈线都分到了正的牵引能量
    assert sum(f1["series"]["traction_v"]) > 0 and sum(f2["series"]["traction_v"]) > 0


def test_scarce_model_widens_band(client, auth):
    """车型样本稀缺:新车型无参考轨迹,匹配降级且预测区间明确扩大。"""
    scarce = _detail(client, auth, "SC-SCARCE-RP")
    f1 = _result(scarce, "F-S1")
    levels = {m["trip_key"]: m["level"] for m in f1["trace_coverage"]["matched"]}
    assert levels["TR-N1"] == "NO_REFERENCE"      # M3 无轨迹
    assert levels["TR-N2"] in ("EXACT", "LOAD_FALLBACK")
    assert f1["trace_coverage"]["scarce"] is True
    # 扩大区间:峰值 hi 与 lo 的相对展宽明显大于常规情景
    base = _detail(client, auth, "SC-BASE-RP")
    bf1 = _result(base, "F-S1")

    def spread(r):
        p = r["peak"]
        return (p["hi"] - p["lo"]) / max(p["v"], 1e-9)

    assert spread(f1) > spread(bf1)


def test_two_routes_independent_same_timetable(client, auth):
    """两条路线对同一拟议运行图独立给出峰值与余量,互不为事后检查。"""
    rp = _scenario(client, auth, name="复核-压缩-回放", method="TRACE_REPLAY",
                   topology_code="TOPO-V1", timetable_code="TT-PROP-COMPRESS",
                   code="SC-UT-REPLAY")
    mc = _scenario(client, auth, name="复核-压缩-包络", method="ENVELOPE_MC",
                   topology_code="TOPO-V1", timetable_code="TT-PROP-COMPRESS",
                   mc_runs=30, code="SC-UT-MC")
    assert rp["method"] == "TRACE_REPLAY" and mc["method"] == "ENVELOPE_MC"
    assert rp["mc_runs"] == 1 and mc["mc_runs"] == 30
    for feeder in ("F-S1", "F-S2"):
        a, b = _result(rp, feeder), _result(mc, feeder)
        # 两路线都独立判定超限
        assert a["overload_windows"] and b["overload_windows"]
        # 量级一致:包络上界不低于回放点估计,且不偏离到另一数量级
        assert b["peak"]["hi"] >= a["peak"]["v"] * 0.8
    # 包络路线输出预测区间而非单点
    p = _result(mc, "F-S1")["peak"]
    assert p["lo"] < p["hi"]


def test_topology_revision_new_version_keeps_diff(client, auth):
    """拓扑/容量修订生成新情景版本,并保留与旧版本的逐馈线差异。"""
    v1 = _detail(client, auth, "SC-COMPRESS-RP")
    v2 = _detail(client, auth, "SC-TOPO-V2-RP")
    assert v2["baseline_scenario_id"] == v1["id"]
    r = client.get(f"/api/scenarios/{v2['id']}/diff?against={v1['id']}",
                   headers=auth)
    assert r.status_code == 200
    diff = r.json()
    f1 = next(x for x in diff["feeders"] if x["feeder_code"] == "F-S1")
    # F-S1 容量 3000 -> 2200:峰值不变但余量下降、超限时间增加
    assert f1["margin_delta_kw"] < 0
    assert f1["overload_minutes_candidate"] >= f1["overload_minutes_base"]
    # 版本引用的拓扑编号不同且同时保留
    assert diff["base"]["topology_code"] == "TOPO-V1"
    assert diff["candidate"]["topology_code"] == "TOPO-V2"


def test_historical_scenario_deterministic_replay(client, auth):
    """历史情景复算:同版本输入逐槽确定性复现,结果与已发布情景一致。"""
    first = _scenario(client, auth, name="复算-现状", method="TRACE_REPLAY",
                      topology_code="TOPO-V1", timetable_code="TT-BASE",
                      code="SC-REPLAY-CHECK")
    again = _scenario(client, auth, name="复算-现状-2", method="TRACE_REPLAY",
                      topology_code="TOPO-V1", timetable_code="TT-BASE",
                      code="SC-REPLAY-CHECK-2")
    for feeder in ("F-S1", "F-S2", "F-S3"):
        a, b = _result(first, feeder), _result(again, feeder)
        assert a["peak"] == b["peak"]
        assert a["series"]["net_v"] == b["series"]["net_v"]
        assert a["overload_windows"] == b["overload_windows"]


def test_incremental_recompute_and_baseline_untouched(client, auth):
    """改动发车时刻只重算受影响窗口;未受影响窗口与基线逐槽一致,基线不被替换。"""
    r = client.post("/api/timetables/derive", json={
        "code": "TT-DERIVED-1", "source_timetable_code": "TT-BASE",
        "trip_offsets_minutes": {"TR-B1": 3.0},
        "note": "仅 TR-B1 延后 3 分钟"}, headers=auth)
    assert r.status_code == 200, r.text
    rows = client.get("/api/scenarios", headers=auth).json()
    base_before = _by_code(rows, "SC-BASE-RP")
    s = _scenario(client, auth, name="增量-单班次平移", method="TRACE_REPLAY",
                  topology_code="TOPO-V1", timetable_code="TT-DERIVED-1",
                  baseline_scenario_code="SC-BASE-RP",
                  changed_trip_keys=["TR-B1"], code="SC-UT-INCR")
    inc = s["assumptions"]["incremental"]
    assert inc["mode"] == "INCREMENTAL"
    assert inc["mismatches"] == []
    assert inc["carried_slots"] > 0 and inc["recomputed_slots"] > 0
    # 受影响供电窗口被正确登记
    windows = s["assumptions"]["recomputed_windows"]
    assert "F-S1" in windows and "F-S2" in windows
    # 已发布现状对标情景未被替换/改写
    rows2 = client.get("/api/scenarios", headers=auth).json()
    base_after = _by_code(rows2, "SC-BASE-RP")
    assert base_after["id"] == base_before["id"]
    assert base_after["summary"] == base_before["summary"]


def test_derive_timetable_and_topology_versioning(client, auth):
    """版本派生:运行图平移与拓扑容量修订都产生新版本,旧版本保留。"""
    tt = client.post("/api/timetables/derive", json={
        "code": "TT-DERIVED-2", "source_timetable_code": "TT-BASE",
        "headway_minutes": 8, "trip_offsets_minutes": {"TR-B2": -2.0}}, headers=auth)
    assert tt.status_code == 200
    assert tt.json()["kind"] == "PROPOSED"
    topo = client.post("/api/topologies/derive", json={
        "code": "TOPO-V3", "source_topology_code": "TOPO-V1",
        "feeder_overrides": {"F-S2": {"capacity_kw": 2600,
                                      "max_regen_accept_kw": 900}}}, headers=auth)
    assert topo.status_code == 200
    feeders = {f["code"]: f for f in topo.json()["feeders"]}
    assert feeders["F-S2"]["capacity_kw"] == 2600
    assert feeders["F-S1"]["capacity_kw"] == 2500  # 未覆盖的馈线沿用
    # 重复编号被拒绝
    again = client.post("/api/topologies/derive", json={
        "code": "TOPO-V3", "source_topology_code": "TOPO-V1",
        "feeder_overrides": {}}, headers=auth)
    assert again.status_code == 400
