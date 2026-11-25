import csv
import io
import copy
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from uuid import uuid4
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.models import SessionLocal, init_db, SimulationRun, TickTelemetry, AlarmAuditLog

# DB Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# 1. 실시간 Fab 상태 및 물리 연산 엔진
# ==========================================
OPT_TEMPS = {"PHOTO_EUV": 22.0, "ETCH": 64.0, "HBM_TSV": 42.0}

CURRENT_FAB_STATE = {
    "tick": 103,
    "timestamp": "2026-11-25 14:30",
    "kpi": {"eds_yield": 98.48, "power_kw": 1210, "unit_cost": 1160},
    "wip_buffers": {"PHOTO_EUV": 48, "ETCH": 35, "HBM_TSV": 50},
    "equipments": {
        "PHOTO_EUV": {"status": "RUNNING", "rul": 82.5, "chamber_temp": 22.0, "load_ratio": 0.65, "feed_rate": 1.0},
        "ETCH":      {"status": "MAINTENANCE", "maint_type": "PM", "maint_remain": 1, "rul": 100.0, "chamber_temp": 30.0, "load_ratio": 0.0, "feed_rate": 0.0},
        "HBM_TSV":   {"status": "RUNNING", "rul": 84.1, "chamber_temp": 41.2, "load_ratio": 0.75, "feed_rate": 1.0}
    },
    "trigger_reason": "PM_EXECUTED:ETCH",
    "debate": None,
    "final_action": {"status": "PM 완료 및 정상 라인 밸런싱 복귀"}
}


def run_dual_ai_council(station_id: str, reason: str, state: dict) -> dict:
    """임계치 초과 시 ChatGPT + Gemini 위원회 의사결정 (규칙 기반 모의 응답)"""
    eq = state["equipments"][station_id]
    debate = [
        {
            "speaker": "ChatGPT 역할 (모의)",
            "turn": 1,
            "text": f"{station_id} 설비에서 '{reason}' 이벤트가 감지되었습니다. 상류 투입 속도를 조절해 WIP 버퍼 적체를 방지해야 합니다."
        },
        {
            "speaker": "Gemini 역할 (모의)",
            "turn": 2,
            "text": f"현재 챔버 온도 {eq['chamber_temp']}°C, RUL {eq['rul']}% 수준입니다. 돌발고장(BM) 손실 방지를 위해 선제적 정비를 권고합니다."
        }
    ]
    action = {"target_station": station_id, "recommended_feed_rate": 0.5, "schedule_pm": eq["rul"] < 25.0}
    return {"debate": debate, "final_action": action}


def step_fab_physics(state: dict) -> dict:
    pipeline = ["PHOTO_EUV", "ETCH", "HBM_TSV"]
    power = 430.0
    load_sum = 0.0

    for idx, eq_id in enumerate(pipeline):
        eq = state["equipments"][eq_id]
        if eq["status"] == "MAINTENANCE":
            eq["maint_remain"] = eq.get("maint_remain", 2) - 1
            if eq["maint_remain"] <= 0:
                eq["status"] = "RUNNING"
                eq["maint_type"] = None
                eq["rul"] = 100.0
                eq["load_ratio"] = 0.78
                eq["feed_rate"] = 1.0
            power += 90.0
            continue

        in_flow = 12.0 * eq["feed_rate"] if idx == 0 else min(14.0, state["wip_buffers"][pipeline[idx - 1]] * 0.25)
        out_flow = 11.0 * eq["load_ratio"]
        state["wip_buffers"][eq_id] = int(min(100, max(10, round(state["wip_buffers"][eq_id] + in_flow - out_flow))))

        eq["chamber_temp"] = round(eq["chamber_temp"] + (eq["load_ratio"] - 0.75) * 2.1, 1)
        temp_stress = max(0.0, (eq["chamber_temp"] - OPT_TEMPS[eq_id]) * 0.14)
        deg = 0.75 + eq["load_ratio"] * 1.15 + temp_stress
        eq["rul"] = max(0.0, round(eq["rul"] - deg, 1))

        if eq["rul"] == 0.0:
            eq["status"] = "MAINTENANCE"
            eq["maint_type"] = "BM"
            eq["maint_remain"] = 6
            eq["load_ratio"] = 0.0
            state["kpi"]["unit_cost"] += 340
            state["kpi"]["eds_yield"] = max(91.0, round(state["kpi"]["eds_yield"] - 1.1, 2))

        power += 375.0 * eq["load_ratio"] + max(0.0, eq["chamber_temp"] - 40.0) * 2.4
        load_sum += eq["load_ratio"]

    state["tick"] += 1
    state["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["kpi"]["power_kw"] = round(power)
    state["kpi"]["eds_yield"] = round(min(99.4, max(91.0, state["kpi"]["eds_yield"] + (0.05 if load_sum > 1.8 else -0.09))), 2)
    state["kpi"]["unit_cost"] = round(1120 + (power / 15.0) + (100.0 - state["kpi"]["eds_yield"]) * 34.0)

    return copy.deepcopy(state)


# ==========================================
# 2. WebSocket Connection Manager
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for conn in list(self.active_connections):
            try:
                await conn.send_json(message)
            except Exception:
                self.disconnect(conn)


ws_manager = ConnectionManager()


def on_startup():
    init_db()
    with SessionLocal() as db:
        latest = db.query(TickTelemetry).order_by(TickTelemetry.id.desc()).first()
        if latest:
            CURRENT_FAB_STATE.update(
                tick=latest.tick,
                timestamp=latest.timestamp_label,
                kpi={"eds_yield": latest.eds_yield, "power_kw": latest.power_kw, "unit_cost": latest.unit_cost},
                wip_buffers=latest.wip_buffers,
                equipments=latest.equipments,
                trigger_reason=latest.trigger_reason,
                debate=latest.debate,
                final_action=latest.final_action,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    on_startup()
    yield


app = FastAPI(
    title="SK hynix M16 Fab Digital Twin MES API",
    description="교육용 로컬 시뮬레이터. 로그인과 AI 위원회는 모의 모드로 동작합니다.",
    version="6.0.0",
    lifespan=lifespan,
)

# ==========================================
# 3. REST API ENDPOINTS
# ==========================================
class LoginRequest(BaseModel):
    emp_id: str
    password: str = Field(repr=False, json_schema_extra={"writeOnly": True, "format": "password"})
    role: str


class StationControlRequest(BaseModel):
    feed_rate: Optional[float] = Field(default=None, ge=0.2, le=1.5)
    load_ratio: Optional[float] = Field(default=None, ge=0.3, le=1.0)
    chamber_temp: Optional[float] = Field(default=None, ge=20, le=90)
    rul: Optional[float] = Field(default=None, ge=5, le=100)
    action: Literal["PM", "BM"] | None = None


class BatchSimRequest(BaseModel):
    ticks: int = Field(default=12, ge=1, le=48)
    operator_id: str = "M16-ENG-2048"


@app.post("/api/auth/login")
def login(req: LoginRequest):
    return {
        "access_token": f"demo-{uuid4().hex}",
        "token_type": "bearer",
        "mode": "demo",
        "user": {"emp_id": req.emp_id, "role": req.role}
    }


@app.get("/api/dashboard/summary")
def get_dashboard_summary(db: Session = Depends(get_db)):
    alarms = db.query(AlarmAuditLog).order_by(AlarmAuditLog.id.desc()).limit(10).all()
    return {
        "state": copy.deepcopy(CURRENT_FAB_STATE),
        "recent_alarms": [
            {
                "id": a.alarm_code,
                "tick": a.tick,
                "time": a.time_label,
                "station": a.station_id,
                "type": a.alarm_type,
                "desc": a.description,
                "ack": a.acknowledged
            } for a in alarms
        ]
    }


@app.patch("/api/stations/{station_id}/control")
async def control_station(station_id: str, req: StationControlRequest, db: Session = Depends(get_db)):
    if station_id not in CURRENT_FAB_STATE["equipments"]:
        raise HTTPException(status_code=404, detail="Unknown station_id")

    state = copy.deepcopy(CURRENT_FAB_STATE)
    eq = state["equipments"][station_id]
    state["debate"] = None
    state["final_action"] = None

    if req.action == "PM":
        eq["status"] = "MAINTENANCE"
        eq["maint_type"] = "PM"
        eq["maint_remain"] = 2
        eq["rul"] = 100.0
        eq["load_ratio"] = 0.0
        state["kpi"]["unit_cost"] += 95
        state["trigger_reason"] = f"MANUAL_PM:{station_id}"
        alarm_type, desc = "PM", f"{station_id} 선제적 예방정비(PM) 투입 — RUL 100% 복원"
    elif req.action == "BM":
        eq["status"] = "MAINTENANCE"
        eq["maint_type"] = "BM"
        eq["maint_remain"] = 6
        eq["rul"] = 0.0
        eq["load_ratio"] = 0.0
        state["kpi"]["eds_yield"] = max(90.0, round(state["kpi"]["eds_yield"] - 1.4, 2))
        state["kpi"]["unit_cost"] += 380
        state["trigger_reason"] = f"INJECTED_BM:{station_id}"
        alarm_type, desc = "CRITICAL", f"{station_id} 돌발고장(BM) 발생 — 긴급 보전팀 호출"
    else:
        if req.feed_rate is not None:
            eq["feed_rate"] = req.feed_rate
        if req.load_ratio is not None:
            eq["load_ratio"] = req.load_ratio
        if req.chamber_temp is not None:
            eq["chamber_temp"] = req.chamber_temp
        if req.rul is not None:
            eq["rul"] = req.rul
            if eq["rul"] > 0 and eq["status"] == "MAINTENANCE":
                eq["status"] = "RUNNING"
        state["trigger_reason"] = f"TUNED:{station_id}"
        alarm_type, desc = "TUNED", f"{station_id} 파라미터 수동 조정 반영"

    # 임계치 초과 시 듀얼 AI 위원회 호출
    if eq["rul"] < 25.0 or req.action == "BM":
        ai_res = run_dual_ai_council(station_id, state["trigger_reason"], state)
        state["debate"] = ai_res["debate"]
        state["final_action"] = ai_res["final_action"]

    # DB 감사 로그 저장
    alarm_record = AlarmAuditLog(
        alarm_code=f"ALM-{uuid4().hex[:28]}",
        tick=state["tick"],
        time_label=datetime.now().strftime("%H:%M:%S"),
        station_id=station_id,
        alarm_type=alarm_type,
        description=desc,
        acknowledged=(alarm_type != "CRITICAL")
    )
    db.add(alarm_record)
    db.commit()

    CURRENT_FAB_STATE.update(state)
    await ws_manager.broadcast({"event": "STATE_UPDATED", "state": state})
    return {"status": "ok", "state": state}


@app.post("/api/simulations/batch")
async def run_batch_simulation(req: BatchSimRequest, db: Session = Depends(get_db)):
    run_code = f"run_{uuid4().hex}"
    state = copy.deepcopy(CURRENT_FAB_STATE)
    start_tick = CURRENT_FAB_STATE["tick"] + 1
    end_tick = CURRENT_FAB_STATE["tick"] + req.ticks

    sim_run = SimulationRun(
        run_code=run_code,
        name=f"Batch Run Tick #{start_tick}~#{end_tick} ({req.ticks}틱)",
        operator_id=req.operator_id,
        total_ticks=req.ticks
    )
    db.add(sim_run)
    db.flush()

    snapshots = []
    for _ in range(req.ticks):
        state.update(trigger_reason=None, debate=None, final_action=None)
        snap = step_fab_physics(state)
        snapshots.append(snap)
        db.add(TickTelemetry(
            run_id=sim_run.id,
            tick=snap["tick"],
            timestamp_label=snap["timestamp"],
            eds_yield=snap["kpi"]["eds_yield"],
            power_kw=snap["kpi"]["power_kw"],
            unit_cost=snap["kpi"]["unit_cost"],
            wip_buffers=snap["wip_buffers"],
            equipments=snap["equipments"],
            trigger_reason=snap.get("trigger_reason"),
            debate=snap.get("debate"),
            final_action=snap.get("final_action")
        ))

    db.commit()
    CURRENT_FAB_STATE.update(state)
    await ws_manager.broadcast({"event": "BATCH_COMPLETED", "run_code": run_code, "state": state})
    return {"run_code": run_code, "name": sim_run.name, "snapshots": snapshots}


@app.patch("/api/alarms/{alarm_code}/ack")
def acknowledge_alarm(alarm_code: str, operator_id: str = "M16-ENG-2048", db: Session = Depends(get_db)):
    alarm = db.query(AlarmAuditLog).filter(AlarmAuditLog.alarm_code == alarm_code).first()
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")
    alarm.acknowledged = True
    alarm.acknowledged_by = operator_id
    db.commit()
    return {"status": "acknowledged", "alarm_code": alarm_code}


@app.get("/api/reports/{run_code}/export.csv")
def export_report_csv(run_code: str, db: Session = Depends(get_db)):
    run = db.query(SimulationRun).filter(SimulationRun.run_code == run_code).first()
    if not run:
        raise HTTPException(status_code=404, detail="Simulation run not found")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Tick", "Timestamp", "Yield(%)", "Power(kW)", "UnitCost($)", "EUV_RUL", "ETCH_RUL", "TSV_RUL"])
    for t in sorted(run.telemetries, key=lambda snapshot: snapshot.tick):
        writer.writerow([
            t.tick, t.timestamp_label, t.eds_yield, t.power_kw, t.unit_cost,
            t.equipments["PHOTO_EUV"]["rul"], t.equipments["ETCH"]["rul"], t.equipments["HBM_TSV"]["rul"]
        ])

    output.seek(0)
    return StreamingResponse(
        iter(["\ufeff" + output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=M16_Fab_{run_code}.csv"}
    )


@app.websocket("/ws/fab")
async def websocket_fab_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        await websocket.send_json({"event": "CONNECTED", "state": copy.deepcopy(CURRENT_FAB_STATE)})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(websocket)