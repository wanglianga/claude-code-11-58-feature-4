"""种子数据:五类验收场景。

RUN-1001 跨区段折返(T01, L1, S1->S2 上行折返后下行返回)
RUN-1002 设备重启 + 倍率区间切换 + 运行中换编组(T02, L1)
RUN-1003 回馈受限(T03, L2/S3,变电所不接受回馈上网)
RUN-1004 车载与供电表差异(T01, L1,差异=线损+未明残差)
RUN-1005 补传读数(T02, L1,先按缺测发布 v1,迟到数据触发 v2)
RUN-1006 供电表缺失(T03, L2,供电侧输出未知范围,不以车载冒充闭合)
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import (ConfigVersion, Consist, Device, Feeder, Line, MeterReading,
                     PowerTrace, Run, RunConsist, RunEvent, Section, TimetableTrip,
                     TimetableVersion, TopologyVersion, Train, User, create_all,
                     drop_all)
from .services.evaluate import evaluate_run, rebuild_grid
from .services.metrics import publish_run_metrics, reprocess_run
from .services.reconcile import reconcile_grid_aligned, reconcile_run_aligned
from .services.scenario import compute_scenario

D1 = datetime(2026, 10, 20, 8, 0)   # 场景1/2
D4 = datetime(2026, 10, 21, 14, 0)  # 场景4
D5 = datetime(2026, 10, 22, 16, 0)  # 场景5
D3 = datetime(2026, 11, 18, 7, 0)   # 场景3(回馈受限配置生效后)
D6 = datetime(2026, 11, 19, 9, 0)   # 场景6

STEP = timedelta(minutes=5)


class Counter:
    """每台设备的累计表码状态(跨运行连续累计)。"""
    def __init__(self):
        self.v = {"traction": 1000.0, "regen": 200.0, "aux": 500.0,
                  "import": 5000.0, "export": 300.0}
        self.seq = 0


def gen_onboard(db: Session, device: Device, st: Counter, t0, t1,
                rates=(0.5, 0.1, 0.05), multiplier=1.0,
                reset_at=None, mult_change_at=None, mult_after=2.0,
                skip_after=None, received_delay_min=0):
    """生成车载读数。reset_at: 该时刻设备重启,计数器归零;
    mult_change_at: 倍率切换时刻;skip_after: 该时刻后缺测(通信中断)。"""
    rows = []
    t, mult = t0, multiplier
    while t <= t1:
        if skip_after and t > skip_after:
            break
        if mult_change_at and t >= mult_change_at:
            mult = mult_after
        if reset_at and t == reset_at:
            st.v["traction"] = st.v["regen"] = st.v["aux"] = 0.0
        st.seq += 1
        rows.append(MeterReading(
            device_id=device.id, sampled_at=t,
            received_at=t + timedelta(minutes=received_delay_min),
            seq=st.seq, traction_cum=round(st.v["traction"], 3),
            regen_cum=round(st.v["regen"], 3), aux_cum=round(st.v["aux"], 3),
            multiplier=mult, comm_status="OK"))
        st.v["traction"] += rates[0] * 5
        st.v["regen"] += rates[1] * 5
        st.v["aux"] += rates[2] * 5
        t += STEP
    db.add_all(rows)
    return rows


def gen_supply(db: Session, device: Device, st: Counter, t0, t1,
               imp_rate=0.62, exp_rate=0.12):
    rows, t = [], t0
    while t <= t1:
        st.seq += 1
        rows.append(MeterReading(
            device_id=device.id, sampled_at=t, received_at=t + timedelta(seconds=30),
            seq=st.seq, import_cum=round(st.v["import"], 3),
            export_cum=round(st.v["export"], 3), multiplier=1.0, comm_status="OK"))
        st.v["import"] += imp_rate * 5
        st.v["export"] += exp_rate * 5
        t += STEP
    db.add_all(rows)
    return rows


def mk_run(db, key, train, line, start, end, dist, pax, shift, mode="AM"):
    run = Run(run_key=key, version=1, train_id=train.id, line_id=line.id,
              driver_mode=mode, started_at=start, ended_at=end,
              distance_km=dist, passengers=pax, shift=shift)
    db.add(run)
    db.flush()
    return run


def ev(db, run, ts, type_, section=None, direction=None, payload=None):
    db.add(RunEvent(run_id=run.id, ts=ts, type=type_,
                    section_id=section.id if section else None,
                    direction=direction, payload=payload))


def process(db, run, note=None):
    db.flush()  # 会话关闭 autoflush,先落库再切分求值
    evaluate_run(db, run)
    rebuild_grid(db)
    reconcile_run_aligned(db, run)
    reconcile_grid_aligned(db, run)
    publish_run_metrics(db, run, note=note)


def seed():
    drop_all()
    create_all()
    db: Session = SessionLocal()
    try:
        # ---- 用户 ----
        db.add_all([
            User(username="admin", password="admin123", role="admin",
                 token="tok-admin"),
            User(username="viewer", password="viewer123", role="viewer",
                 token="tok-viewer"),
        ])
        # ---- 线路/区段 ----
        l1 = Line(code="L1", name="1号线")
        l2 = Line(code="L2", name="2号线")
        db.add_all([l1, l2]); db.flush()
        # ---- 列车/编组/设备 ----
        t01 = Train(code="T01", vehicle_model="M1")
        t02 = Train(code="T02", vehicle_model="M1")
        t03 = Train(code="T03", vehicle_model="M2")
        db.add_all([t01, t02, t03]); db.flush()
        c01 = Consist(train_id=t01.id, code="6B", valid_from=datetime(2026, 1, 1))
        c02a = Consist(train_id=t02.id, code="6B", valid_from=datetime(2026, 1, 1),
                       valid_to=datetime(2026, 10, 20, 10, 15))
        c02b = Consist(train_id=t02.id, code="4B", valid_from=datetime(2026, 10, 20, 10, 15))
        c03 = Consist(train_id=t03.id, code="6A", valid_from=datetime(2026, 1, 1))
        db.add_all([c01, c02a, c02b, c03]); db.flush()
        ob1 = Device(serial="OB-T01", kind="ONBOARD", train_id=t01.id)
        ob2 = Device(serial="OB-T02", kind="ONBOARD", train_id=t02.id)
        ob3 = Device(serial="OB-T03", kind="ONBOARD", train_id=t03.id)
        sub1 = Device(serial="SUB-S1", kind="SUBSTATION", station_name="A站牵引所")
        sub2 = Device(serial="SUB-S2", kind="SUBSTATION", station_name="B站牵引所")
        sub3 = Device(serial="SUB-S3", kind="SUBSTATION", station_name="D站牵引所")
        db.add_all([ob1, ob2, ob3, sub1, sub2, sub3]); db.flush()
        s1 = Section(line_id=l1.id, code="S1", name="A站-B站", seq=1, length_km=10,
                     multiplier_zone="Z1", substation_device_id=sub1.id)
        s2 = Section(line_id=l1.id, code="S2", name="B站-C站", seq=2, length_km=12,
                     multiplier_zone="Z1", substation_device_id=sub2.id)
        s3 = Section(line_id=l2.id, code="S3", name="D站-E站", seq=1, length_km=8,
                     multiplier_zone="Z2", substation_device_id=sub3.id)
        db.add_all([s1, s2, s3]); db.flush()
        # ---- 配置版本 ----
        db.add_all([
            ConfigVersion(kind="MULTIPLIER", valid_from=datetime(2026, 1, 1),
                          payload={"OB-T01": 1.0, "OB-T02": 1.0, "OB-T03": 1.0},
                          note="车载表初始倍率"),
            ConfigVersion(kind="MULTIPLIER", valid_from=datetime(2026, 10, 20, 10, 40),
                          payload={"OB-T02": 2.0},
                          note="T02 进入倍率区间 Z2,倍率 1.0->2.0"),
            ConfigVersion(kind="REGEN_ACCEPT", valid_from=datetime(2026, 1, 1),
                          payload={"default": True, "stations": {}},
                          note="默认接受回馈上网"),
            ConfigVersion(kind="REGEN_ACCEPT", valid_from=datetime(2026, 11, 1),
                          payload={"default": True, "stations": {"SUB-S3": False}},
                          note="D站牵引所回馈装置检修,不接受回馈上网"),
            ConfigVersion(kind="LOSS_RATE", valid_from=datetime(2026, 1, 1),
                          payload={"rate": 0.06}, note="线网损耗经验值 6%"),
        ])

        st = {d.serial: Counter() for d in [ob1, ob2, ob3, sub1, sub2, sub3]}

        # ============ RUN-1001 跨区段折返 ============
        r1 = mk_run(db, "RUN-1001", t01, l1, D1, D1 + timedelta(minutes=85),
                    44, 1200, "早高峰-1")
        db.add(RunConsist(run_id=r1.id, consist_id=c01.id, valid_from=D1))
        ev(db, r1, D1, "ENTER_SECTION", s1, "UP")
        ev(db, r1, D1 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r1, D1 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=30), "STOP", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=35), "START_MOVE", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=45), "TURNBACK", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=65), "EXIT_SECTION", s2, "DOWN")
        ev(db, r1, D1 + timedelta(minutes=65), "ENTER_SECTION", s1, "DOWN")
        ev(db, r1, D1 + timedelta(minutes=85), "EXIT_SECTION", s1, "DOWN")
        gen_onboard(db, ob1, st["OB-T01"], D1, D1 + timedelta(minutes=85))
        gen_supply(db, sub1, st["SUB-S1"], D1, D1 + timedelta(minutes=90),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], D1 + timedelta(minutes=20),
                   D1 + timedelta(minutes=70), imp_rate=0.60, exp_rate=0.10)
        process(db, r1)

        # ============ RUN-1002 设备重启+倍率切换+换编组 ============
        t0 = D1 + timedelta(hours=2)  # 10:00-10:50,避开 RUN-1001 的供电窗口
        r2 = mk_run(db, "RUN-1002", t02, l1, t0, t0 + timedelta(minutes=50),
                    22, 800, "早高峰-1", mode="CM")
        db.add_all([
            RunConsist(run_id=r2.id, consist_id=c02a.id, valid_from=t0,
                       valid_to=t0 + timedelta(minutes=15)),
            RunConsist(run_id=r2.id, consist_id=c02b.id,
                       valid_from=t0 + timedelta(minutes=15)),
        ])
        ev(db, r2, t0, "ENTER_SECTION", s1, "UP")
        ev(db, r2, t0 + timedelta(minutes=15), "CONSIST_CHANGE", s1, "UP",
           payload={"consist_id": c02b.id})
        ev(db, r2, t0 + timedelta(minutes=25), "EXIT_SECTION", s1, "UP")
        ev(db, r2, t0 + timedelta(minutes=25), "ENTER_SECTION", s2, "UP")
        ev(db, r2, t0 + timedelta(minutes=50), "EXIT_SECTION", s2, "UP")
        gen_onboard(db, ob2, st["OB-T02"], t0, t0 + timedelta(minutes=50),
                    reset_at=t0 + timedelta(minutes=10),
                    mult_change_at=t0 + timedelta(minutes=40), mult_after=2.0)
        gen_supply(db, sub1, st["SUB-S1"], t0, t0 + timedelta(minutes=30),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], t0 + timedelta(minutes=25),
                   t0 + timedelta(minutes=55), imp_rate=0.60, exp_rate=0.10)
        process(db, r2)

        # ============ RUN-1003 回馈受限 ============
        r3 = mk_run(db, "RUN-1003", t03, l2, D3, D3 + timedelta(minutes=25),
                    8, 500, "早高峰-2")
        db.add(RunConsist(run_id=r3.id, consist_id=c03.id, valid_from=D3))
        ev(db, r3, D3, "ENTER_SECTION", s3, "UP")
        ev(db, r3, D3 + timedelta(minutes=25), "EXIT_SECTION", s3, "UP")
        gen_onboard(db, ob3, st["OB-T03"], D3, D3 + timedelta(minutes=25),
                    rates=(0.5, 0.4, 0.05))  # 大回馈
        gen_supply(db, sub3, st["SUB-S3"], D3, D3 + timedelta(minutes=30),
                   imp_rate=0.30, exp_rate=0.0)  # 电网不接受回馈
        process(db, r3)

        # ============ RUN-1004 车载与供电表差异 ============
        r4 = mk_run(db, "RUN-1004", t01, l1, D4, D4 + timedelta(minutes=40),
                    22, 600, "平峰-1")
        db.add(RunConsist(run_id=r4.id, consist_id=c01.id, valid_from=D4))
        ev(db, r4, D4, "ENTER_SECTION", s1, "UP")
        ev(db, r4, D4 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r4, D4 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r4, D4 + timedelta(minutes=40), "EXIT_SECTION", s2, "UP")
        gen_onboard(db, ob1, st["OB-T01"], D4, D4 + timedelta(minutes=40))
        gen_supply(db, sub1, st["SUB-S1"], D4, D4 + timedelta(minutes=25),
                   imp_rate=0.78, exp_rate=0.05)  # 差异偏大
        gen_supply(db, sub2, st["SUB-S2"], D4 + timedelta(minutes=20),
                   D4 + timedelta(minutes=45), imp_rate=0.74, exp_rate=0.05)
        process(db, r4)

        # ============ RUN-1005 补传读数 ============
        r5 = mk_run(db, "RUN-1005", t02, l1, D5, D5 + timedelta(minutes=40),
                    22, 900, "平峰-2")
        db.add(RunConsist(run_id=r5.id, consist_id=c02b.id, valid_from=D5))
        ev(db, r5, D5, "ENTER_SECTION", s1, "UP")
        ev(db, r5, D5 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r5, D5 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r5, D5 + timedelta(minutes=40), "EXIT_SECTION", s2, "UP")
        # v1: 16:15 之后车载缺测(通信中断),先按缺测发布
        gen_onboard(db, ob2, st["OB-T02"], D5, D5 + timedelta(minutes=40),
                    skip_after=D5 + timedelta(minutes=15))
        gen_supply(db, sub1, st["SUB-S1"], D5, D5 + timedelta(minutes=25),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], D5 + timedelta(minutes=20),
                   D5 + timedelta(minutes=45), imp_rate=0.60, exp_rate=0.10)
        process(db, r5)  # 发布 v1(低覆盖)
        # 迟到补传:16:20-16:40 的读数在 1 小时后到达 -> 触发 v2
        late_rows = []
        t = D5 + timedelta(minutes=20)
        while t <= D5 + timedelta(minutes=40):
            st["OB-T02"].seq += 1
            late_rows.append(MeterReading(
                device_id=ob2.id, sampled_at=t,
                received_at=t + timedelta(hours=1),  # 迟到
                seq=st["OB-T02"].seq,
                traction_cum=round(st["OB-T02"].v["traction"], 3),
                regen_cum=round(st["OB-T02"].v["regen"], 3),
                aux_cum=round(st["OB-T02"].v["aux"], 3),
                multiplier=1.0, comm_status="OK"))
            st["OB-T02"].v["traction"] += 0.5 * 5
            st["OB-T02"].v["regen"] += 0.1 * 5
            st["OB-T02"].v["aux"] += 0.05 * 5
            t += STEP
        db.add_all(late_rows); db.flush()
        r5v2 = reprocess_run(db, r5, note="迟到补传")
        process(db, r5v2, note="迟到补传触发新版本")

        # ============ RUN-1006 供电表缺失 ============
        r6 = mk_run(db, "RUN-1006", t03, l2, D6, D6 + timedelta(minutes=25),
                    8, 450, "早高峰-2")
        db.add(RunConsist(run_id=r6.id, consist_id=c03.id, valid_from=D6))
        ev(db, r6, D6, "ENTER_SECTION", s3, "UP")
        ev(db, r6, D6 + timedelta(minutes=25), "EXIT_SECTION", s3, "UP")
        gen_onboard(db, ob3, st["OB-T03"], D6, D6 + timedelta(minutes=25))
        # SUB-S3 在该窗口无任何读数(表计检修)-> 供电侧未知
        process(db, r6)

        # ============ 馈线负荷与容量余量情景 ============
        from .scenario_seed import seed_scenarios
        run_by_key = {"RUN-1001": r1.id, "RUN-1002": r2.id, "RUN-1003": r3.id,
                      "RUN-1004": r4.id, "RUN-1005": r5v2.id, "RUN-1006": r6.id}
        scenario_codes = seed_scenarios(db, run_by_key)

        db.commit()
        print(f"seed ok: 6 runs, 含五类验收场景;馈线情景 {len(scenario_codes)} 个")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
