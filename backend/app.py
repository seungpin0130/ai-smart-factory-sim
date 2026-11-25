"""Local v6 dashboard API backed by the fab replay journal and isolated scenarios."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field, model_validator

from backend.semicon import PROCESS_ROUTE, STATION_TO_TOOL, dashboard_snapshot, next_timestamp, set_rul, start_engine
from db_manager import DB_PATH, read_replay
from m16_semicon_fab_sim import M16SemiconFabSimulator, SemiE10State
from models import FactoryState, StrictModel
from oht_logistics import CARRIER_POWER_KW, simulate_oht_logistics
from storage import read_json

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = "M16_Fab_Admin_Dashboard_v6.html"


class ScenarioRequest(StrictModel):
    base_tick: int | None = Field(default=None, ge=0)
    scenario_id: UUID | None = Field(default=None, strict=False)


class ControlRequest(ScenarioRequest):
    station_id: Literal["PHOTO_EUV", "ETCH", "HBM_TSV"]
    feed_rate: float | None = Field(default=None, ge=0.2, le=1.5)
    load_ratio: float | None = Field(default=None, ge=0.3, le=1)
    chamber_temp: float | None = Field(default=None, ge=20, le=90)
    rul: float | None = Field(default=None, ge=5, le=100)
    action: Literal["PM", "BM"] | None = None

    @model_validator(mode="after")
    def check_control(self):
        edited = any(value is not None for value in (self.feed_rate, self.load_ratio, self.chamber_temp, self.rul))
        if edited == bool(self.action):
            raise ValueError("Provide either station settings or a PM/BM action")
        return self


class SimulateRequest(ScenarioRequest):
    ticks: Literal[6, 12, 24, 48]


def replay_snapshot(row):
    wip = json.loads(row["wip_buffers"])
    equipment = json.loads(row["equipment_states"])
    oht = json.loads(row["oht_metrics"]) if row["oht_metrics"] else simulate_oht_logistics(wip, equipment)
    for route in oht["routes"].values():
        route.setdefault("oht_power_kw", round(route["active_carriers"] * CARRIER_POWER_KW, 1))
    return {
        "tick": row["tick_id"],
        "timestamp": row["timestamp"],
        "kpi": {"eds_yield": row["eds_yield"], "power_kw": row["power_kw"], "unit_cost": row["unit_cost"]},
        "wip_buffers": wip,
        "equipments": equipment,
        "trigger_reason": row["trigger_reason"],
        "debate": json.loads(row["debate_transcript"] or "null"),
        "final_action": json.loads(row["final_action"] or "null"),
        "oht_metrics": oht,
    }


def open_scenarios(path):
    db = sqlite3.connect(path, timeout=15)
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS fab_scenarios (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fab_scenario_runs (
                id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL REFERENCES fab_scenarios(id),
                snapshots TEXT NOT NULL, maintenance TEXT NOT NULL
            );
        """)
    except Exception:
        db.close()
        raise
    return db


def save_scenario(db, scenario_id, sim, view):
    payload = json.dumps({"engine": sim.checkpoint(), "view": view}, allow_nan=False)
    db.execute("""
        INSERT INTO fab_scenarios (id, payload) VALUES (?, ?)
        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload
    """, (scenario_id, payload))


def find_scenario(db, scenario_id):
    row = db.execute("SELECT payload FROM fab_scenarios WHERE id = ?", (str(scenario_id),)).fetchone()
    if row is None:
        raise HTTPException(404, "Scenario not found")
    return json.loads(row[0])


def load_scenario(db, request, data_dir):
    if request.scenario_id is not None:
        data = find_scenario(db, request.scenario_id)
        scenario_id = str(request.scenario_id)
        if "engine" in data:
            return scenario_id, M16SemiconFabSimulator.restore(data["engine"]), data["view"]["timestamp"]
        # Upgrade legacy scenario settings without editing saved legacy reports.
        state = FactoryState.model_validate(data["state"])
        return scenario_id, start_engine(state.cycle, data["fab"]["equipments"], True), state.timestamp

    state = FactoryState.model_validate(read_json(data_dir / "state.json"))
    rows = read_replay(data_dir / DB_PATH, limit=1, through_tick=request.base_tick)
    if request.base_tick is not None and not any(row["tick_id"] == request.base_tick for row in rows):
        if request.base_tick != state.cycle:
            raise HTTPException(404, "Replay tick not found")
        rows = []
    if not rows:
        return str(uuid4()), start_engine(state.cycle, {}), state.timestamp
    base = replay_snapshot(rows[-1])
    return str(uuid4()), start_engine(base["tick"], base["equipments"], True), base["timestamp"]


def create_app(data_dir: Path = ROOT, scenario_db: Path | None = None):
    data_dir = Path(data_dir)
    scenario_db = Path(scenario_db or data_dir / "m16_fab.db")
    if scenario_db.resolve() == (data_dir / DB_PATH).resolve():
        raise ValueError("The scenario DB must be separate from the replay journal")
    api = FastAPI(title="M16 Fab v6 local simulation")

    @api.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(ROOT / DASHBOARD, media_type="text/html")

    @api.get("/api/fab/state")
    def get_state(scenario_id: UUID | None = None):
        if scenario_id is not None:
            if not scenario_db.is_file():
                raise HTTPException(404, "Scenario not found")
            with closing(open_scenarios(scenario_db)) as db:
                data = find_scenario(db, scenario_id)
                if "view" not in data:
                    state = FactoryState.model_validate(data["state"])
                    sim = start_engine(state.cycle, data["fab"]["equipments"], True)
                    view = dashboard_snapshot(sim, sim.snapshot(), state.timestamp)
                else:
                    view = data["view"]
                rows = db.execute(
                    "SELECT snapshots FROM fab_scenario_runs WHERE scenario_id = ? ORDER BY rowid DESC LIMIT 1",
                    (str(scenario_id),),
                ).fetchone()
            return {"state": view, "history": json.loads(rows[0]) if rows else [view], "process_route": PROCESS_ROUTE}
        history = [replay_snapshot(row) for row in read_replay(data_dir / DB_PATH)]
        if not history:
            state = FactoryState.model_validate(read_json(data_dir / "state.json"))
            sim = start_engine(state.cycle, {})
            history = [dashboard_snapshot(sim, sim.snapshot(), state.timestamp)]
        return {"state": history[-1], "history": history, "process_route": PROCESS_ROUTE}

    @api.post("/api/fab/control")
    def control(request: ControlRequest):
        with closing(open_scenarios(scenario_db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            scenario_id, sim, timestamp = load_scenario(db, request, data_dir)
            tool = sim.tools[STATION_TO_TOOL[request.station_id]]
            if tool.state in (SemiE10State.SDT, SemiE10State.UDT):
                raise HTTPException(409, "Station is undergoing maintenance")
            if request.action:
                is_pm = request.action == "PM"
                sim.start_maintenance(tool, request.action, 30 if is_pm else 90,
                                      "MANUAL_PM" if is_pm else "INJECTED_BM")
                if not is_pm:
                    tool.rf_on_hours = tool.max_rf_hours
            else:
                if request.feed_rate is not None:
                    tool.feed_rate = request.feed_rate
                if request.load_ratio is not None:
                    tool.load_ratio = request.load_ratio
                if request.chamber_temp is not None:
                    tool.temperature_setpoint_c = tool.chamber_temp_c = request.chamber_temp
                if request.rul is not None:
                    set_rul(tool, request.rul)
            details = request.model_dump(exclude_none=True, exclude={"base_tick", "scenario_id"})
            view = dashboard_snapshot(sim, sim.snapshot(), timestamp,
                                      trigger=f"{request.action or 'TUNED'}:{request.station_id}", action=details)
            save_scenario(db, scenario_id, sim, view)
            return {"scenario_id": scenario_id, "state": view}

    @api.post("/api/fab/simulate")
    def batch(request: SimulateRequest):
        with closing(open_scenarios(scenario_db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            scenario_id, sim, timestamp = load_scenario(db, request, data_dir)
            snapshots, maintenance = [], []
            for _ in range(request.ticks):
                previously_violated = sim.q_time_violations
                observation = sim.step()
                timestamp = next_timestamp(timestamp, sim.tick_minutes)
                events, sim.maintenance_events = sim.maintenance_events, []
                triggers = [f"{event[2]}:{event[1]}" for event in events]
                if sim.q_time_violations > previously_violated:
                    triggers.append("Q_TIME_VIOLATION")
                if observation["amhs_oht"]["congestion_pct"] > 85:
                    triggers.append("OHT_CONGESTION")
                snapshots.append(dashboard_snapshot(sim, observation, timestamp,
                                                     trigger=" & ".join(triggers) or None, maintenance=events))
                maintenance.extend(events)

            run_id = str(uuid4())
            save_scenario(db, scenario_id, sim, snapshots[-1])
            db.execute("INSERT INTO fab_scenario_runs VALUES (?, ?, ?, ?)",
                       (run_id, scenario_id, json.dumps(snapshots), json.dumps(maintenance)))
            return {"scenario_id": scenario_id, "run_id": run_id, "state": snapshots[-1], "snapshots": snapshots}

    return api


app = create_app()
