"""API 路由。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import LATE_ARRIVAL_SECONDS, MC_RUNS_DEFAULT
from ..db import get_db
from ..models import (ConfigVersion, Consist, Device, EnergyFragment, Feeder, GridCell,
                      Line, MeterReading, Metric, PowerTrace, Reconciliation, Run,
                      RunEvent, RunSegment, Scenario, Section, TimetableTrip,
                      TimetableVersion, TopologyVersion, Train, User)
from ..services.evaluate import evaluate_run, rebuild_grid
from ..services.metrics import publish_run_metrics, reprocess_run
from ..services.reconcile import reconcile_grid_aligned, reconcile_run_aligned
from ..services.scenario import compute_scenario, diff_scenarios

router = APIRouter(prefix="/api")


# ---------------- 鉴权 ----------------

def current_user(authorization: str = Header(default=""),
                 db: Session = Depends(get_db)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    user = db.query(User).filter(User.token == token).first() if token else None
    if not user:
        raise HTTPException(401, "未登录或令牌无效")
    return user


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = (db.query(User).filter(User.username == body.username,
                                  User.password == body.password).first())
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    return {"token": user.token, "username": user.username, "role": user.role}


# ---------------- 元数据 ----------------

@router.get("/meta/overview")
def overview(db: Session = Depends(get_db), _: User = Depends(current_user)):
    latest_runs = {}
    for r in db.query(Run).order_by(Run.run_key, Run.version.desc()).all():
        latest_runs.setdefault(r.run_key, r)
    runs = list(latest_runs.values())
    recs = db.query(Reconciliation).all()
    return {
        "lines": db.query(Line).count(),
        "sections": db.query(Section).count(),
        "trains": db.query(Train).count(),
        "devices": db.query(Device).count(),
        "readings": db.query(MeterReading).count(),
        "runs": len(runs),
        "run_versions": db.query(Run).count(),
        "segments": db.query(RunSegment).count(),
        "fragments": db.query(EnergyFragment).count(),
        "reconciliations": len(recs),
        "metrics": db.query(Metric).count(),
        "comparable_full": sum(1 for r in recs if r.comparable == "FULL"),
        "comparable_partial": sum(1 for r in recs if r.comparable == "PARTIAL"),
    }


@router.get("/meta/catalog")
def catalog(db: Session = Depends(get_db), _: User = Depends(current_user)):
    lines = db.query(Line).all()
    sections = db.query(Section).order_by(Section.line_id, Section.seq).all()
    trains = db.query(Train).all()
    devices = db.query(Device).all()
    consists = db.query(Consist).all()
    return {
        "lines": [{"id": l.id, "code": l.code, "name": l.name} for l in lines],
        "sections": [{"id": s.id, "line_id": s.line_id, "code": s.code, "name": s.name,
                      "seq": s.seq, "length_km": s.length_km,
                      "multiplier_zone": s.multiplier_zone,
                      "substation_device_id": s.substation_device_id} for s in sections],
        "trains": [{"id": t.id, "code": t.code, "vehicle_model": t.vehicle_model} for t in trains],
        "devices": [{"id": d.id, "serial": d.serial, "kind": d.kind,
                     "train_id": d.train_id, "station_name": d.station_name} for d in devices],
        "consists": [{"id": c.id, "train_id": c.train_id, "code": c.code,
                      "valid_from": c.valid_from.isoformat(),
                      "valid_to": c.valid_to.isoformat() if c.valid_to else None}
                     for c in consists],
    }


# ---------------- 采集接入 ----------------

class ReadingIn(BaseModel):
    device_serial: str
    sampled_at: datetime
    received_at: Optional[datetime] = None
    seq: int
    traction_cum: Optional[float] = None
    regen_cum: Optional[float] = None
    aux_cum: Optional[float] = None
    import_cum: Optional[float] = None
    export_cum: Optional[float] = None
    multiplier: float = 1.0
    comm_status: str = "OK"


@router.post("/readings")
def ingest_readings(body: list[ReadingIn], db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    """批量接入原始累计值。迟到采样若落在已发布运行上,自动生成新运行版本并重发指标。"""
    now = datetime.utcnow()
    inserted, late_runs = 0, {}
    for item in body:
        device = db.query(Device).filter(Device.serial == item.device_serial).first()
        if not device:
            raise HTTPException(400, f"未知设备序列号 {item.device_serial}")
        received = item.received_at or now
        is_late = (received - item.sampled_at).total_seconds() > LATE_ARRIVAL_SECONDS
        db.add(MeterReading(device_id=device.id, sampled_at=item.sampled_at,
                            received_at=received, seq=item.seq,
                            traction_cum=item.traction_cum, regen_cum=item.regen_cum,
                            aux_cum=item.aux_cum, import_cum=item.import_cum,
                            export_cum=item.export_cum, multiplier=item.multiplier,
                            comm_status=item.comm_status))
        inserted += 1
        if is_late and device.kind == "ONBOARD":
            runs = (db.query(Run)
                    .filter(Run.train_id == device.train_id,
                            Run.started_at <= item.sampled_at,
                            Run.ended_at >= item.sampled_at,
                            Run.status == "PUBLISHED").all())
            for r in runs:
                late_runs[r.run_key] = r
    db.flush()

    new_versions = []
    for r in late_runs.values():
        new_run = reprocess_run(db, r, note="迟到补传")
        evaluate_run(db, new_run)
        rebuild_grid(db)
        reconcile_run_aligned(db, new_run)
        reconcile_grid_aligned(db, new_run)
        publish_run_metrics(db, new_run, note="迟到补传触发新版本")
        new_versions.append({"run_key": new_run.run_key, "version": new_run.version})
    db.commit()
    return {"inserted": inserted, "late_new_versions": new_versions}


@router.get("/readings")
def list_readings(device_id: Optional[int] = None, limit: int = 200,
                  db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = db.query(MeterReading).order_by(MeterReading.sampled_at.desc(),
                                        MeterReading.seq.desc())
    if device_id:
        q = q.filter(MeterReading.device_id == device_id)
    rows = q.limit(limit).all()
    return [{"id": r.id, "device_id": r.device_id, "serial": r.device.serial,
             "sampled_at": r.sampled_at.isoformat(),
             "received_at": r.received_at.isoformat(), "seq": r.seq,
             "traction_cum": r.traction_cum, "regen_cum": r.regen_cum,
             "aux_cum": r.aux_cum, "import_cum": r.import_cum,
             "export_cum": r.export_cum, "multiplier": r.multiplier,
             "comm_status": r.comm_status,
             "late": (r.received_at - r.sampled_at).total_seconds() > LATE_ARRIVAL_SECONDS}
            for r in rows]


@router.get("/devices")
def list_devices(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": d.id, "serial": d.serial, "kind": d.kind, "train_id": d.train_id,
             "station_name": d.station_name} for d in db.query(Device).all()]


# ---------------- 运行与能量 ----------------

def _run_json(db: Session, r: Run, detail: bool = False):
    out = {"id": r.id, "run_key": r.run_key, "version": r.version,
           "train": r.train.code, "vehicle_model": r.train.vehicle_model,
           "line": r.line.code, "driver_mode": r.driver_mode,
           "started_at": r.started_at.isoformat(), "ended_at": r.ended_at.isoformat(),
           "distance_km": r.distance_km, "passengers": r.passengers,
           "shift": r.shift, "status": r.status,
           "supersedes_id": r.supersedes_id}
    if detail:
        segs = (db.query(RunSegment).filter(RunSegment.run_id == r.id)
                .order_by(RunSegment.seq).all())
        out["segments"] = []
        for s in segs:
            frags = (db.query(EnergyFragment)
                     .filter(EnergyFragment.run_segment_id == s.id).all())
            out["segments"].append({
                "id": s.id, "seq": s.seq, "section": s.section.code,
                "direction": s.direction, "start_ts": s.start_ts.isoformat(),
                "end_ts": s.end_ts.isoformat(), "cut_reason": s.cut_reason,
                "consist_id": s.consist_id,
                "fragments": [{"id": f.id, "source": f.source, "quality": f.quality,
                               "coverage": f.coverage, "quantities": f.quantities,
                               "detail": f.detail} for f in frags]})
        out["events"] = [{"ts": e.ts.isoformat(), "type": e.type,
                          "section_id": e.section_id, "direction": e.direction}
                         for e in db.query(RunEvent)
                         .filter(RunEvent.run_id == r.id).order_by(RunEvent.ts).all()]
    return out


@router.get("/runs")
def list_runs(all_versions: bool = False, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    q = db.query(Run).order_by(Run.run_key, Run.version.desc())
    rows = q.all()
    if not all_versions:
        seen, rows2 = set(), []
        for r in rows:
            if r.run_key not in seen:
                seen.add(r.run_key)
                rows2.append(r)
        rows = rows2
    return [_run_json(db, r) for r in rows]


@router.get("/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    return _run_json(db, run, detail=True)


@router.get("/runs/{run_id}/reconciliation")
def run_reconciliation(run_id: int, method: str = "RUN_ALIGNED",
                       db: Session = Depends(get_db), _: User = Depends(current_user)):
    rec = (db.query(Reconciliation)
           .filter(Reconciliation.run_id == run_id, Reconciliation.method == method)
           .order_by(Reconciliation.id.desc()).first())
    if not rec:
        raise HTTPException(404, "该运行尚无此方法的核对结论")
    return {"id": rec.id, "run_id": rec.run_id, "run_version": rec.run_version,
            "method": rec.method, "range_start": rec.range_start.isoformat(),
            "range_end": rec.range_end.isoformat(), "comparable": rec.comparable,
            "coverage_onboard": rec.coverage_onboard,
            "coverage_supply": rec.coverage_supply,
            "onboard_net": rec.onboard_net, "supply_net": rec.supply_net,
            "diff": rec.diff, "attribution": rec.attribution,
            "undetermined": rec.undetermined}


@router.get("/runs/{run_id}/grid")
def run_grid(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    cells = (db.query(GridCell).order_by(GridCell.bucket_start).all())
    mine = [c for c in cells if run_id in (c.run_ids or [])]
    return [{"id": c.id, "section": c.section.code, "bucket_start": c.bucket_start.isoformat(),
             "bucket_minutes": c.bucket_minutes, "onboard": c.onboard,
             "supply": c.supply, "run_ids": c.run_ids} for c in mine]


# ---------------- 指标与复盘 ----------------

@router.get("/metrics")
def list_metrics(scope: Optional[str] = None, scope_key: Optional[str] = None,
                 db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = db.query(Metric).order_by(Metric.published_at.desc(), Metric.id.desc())
    if scope:
        q = q.filter(Metric.scope == scope)
    if scope_key:
        q = q.filter(Metric.scope_key == scope_key)
    return [{"id": m.id, "scope": m.scope, "scope_key": m.scope_key, "name": m.name,
             "value": m.value, "coverage": m.coverage, "method": m.method,
             "run_version": m.run_version, "published_at": m.published_at.isoformat(),
             "supersedes_id": m.supersedes_id, "diff_note": m.diff_note,
             "source_run_ids": m.source_run_ids} for m in q.limit(500).all()]


@router.get("/metrics/{metric_id}/replay")
def metric_replay(metric_id: int, db: Session = Depends(get_db),
                  _: User = Depends(current_user)):
    """复盘:某指标由哪些采样片段、配置版本和核对结论构成。"""
    m = db.get(Metric, metric_id)
    if not m:
        raise HTTPException(404, "指标不存在")
    runs = [db.get(Run, rid) for rid in m.source_run_ids]
    runs = [r for r in runs if r]
    chain = []
    cur = m
    while cur and cur.supersedes_id:
        prev = db.get(Metric, cur.supersedes_id)
        if not prev:
            break
        chain.append({"id": prev.id, "value": prev.value,
                      "run_version": prev.run_version,
                      "published_at": prev.published_at.isoformat(),
                      "diff_note": prev.diff_note})
        cur = prev
    runs_json = [_run_json(db, r, detail=True) for r in runs]
    # 采样片段涉及的所有原始读数
    reading_ids = []
    for r in runs:
        devs = (db.query(Device)
                .filter((Device.train_id == r.train_id) | (Device.kind == "SUBSTATION")).all())
        for d in devs:
            rows = (db.query(MeterReading)
                    .filter(MeterReading.device_id == d.id,
                            MeterReading.sampled_at >= r.started_at,
                            MeterReading.sampled_at <= r.ended_at)
                    .order_by(MeterReading.sampled_at, MeterReading.seq).all())
            reading_ids.extend(x.id for x in rows)
    recs = (db.query(Reconciliation)
            .filter(Reconciliation.run_id.in_([r.id for r in runs])).all()) if runs else []
    configs = (db.query(ConfigVersion)
               .filter(ConfigVersion.valid_from <= (runs[0].ended_at if runs else datetime.utcnow()))
               .order_by(ConfigVersion.valid_from).all()) if runs else []
    return {
        "metric": {"id": m.id, "scope": m.scope, "scope_key": m.scope_key,
                   "name": m.name, "value": m.value, "coverage": m.coverage,
                   "method": m.method, "run_version": m.run_version,
                   "published_at": m.published_at.isoformat(),
                   "diff_note": m.diff_note},
        "history": chain,
        "runs": runs_json,
        "reading_ids": sorted(set(reading_ids)),
        "reconciliations": [
            {"id": x.id, "method": x.method, "comparable": x.comparable,
             "onboard_net": x.onboard_net, "supply_net": x.supply_net,
             "diff": x.diff, "attribution": x.attribution,
             "undetermined": x.undetermined} for x in recs],
        "config_versions": [
            {"id": c.id, "kind": c.kind, "payload": c.payload,
             "valid_from": c.valid_from.isoformat(),
             "valid_to": c.valid_to.isoformat() if c.valid_to else None,
             "note": c.note} for c in configs],
    }


@router.get("/config-versions")
def list_config_versions(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"id": c.id, "kind": c.kind, "payload": c.payload,
             "valid_from": c.valid_from.isoformat(),
             "valid_to": c.valid_to.isoformat() if c.valid_to else None,
             "note": c.note}
            for c in db.query(ConfigVersion).order_by(ConfigVersion.valid_from).all()]


# ---------------- 馈线负荷情景:轨迹/拓扑/运行图/情景 ----------------

def _topology_json(t: TopologyVersion, detail: bool = False):
    out = {"id": t.id, "code": t.code, "version": t.version, "status": t.status,
           "valid_from": t.valid_from.isoformat(), "note": t.note,
           "feeder_count": len(t.feeders)}
    if detail:
        out["feeders"] = [{"id": f.id, "code": f.code,
                           "substation_serial": f.substation_serial,
                           "capacity_kw": f.capacity_kw,
                           "max_regen_accept_kw": f.max_regen_accept_kw,
                           "sections": f.sections} for f in t.feeders]
    return out


def _timetable_json(t: TimetableVersion, detail: bool = False):
    out = {"id": t.id, "code": t.code, "version": t.version, "kind": t.kind,
           "status": t.status, "line_code": t.line_code,
           "valid_from": t.valid_from.isoformat(), "headway_minutes": t.headway_minutes,
           "note": t.note, "trip_count": len(t.trips)}
    if detail:
        out["trips"] = [{"id": x.id, "trip_key": x.trip_key, "train_code": x.train_code,
                         "vehicle_model": x.vehicle_model, "consist_code": x.consist_code,
                         "load_band": x.load_band, "legs": x.legs} for x in t.trips]
    return out


def _scenario_json(s: Scenario, detail: bool = False):
    out = {"id": s.id, "code": s.code, "name": s.name, "method": s.method,
           "topology_version_id": s.topology_version_id,
           "timetable_version_id": s.timetable_version_id,
           "baseline_scenario_id": s.baseline_scenario_id, "mc_runs": s.mc_runs,
           "status": s.status, "range_start": s.range_start.isoformat(),
           "range_end": s.range_end.isoformat(), "summary": s.summary,
           "assumptions": s.assumptions, "note": s.note,
           "created_at": s.created_at.isoformat()}
    if detail:
        out["results"] = [{
            "id": r.id, "feeder_code": r.feeder_code,
            "substation_serial": r.substation_serial, "capacity_kw": r.capacity_kw,
            "max_regen_accept_kw": r.max_regen_accept_kw, "peak": r.peak,
            "peak_window": r.peak_window, "margin": r.margin,
            "overload_windows": r.overload_windows, "rejected_regen": r.rejected_regen,
            "trace_coverage": r.trace_coverage,
            "contributing_trips": r.contributing_trips, "series": r.series}
            for r in s.results]
    return out


@router.get("/power-traces")
def list_power_traces(line_code: Optional[str] = None, section_code: Optional[str] = None,
                      vehicle_model: Optional[str] = None,
                      db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = db.query(PowerTrace)
    if line_code:
        q = q.filter(PowerTrace.line_code == line_code)
    if section_code:
        q = q.filter(PowerTrace.section_code == section_code)
    if vehicle_model:
        q = q.filter(PowerTrace.vehicle_model == vehicle_model)
    return [{"id": t.id, "code": t.code, "line_code": t.line_code,
             "section_code": t.section_code, "vehicle_model": t.vehicle_model,
             "consist_code": t.consist_code, "load_band": t.load_band,
             "duration_s": t.duration_s, "traction_energy_kwh": t.traction_energy_kwh,
             "regen_energy_kwh": t.regen_energy_kwh, "sample_count": t.sample_count,
             "source_run_id": t.source_run_id, "series": t.series}
            for t in q.order_by(PowerTrace.line_code, PowerTrace.section_code).all()]


@router.get("/topologies")
def list_topologies(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [_topology_json(t, detail=True)
            for t in db.query(TopologyVersion).order_by(TopologyVersion.version).all()]


@router.get("/timetables")
def list_timetables(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [_timetable_json(t, detail=True)
            for t in db.query(TimetableVersion).order_by(TimetableVersion.version).all()]


class TopologyDeriveIn(BaseModel):
    code: str
    source_topology_code: str
    feeder_overrides: dict          # {feeder_code: {"capacity_kw":..,"max_regen_accept_kw":..}}
    note: Optional[str] = None


@router.post("/topologies/derive")
def derive_topology(body: TopologyDeriveIn, db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    """修订馈线容量/反送边界生成新拓扑版本,旧版本标记 SUPERSEDED 并保留。"""
    src = (db.query(TopologyVersion)
           .filter(TopologyVersion.code == body.source_topology_code).first())
    if not src:
        raise HTTPException(404, "源拓扑版本不存在")
    if db.query(TopologyVersion).filter(TopologyVersion.code == body.code).first():
        raise HTTPException(400, f"拓扑版本 {body.code} 已存在")
    new = TopologyVersion(code=body.code, version=src.version + 1, status="ACTIVE",
                          valid_from=datetime.utcnow(),
                          note=body.note or f"由 {src.code} 修订馈线容量/反送边界")
    for f in src.feeders:
        ov = body.feeder_overrides.get(f.code, {})
        new.feeders.append(Feeder(
            code=f.code, substation_serial=f.substation_serial,
            capacity_kw=ov.get("capacity_kw", f.capacity_kw),
            max_regen_accept_kw=ov.get("max_regen_accept_kw", f.max_regen_accept_kw),
            sections=f.sections))
    src.status = "SUPERSEDED"
    db.add(new); db.flush(); db.commit()
    return _topology_json(new, detail=True)


class TimetableDeriveIn(BaseModel):
    code: str
    source_timetable_code: str
    kind: str = "PROPOSED"
    headway_minutes: Optional[float] = None
    trip_offsets_minutes: Optional[dict] = None   # {trip_key: 偏移分钟,负值=提前发车}
    note: Optional[str] = None


@router.post("/timetables/derive")
def derive_timetable(body: TimetableDeriveIn, db: Session = Depends(get_db),
                     _: User = Depends(current_user)):
    """改动发车时刻:由既有运行图派生新版本,只平移指定班次的全部行程时刻。"""
    src = (db.query(TimetableVersion)
           .filter(TimetableVersion.code == body.source_timetable_code).first())
    if not src:
        raise HTTPException(404, "源运行图版本不存在")
    if db.query(TimetableVersion).filter(TimetableVersion.code == body.code).first():
        raise HTTPException(400, f"运行图版本 {body.code} 已存在")
    offsets = body.trip_offsets_minutes or {}
    new = TimetableVersion(code=body.code, version=src.version + 1, kind=body.kind,
                           status="DRAFT", line_code=src.line_code,
                           valid_from=datetime.utcnow(),
                           headway_minutes=body.headway_minutes,
                           note=body.note or f"由 {src.code} 调整发车时刻派生")
    for tr in src.trips:
        shift = offsets.get(tr.trip_key, 0.0)
        legs = [dict(leg,
                     enter_at=(datetime.fromisoformat(leg["enter_at"])
                               + timedelta(minutes=shift)).isoformat(),
                     exit_at=(datetime.fromisoformat(leg["exit_at"])
                              + timedelta(minutes=shift)).isoformat())
                for leg in tr.legs]
        new.trips.append(TimetableTrip(
            trip_key=tr.trip_key, train_code=tr.train_code,
            vehicle_model=tr.vehicle_model, consist_code=tr.consist_code,
            load_band=tr.load_band, legs=legs))
    db.add(new); db.flush(); db.commit()
    # 拟议运行图独立成版,已发布现状基线(BASELINE)状态与数据保持不变
    return _timetable_json(new, detail=True)


@router.get("/scenarios")
def list_scenarios(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.query(Scenario).order_by(Scenario.id.desc()).all()
    return [_scenario_json(s) for s in rows]


@router.get("/scenarios/{scenario_id}")
def scenario_detail(scenario_id: int, db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    s = db.get(Scenario, scenario_id)
    if not s:
        raise HTTPException(404, "情景不存在")
    return _scenario_json(s, detail=True)


class ScenarioIn(BaseModel):
    name: str
    method: str                                  # TRACE_REPLAY|ENVELOPE_MC
    topology_code: str
    timetable_code: str
    mc_runs: Optional[int] = None
    code: Optional[str] = None
    baseline_scenario_code: Optional[str] = None
    changed_trip_keys: Optional[list[str]] = None
    note: Optional[str] = None


@router.post("/scenarios")
def create_scenario(body: ScenarioIn, db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    topology = (db.query(TopologyVersion)
                .filter(TopologyVersion.code == body.topology_code).first())
    timetable = (db.query(TimetableVersion)
                 .filter(TimetableVersion.code == body.timetable_code).first())
    if not topology:
        raise HTTPException(404, f"拓扑版本 {body.topology_code} 不存在")
    if not timetable:
        raise HTTPException(404, f"运行图版本 {body.timetable_code} 不存在")
    baseline = None
    if body.baseline_scenario_code:
        baseline = (db.query(Scenario)
                    .filter(Scenario.code == body.baseline_scenario_code).first())
        if not baseline:
            raise HTTPException(404, "基线情景不存在")
    code = body.code or f"SC-{db.query(Scenario).count() + 1:04d}"
    if db.query(Scenario).filter(Scenario.code == code).first():
        raise HTTPException(400, f"情景编号 {code} 已存在")
    mc_runs = body.mc_runs or (MC_RUNS_DEFAULT if body.method == "ENVELOPE_MC" else 1)
    try:
        s = compute_scenario(
            db, code=code, name=body.name, method=body.method, topology=topology,
            timetable=timetable, mc_runs=mc_runs, baseline_scenario=baseline,
            changed_trip_keys=body.changed_trip_keys, note=body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return _scenario_json(s, detail=True)


@router.get("/scenarios/{scenario_id}/diff")
def scenario_diff(scenario_id: int, against: int, db: Session = Depends(get_db),
                  _: User = Depends(current_user)):
    """当前情景相对 against 基线情景的逐馈线差异(基线不被修改)。"""
    cand = db.get(Scenario, scenario_id)
    base = db.get(Scenario, against)
    if not base or not cand:
        raise HTTPException(404, "情景不存在")
    return diff_scenarios(base, cand)
