import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from statistics import mean
from threading import Lock
from typing import Dict, List, Literal, Optional
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
import csv
import io

from backend.models import (
    SessionLocal,
    SimulationTickLog,
    AiCouncilDecisionLog,
    AuditTrailLog,
    LotTraceEvent,
    FabCheckpoint,
    OperatorSession,
    init_db,
)
from backend import auth, checkpoint
from backend.simulator import M16SemiconFabSimulator
from backend.ai_council import evaluate_and_decide

sim_engine = M16SemiconFabSimulator(seed=2026)
state_lock = asyncio.Lock()
live_streaming_enabled = True
ai_control_mode = "AUTO_PILOT"  # "AUTO_PILOT" or "HITL"
pending_ai_decision: Optional[Dict] = None
operator_accounts = None
login_attempts = {}
login_attempts_lock = Lock()


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict):
        dead = []
        for conn in list(self.active_connections):
            try:
                if operator_accounts is not None and not websocket_actor(conn):
                    await conn.close(code=1008)
                    dead.append(conn)
                    continue
                await asyncio.wait_for(conn.send_json(message), timeout=2)
            except Exception:
                dead.append(conn)
        for d in dead:
            self.disconnect(d)


ws_manager = ConnectionManager()


def persist_tick(db, snap: Dict, run_mode: str = "LIVE"):
    kpi = snap["kpi"]
    oht = snap["amhs_oht"]
    row = SimulationTickLog(
        run_mode=run_mode,
        tick=snap["tick"],
        eds_yield_pct=kpi["eds_yield_pct"],
        oee_pct=kpi["oee_pct"],
        availability_pct=kpi["availability_pct"],
        power_kw=kpi["power_kw"],
        unit_cost_usd=kpi["unit_cost_usd"],
        wip_foup_count=kpi["wip_foup_count"],
        completed_wafers=kpi["completed_wafers"],
        q_time_violations=kpi["q_time_violations"],
        oht_congestion_pct=oht["congestion_pct"],
        oht_avg_delivery_sec=oht["avg_delivery_sec"],
        tools_snapshot=snap["tools"],
    )
    db.add(row)


def log_audit(actor_id: str, actor_role: str, action_type: str, target_tool: str, detail: str, allowed: bool = True):
    db = SessionLocal()
    try:
        db.add(AuditTrailLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=action_type,
            target_tool=target_tool,
            detail=detail,
            allowed=allowed,
        ))
        db.commit()
    finally:
        db.close()


def apply_decision(engine, decision):
    for act in decision["agreed_action"]:
        tool = engine.tools[act["tool_id"]]
        if act.get("action") == "PM" and tool.state.value in ("SDT", "UDT"):
            continue
        feed = act.get("feed_rate")
        engine.apply_tool_control(
            tool_id=act["tool_id"],
            feed_rate=min(feed, tool.feed_rate) if feed is not None else None,
            action=act.get("action"),
        )


def persist_lot_events(db):
    events = sorted(sim_engine.lot_events, key=lambda event: (event["fab_minute"], event["seq"]))
    db.add_all(LotTraceEvent(trace_id=event["trace_id"], lot_id=event["lot_id"],
                             fab_minute=event["fab_minute"], payload=event) for event in events)
    return events


def lot_history(db, trace_id, lot_id=None, limit=120):
    query = db.query(LotTraceEvent).filter_by(trace_id=trace_id)
    if lot_id:
        query = query.filter_by(lot_id=lot_id)
    rows = query.order_by(LotTraceEvent.fab_minute.desc(), LotTraceEvent.id.desc()).limit(limit).all()
    return [{**row.payload, "recorded_at": row.recorded_at.isoformat()} for row in reversed(rows)]


def current_checkpoint():
    return {"sim": checkpoint.snapshot(sim_engine), "pending": pending_ai_decision,
            "live": live_streaming_enabled, "mode": ai_control_mode}


def load_checkpoint(payload):
    global sim_engine, pending_ai_decision, live_streaming_enabled, ai_control_mode
    sim_engine = checkpoint.restore(payload["sim"])
    pending_ai_decision, live_streaming_enabled, ai_control_mode = (
        payload["pending"], payload["live"], payload["mode"],
    )


def persist_checkpoint(db):
    db.merge(FabCheckpoint(id=1, payload=current_checkpoint()))


@asynccontextmanager
async def rollback_uncommitted_state():
    before = current_checkpoint()
    try:
        yield
    except BaseException:
        load_checkpoint(before)
        raise


@asynccontextmanager
async def operating_state():
    async with state_lock, rollback_uncommitted_state():
        yield


async def execute_one_tick(run_mode: str = "LIVE") -> Dict:
    global pending_ai_decision
    previous_q_time = sim_engine.snapshot()["kpi"]["q_time_violations"]
    snap = sim_engine.step()
    decision = None
    if ai_control_mode == "AUTO_PILOT" or pending_ai_decision is None:
        decision = await evaluate_and_decide(snap, control_mode=ai_control_mode,
                                             q_time_delta=snap["kpi"]["q_time_violations"] - previous_q_time)
    with SessionLocal.begin() as db:
        if decision:
            applied = ai_control_mode == "AUTO_PILOT"
            if applied:
                apply_decision(sim_engine, decision)
                snap = sim_engine.snapshot(override_power_kw=snap["kpi"]["power_kw"])
            row = AiCouncilDecisionLog(
                **decision, applied=applied, approved_by="AUTO_PILOT" if applied else None,
            )
            db.add(row)
            db.flush()
            if not applied:
                pending_ai_decision = {**decision, "decision_id": row.id}
        persist_tick(db, snap, run_mode)
        events = persist_lot_events(db)
        persist_checkpoint(db)
    sim_engine.lot_events.clear()
    return {
        "type": "TICK_UPDATE", "live_streaming": live_streaming_enabled,
        "ai_control_mode": ai_control_mode, "pending_ai_decision": pending_ai_decision,
        "latest_decision": decision, "snapshot": snap, "lot_events": events,
    }


async def live_fab_loop():
    while True:
        await asyncio.sleep(2.0)
        if live_streaming_enabled and ws_manager.active_connections:
            async with operating_state():
                payload = await execute_one_tick(run_mode="LIVE")
            await ws_manager.broadcast(payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global operator_accounts
    operator_accounts = auth.accounts()
    login_attempts.clear()
    init_db()
    with SessionLocal.begin() as db:
        row = db.get(FabCheckpoint, 1)
        if row is not None:
            load_checkpoint(row.payload)
        else:
            persist_lot_events(db)
            persist_checkpoint(db)
    sim_engine.lot_events.clear()
    task = asyncio.create_task(live_fab_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="SK hynix M16 Fab Enterprise Digital Twin API",
    version="7.0.0",
    lifespan=lifespan,
)

class ToolControlRequest(BaseModel):
    tool_id: str = Field(min_length=1, max_length=64)
    feed_rate: Optional[float] = Field(default=None, ge=.2, le=1.5)
    load_ratio: Optional[float] = Field(default=None, ge=.3, le=1)
    chamber_temp_c: Optional[float] = Field(default=None, ge=20, le=450)
    rul_pct: Optional[float] = Field(default=None, ge=0, le=100)
    action: Literal["PM", "BM"] | None = None

    @model_validator(mode="after")
    def check_action(self):
        fields = self.model_dump(exclude={"tool_id", "action"}, exclude_none=True)
        if bool(self.action) == bool(fields):
            raise ValueError("Provide PM/BM or one or more settings")
        return self


class ModeSwitchRequest(BaseModel):
    live_streaming: Optional[bool] = None
    ai_control_mode: Literal["AUTO_PILOT", "HITL"] | None = None


class BatchSimRequest(BaseModel):
    ticks: int = Field(default=12, ge=1, le=96)


class BenchmarkRequest(BaseModel):
    ticks: int = Field(default=24, ge=6, le=288)
    seed: int = 2026


class BenchmarkStudyRequest(BaseModel):
    ticks: int = Field(default=96, ge=6, le=288)
    seeds: list[int] = Field(default_factory=lambda: [2026, 2027, 2028, 2029, 2030], min_length=2, max_length=20)

    @model_validator(mode="after")
    def unique_seeds(self):
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("Seeds must be distinct")
        return self


ROLE_NAMES = {
    "engineer": "공정기술 엔지니어",
    "maintenance": "설비보전 파트장",
    "manager": "Fab 생산관리 총괄",
}


def session_actor(token):
    with SessionLocal() as db:
        return auth.resolve_session(db, token, operator_accounts)


def websocket_actor(websocket):
    return session_actor(websocket.cookies.get(auth.COOKIE))


def same_origin(request):
    origin = request.headers.get("origin")
    return not origin or urlsplit(origin).netloc == request.headers.get("host")


@app.middleware("http")
async def authenticate_api(request: Request, call_next):
    request.state.actor = None
    path = request.url.path
    if operator_accounts is not None and path.startswith("/api/"):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(request):
            return JSONResponse({"detail": "Cross-origin request denied"}, status_code=403)
        if path not in {"/api/auth/status", "/api/auth/login"}:
            request.state.actor = session_actor(request.cookies.get(auth.COOKIE))
            if request.state.actor is None:
                return JSONResponse({"detail": "Login required"}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=256)


@app.get("/api/auth/status")
def auth_status():
    return {"required": operator_accounts is not None}


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    if operator_accounts is None:
        raise HTTPException(404, "Operator authentication is disabled")
    now = time.monotonic()
    with login_attempts_lock:
        for key, (_, start) in tuple(login_attempts.items()):
            if now - start >= 60:
                login_attempts.pop(key, None)
        key = request.client.host if request.client else "unknown"
        count, start = login_attempts.get(key, (0, now))
        if count >= 8:
            raise HTTPException(429, "Too many attempts")
        login_attempts[key] = (count + 1, start)
    if not auth.verify(operator_accounts, req.actor_id, req.password):
        raise HTTPException(401, "Invalid operator credentials")
    with SessionLocal.begin() as db:
        existing = request.cookies.get(auth.COOKIE)
        if existing:
            row = db.get(OperatorSession, auth.token_key(existing))
            if row is not None:
                db.delete(row)
        token = auth.issue_session(db, req.actor_id)
    role = operator_accounts[req.actor_id]["role"]
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_SECONDS, httponly=True,
                        samesite="strict", secure=os.getenv("M16_SECURE_COOKIE") == "1")
    return {"actor_id": req.actor_id, "role": ROLE_NAMES[role]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        with SessionLocal.begin() as db:
            row = db.get(OperatorSession, auth.token_key(token))
            if row is not None:
                db.delete(row)
    response.delete_cookie(auth.COOKIE)
    return {"status": "logged_out"}


def request_actor(request, actor_id, role):
    return request.state.actor or (actor_id, role)


def enforce_rbac(actor_id, role_header, action, tool, allowed_roles):
    label = unquote(role_header)
    role = ROLE_NAMES.get(label, label)
    allowed = bool(actor_id.strip()) and role in {ROLE_NAMES[key] for key in allowed_roles}
    if not allowed:
        log_audit(actor_id, role[:64], action, tool, "직무 권한이 없습니다.", False)
        raise HTTPException(403, "직무 권한이 없습니다.")
    return role


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>M16 Fab Enterprise API v7.0 Running. Visit /docs for Swagger UI.</h1>"


@app.get("/api/fab/state")
async def get_fab_state():
    db = SessionLocal()
    try:
        recent_ticks = (
            db.query(SimulationTickLog)
            .order_by(SimulationTickLog.id.desc())
            .limit(30)
            .all()
        )
        history = [
            {
                "tick": r.tick,
                "eds_yield_pct": r.eds_yield_pct,
                "oee_pct": r.oee_pct,
                "power_kw": r.power_kw,
                "unit_cost_usd": r.unit_cost_usd,
                "oht_congestion_pct": r.oht_congestion_pct,
                "q_time_violations": r.q_time_violations,
            }
            for r in reversed(recent_ticks)
        ]
        audits = db.query(AuditTrailLog).order_by(AuditTrailLog.id.desc()).limit(25).all()
        audit_list = [
            {
                "id": a.id,
                "actor_id": a.actor_id,
                "actor_role": a.actor_role,
                "action_type": a.action_type,
                "target_tool": a.target_tool,
                "detail": a.detail,
                "allowed": a.allowed,
                "created_at": a.created_at.strftime("%H:%M:%S") if a.created_at else "",
            }
            for a in audits
        ]
    finally:
        db.close()

    return {
        "live_streaming": live_streaming_enabled,
        "ai_control_mode": ai_control_mode,
        "pending_ai_decision": pending_ai_decision,
        "snapshot": sim_engine.snapshot(),
        "history": history,
        "audit_logs": audit_list,
    }


@app.post("/api/fab/mode")
async def switch_mode(
    req: ModeSwitchRequest,
    request: Request,
    x_actor_id: str = Header(default="", max_length=64),
    x_actor_role: str = Header(default="", max_length=256),
):
    global live_streaming_enabled, ai_control_mode, pending_ai_decision
    x_actor_id, x_actor_role = request_actor(request, x_actor_id, x_actor_role)
    role = enforce_rbac(x_actor_id, x_actor_role, "MODE_SWITCH", "SYSTEM", {"manager"})
    async with operating_state():
        if req.live_streaming is not None:
            live_streaming_enabled = req.live_streaming
        if req.ai_control_mode is not None:
            ai_control_mode = req.ai_control_mode
            if ai_control_mode == "AUTO_PILOT":
                pending_ai_decision = None
        payload = {"live_streaming": live_streaming_enabled, "ai_control_mode": ai_control_mode,
                   "pending_ai_decision": pending_ai_decision}
        with SessionLocal.begin() as db:
            db.add(AuditTrailLog(actor_id=x_actor_id, actor_role=role, action_type="MODE_SWITCH",
                                 target_tool="SYSTEM", detail=str(payload)))
            persist_checkpoint(db)
    await ws_manager.broadcast({"type": "MODE_UPDATE", **payload})
    return payload


class ReleaseLotRequest(BaseModel):
    wafers: int = Field(ge=1, le=25, strict=True)
    priority: Literal["NORMAL", "HOT_LOT"] = "NORMAL"


@app.get("/api/fab/lot-events")
def get_lot_events(lot_id: str | None = Query(default=None, max_length=64),
                   trace_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
                   limit: int = Query(default=120, ge=1, le=1000)):
    run_id = trace_id or sim_engine.trace_id
    with SessionLocal() as db:
        return {"trace_id": run_id, "events": lot_history(db, run_id, lot_id, limit)}


@app.post("/api/fab/lots", status_code=201)
async def release_lot(
    req: ReleaseLotRequest,
    request: Request,
    x_actor_id: str = Header(default="", max_length=64),
    x_actor_role: str = Header(default="", max_length=256),
):
    x_actor_id, x_actor_role = request_actor(request, x_actor_id, x_actor_role)
    role = enforce_rbac(x_actor_id, x_actor_role, "LOT_RELEASE", "STOCKER_A", {"manager", "engineer"})
    async with operating_state():
        if not sim_engine.can_release():
            raise HTTPException(409, "입고 스토커 가용 슬롯이 없습니다.")
        lot = sim_engine.release_lot(req.wafers, req.priority, source="MANUAL")
        with SessionLocal.begin() as db:
            events = persist_lot_events(db)
            db.add(AuditTrailLog(actor_id=x_actor_id, actor_role=role, action_type="LOT_RELEASE",
                                 target_tool="STOCKER_A", detail=f"{lot.lot_id}: {lot.wafer_count} wafers"))
            persist_checkpoint(db)
        sim_engine.lot_events.clear()
        snap = sim_engine.snapshot()
    await ws_manager.broadcast({"type": "CONTROL_UPDATE", "snapshot": snap, "lot_events": events})
    return {"trace_id": snap["trace_id"], "lot_id": lot.lot_id, "foup_id": lot.foup_id,
            "wafers": lot.wafer_count, "snapshot": snap, "lot_events": events}


@app.post("/api/fab/control")
async def control_tool(
    req: ToolControlRequest,
    request: Request,
    x_actor_id: str = Header(default="", max_length=64),
    x_actor_role: str = Header(default="", max_length=256),
):
    x_actor_id, x_actor_role = request_actor(request, x_actor_id, x_actor_role)
    allowed = {"manager"}
    tuning_fields = (req.feed_rate, req.load_ratio, req.chamber_temp_c)
    if req.action or req.rul_pct is not None:
        if not any(value is not None for value in tuning_fields):
            allowed.add("maintenance")
    else:
        allowed.add("engineer")
    role = enforce_rbac(x_actor_id, x_actor_role, req.action or "PARAM_TUNE", req.tool_id, allowed)
    if req.tool_id not in sim_engine.tools:
        raise HTTPException(404, "Unknown tool")
    async with operating_state():
        snap = sim_engine.apply_tool_control(
            tool_id=req.tool_id,
            feed_rate=req.feed_rate,
            load_ratio=req.load_ratio,
            chamber_temp_c=req.chamber_temp_c,
            rul_pct=req.rul_pct,
            action=req.action,
        )
        detail = (
            f"Action={req.action}" if req.action else
            f"Feed={req.feed_rate}, Load={req.load_ratio}, Temp={req.chamber_temp_c}, RUL={req.rul_pct}"
        )
        with SessionLocal.begin() as db:
            events = persist_lot_events(db)
            db.add(AuditTrailLog(actor_id=x_actor_id, actor_role=role, action_type=req.action or "PARAM_TUNE",
                                 target_tool=req.tool_id, detail=detail))
            persist_checkpoint(db)
        sim_engine.lot_events.clear()
    await ws_manager.broadcast({"type": "CONTROL_UPDATE", "snapshot": snap, "lot_events": events})
    return {"snapshot": snap, "lot_events": events}


@app.post("/api/fab/ai/approve")
async def approve_pending_ai(
    request: Request,
    x_actor_id: str = Header(default="", max_length=64),
    x_actor_role: str = Header(default="", max_length=256),
):
    global pending_ai_decision
    x_actor_id, x_actor_role = request_actor(request, x_actor_id, x_actor_role)
    role = enforce_rbac(x_actor_id, x_actor_role, "AI_APPROVE", "AI_COUNCIL", {"manager"})
    async with operating_state():
        if not pending_ai_decision:
            return {"status": "NO_PENDING_DECISION", "snapshot": sim_engine.snapshot()}

        decision = pending_ai_decision
        apply_decision(sim_engine, decision)
        snap = sim_engine.snapshot()
        with SessionLocal.begin() as db:
            row = db.get(AiCouncilDecisionLog, decision["decision_id"])
            row.applied = True
            row.approved_by = x_actor_id
            events = persist_lot_events(db)
            db.add(AuditTrailLog(actor_id=x_actor_id, actor_role=role, action_type="AI_APPROVE",
                                 target_tool="AI_COUNCIL", detail=decision["trigger_reason"], allowed=True))
            pending_ai_decision = None
            persist_checkpoint(db)
        sim_engine.lot_events.clear()
    await ws_manager.broadcast({"type": "CONTROL_UPDATE", "snapshot": snap,
                                "pending_ai_decision": None, "lot_events": events})
    return {"status": "APPROVED", "snapshot": snap, "lot_events": events}


@app.post("/api/fab/simulate")
async def run_batch_simulation(req: BatchSimRequest, request: Request):
    if request.state.actor is not None:
        enforce_rbac(*request.state.actor, "BATCH", "SYSTEM", {"manager", "engineer"})
    ticks = req.ticks
    snapshots = []
    decisions = []
    lot_events = []
    async with state_lock:
        for _ in range(ticks):
            async with rollback_uncommitted_state():
                res = await execute_one_tick(run_mode="BATCH")
            snapshots.append(res["snapshot"])
            lot_events.extend(res["lot_events"])
            if res["latest_decision"]:
                decisions.append(res["latest_decision"])
    await ws_manager.broadcast({"type": "CONTROL_UPDATE", "snapshot": snapshots[-1],
                                "lot_events": lot_events, "pending_ai_decision": pending_ai_decision})
    return {"ticks": ticks, "snapshots": snapshots, "decisions": decisions, "lot_events": lot_events}


@app.post("/api/fab/benchmark")
async def run_ab_benchmark(req: BenchmarkRequest):
    """
    Runs identical seed & ticks side-by-side:
    - Scenario A: Baseline (No AI closed-loop intervention)
    - Scenario B: AI Council Closed-Loop Auto-Pilot
    """
    ticks = req.ticks

    async def simulate_variant(use_ai: bool):
        sim = M16SemiconFabSimulator(seed=req.seed)
        series = []
        pm_count = 0
        due_minutes = 480
        for _ in range(ticks):
            previous_q = sim.total_q_time_violations
            snap = sim.step()
            if use_ai:
                dec = await evaluate_and_decide(snap, control_mode="AUTO_PILOT", allow_live=False,
                                               q_time_delta=snap["kpi"]["q_time_violations"] - previous_q)
                if dec:
                    pm_count += sum(act.get("action") == "PM" for act in dec["agreed_action"])
                    apply_decision(sim, dec)
                    snap = sim.snapshot(override_power_kw=snap["kpi"]["power_kw"])
            sim.lot_events.clear()
            series.append(snap)
        last = series[-1]["kpi"]
        avg_yield = round(sum(s["kpi"]["eds_yield_pct"] for s in series) / len(series), 2)
        avg_cost = round(sum(s["kpi"]["unit_cost_usd"] for s in series) / len(series), 1)
        avg_oee = round(sum(s["kpi"]["oee_pct"] for s in series) / len(series), 1)
        horizon = ticks * sim.tick_minutes
        due_lots = [
            lot for lot in sim.completed_lots + sim.wip_lots
            if lot.new_release and lot.released_minute + due_minutes <= horizon
        ]
        on_time = sum(
            lot.finished_minute is not None and lot.finished_minute <= lot.released_minute + due_minutes
            for lot in due_lots
        )
        accepted = last["accepted_wafers"]
        return {
            "summary": {
                "accepted_wafers": accepted,
                "due_lots": len(due_lots),
                "on_time_lots": on_time,
                "on_time_pct": round(100 * on_time / len(due_lots), 2) if due_lots else None,
                "cost_per_accepted_usd": round(avg_cost * last["completed_wafers"] / accepted, 2) if accepted else None,
                "avg_yield_pct": avg_yield,
                "avg_oee_pct": avg_oee,
                "avg_unit_cost_usd": avg_cost,
                "completed_wafers": last["completed_wafers"],
                "q_time_violations": last["q_time_violations"],
                "udt_breakdowns": last["udt_events"],
                "proactive_pm_count": pm_count,
            },
            "series": [
                {
                    "tick": s["tick"],
                    "yield": s["kpi"]["eds_yield_pct"],
                    "cost": s["kpi"]["unit_cost_usd"],
                    "etch_rul": s["tools"]["ETCH_01"]["rul_pct"],
                }
                for s in series
            ],
        }

    baseline = await simulate_variant(use_ai=False)
    ai_pilot = await simulate_variant(use_ai=True)
    return {"ticks": ticks, "seed": req.seed, "baseline": baseline, "ai_autopilot": ai_pilot}


@app.post("/api/fab/benchmark-study")
async def benchmark_study(req: BenchmarkStudyRequest):
    return await asyncio.to_thread(lambda: asyncio.run(collect_benchmark_study(req)))


async def collect_benchmark_study(req):
    trials = []
    for seed in req.seeds:
        trial = await run_ab_benchmark(BenchmarkRequest(ticks=req.ticks, seed=seed))
        trials.append({"seed": seed, **{variant: trial[variant]["summary"]
                                      for variant in ("baseline", "ai_autopilot")}})

    metrics = ("accepted_wafers", "on_time_pct", "cost_per_accepted_usd", "q_time_violations", "udt_breakdowns")
    paired = {}
    for metric in metrics:
        pairs = [(trial["baseline"][metric], trial["ai_autopilot"][metric]) for trial in trials]
        valid = [(base, ai) for base, ai in pairs if base is not None and ai is not None]
        differences = [ai - base for base, ai in valid]
        paired[metric] = {
            "compared_seeds": len(valid),
            "baseline_mean": round(mean(base for base, _ in valid), 2) if valid else None,
            "autopilot_mean": round(mean(ai for _, ai in valid), 2) if valid else None,
            "mean_delta": round(mean(differences), 2) if differences else None,
            "min_delta": round(min(differences), 2) if differences else None,
            "max_delta": round(max(differences), 2) if differences else None,
        }
    return {"ticks": req.ticks, "due_minutes": 480, "trials": trials, "paired": paired}


@app.get("/api/reports/export.csv")
async def export_csv():
    db = SessionLocal()
    try:
        rows = db.query(SimulationTickLog).order_by(SimulationTickLog.id.desc()).limit(100).all()
    finally:
        db.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Tick", "Mode", "EDS_Yield(%)", "OEE(%)", "Power(kW)", "UnitCost($)", "WIP_FOUPs", "Completed_Wafers", "QTime_Violations", "OHT_Congestion(%)"])
    for r in reversed(rows):
        writer.writerow([
            r.tick, r.run_mode, r.eds_yield_pct, r.oee_pct, r.power_kw,
            r.unit_cost_usd, r.wip_foup_count, r.completed_wafers, r.q_time_violations, r.oht_congestion_pct
        ])
    buf.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=M16_Fab_Enterprise_Telemetry.csv"},
    )


@app.websocket("/ws/fab/live")
async def websocket_fab_live(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    if operator_accounts is not None and not websocket_actor(websocket):
        await websocket.close(code=1008)
        return
    await ws_manager.connect(websocket)
    try:
        await websocket.send_json({
            "type": "INIT",
            "live_streaming": live_streaming_enabled,
            "ai_control_mode": ai_control_mode,
            "pending_ai_decision": pending_ai_decision,
            "snapshot": sim_engine.snapshot(),
        })
        while True:
            msg = await websocket.receive_text()
            if msg == "STEP_NOW":
                if operator_accounts is not None:
                    actor = websocket_actor(websocket)
                    if not actor:
                        await websocket.close(code=1008)
                        return
                    try:
                        enforce_rbac(*actor, "STEP_NOW", "SYSTEM", {"manager", "engineer"})
                    except HTTPException:
                        await websocket.send_json({"type": "ERROR", "detail": "직무 권한이 없습니다."})
                        continue
                async with operating_state():
                    payload = await execute_one_tick(run_mode="LIVE")
                await ws_manager.broadcast(payload)
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket)
