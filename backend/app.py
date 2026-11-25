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

from ai_council import deliberate
from db_manager import DB_PATH, read_replay
from factory_sim import TICK_HOURS, initialize_equipment, simulate
from models import FactoryConfig, FactoryState, StrictModel
from oht_logistics import CARRIER_POWER_KW, simulate_oht_logistics
from pdm_wip_engine import execute_preventive_maintenance
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


def serialize_state(state, fab, *, reason=None, debate=None, action=None, unit_cost=None, oht=None):
    return {
        "tick": state.cycle,
        "timestamp": state.timestamp,
        "wafer_output": state.wafer_output,
        "kpi": {"eds_yield": state.eds_yield, "power_kw": round(state.power_kwh / TICK_HOURS, 2),
                "unit_cost": unit_cost},
        "wip_buffers": fab["wip_buffers"],
        "equipments": fab["equipments"],
        "trigger_reason": reason,
        "debate": debate,
        "final_action": action,
        "oht_metrics": oht if oht is not None else simulate_oht_logistics(fab["wip_buffers"], fab["equipments"]),
    }


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


def save_scenario(db, scenario_id, state, fab, config):
    payload = json.dumps({"state": state.model_dump(), "fab": fab, "config": config.model_dump()}, allow_nan=False)
    db.execute("""
        INSERT INTO fab_scenarios (id, payload) VALUES (?, ?)
        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload
    """, (scenario_id, payload))


def load_scenario(db, request, data_dir):
    if request.scenario_id is not None:
        scenario_id = str(request.scenario_id)
        row = db.execute("SELECT payload FROM fab_scenarios WHERE id = ?", (scenario_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Scenario not found")
        data = json.loads(row[0])
        return scenario_id, FactoryState.model_validate(data["state"]), data["fab"], FactoryConfig.model_validate(data["config"])

    state = FactoryState.model_validate(read_json(data_dir / "state.json"))
    config = FactoryConfig.model_validate(read_json(data_dir / "config.json"))
    rows = read_replay(data_dir / DB_PATH, limit=1, through_tick=request.base_tick)
    if request.base_tick is not None and not any(row["tick_id"] == request.base_tick for row in rows):
        if request.base_tick != state.cycle:
            raise HTTPException(404, "Replay tick not found")
        rows = []
    if not rows:
        return str(uuid4()), state, initialize_equipment(state), config

    snapshot = replay_snapshot(rows[-1])
    log = data_dir / f"logs/cycles/{snapshot['tick']:08d}.json"
    if log.is_file():
        record = read_json(log)
        state = FactoryState.model_validate(record["state"])
        config = FactoryConfig.model_validate(record["applied_config"])
    fab = {"wip_buffers": snapshot["wip_buffers"], "equipments": snapshot["equipments"]}
    state = state.model_copy(update={
        "cycle": snapshot["tick"], "timestamp": snapshot["timestamp"],
        "eds_yield": snapshot["kpi"]["eds_yield"], "power_kwh": snapshot["kpi"]["power_kw"] * TICK_HOURS,
        "chamber_temp": fab["equipments"]["ETCH"]["chamber_temp"], "hbm_wip_buffer": fab["wip_buffers"]["HBM_TSV"],
    })
    return str(uuid4()), state, fab, config


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
    def get_state():
        history = [replay_snapshot(row) for row in read_replay(data_dir / DB_PATH)]
        if not history:
            state = FactoryState.model_validate(read_json(data_dir / "state.json"))
            history = [serialize_state(state, initialize_equipment(state))]
        return {"state": history[-1], "history": history}

    @api.post("/api/fab/control")
    def control(request: ControlRequest):
        with closing(open_scenarios(scenario_db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            scenario_id, state, fab, config = load_scenario(db, request, data_dir)
            station = request.station_id
            eq = fab["equipments"][station]
            if eq["status"] == "MAINTENANCE":
                raise HTTPException(409, "Station is undergoing maintenance")
            if request.action == "PM":
                events = []
                execute_preventive_maintenance(fab["equipments"], station, events, state.cycle)
                eq["load_ratio"] = 0.0
                fab.setdefault("pending_maintenance", []).extend(events)
            elif request.action == "BM":
                fab.setdefault("pending_maintenance", []).append((state.cycle, station, "BM", eq["rul"], 6, 4500.0))
                eq.update(status="MAINTENANCE", maint_type="BM", maint_remain=6, rul=0.0, load_ratio=0.0)
            else:
                for field, target in (("feed_rate", "target_feed_rate"), ("load_ratio", "target_load_ratio"),
                                      ("chamber_temp", "target_temp"), ("rul", None)):
                    value = getattr(request, field)
                    if value is not None:
                        eq[field] = value
                        if target:
                            eq[target] = value
                if station == "ETCH" and request.chamber_temp is not None:
                    state = state.model_copy(update={"chamber_temp": request.chamber_temp})
            save_scenario(db, scenario_id, state, fab, config)
            details = request.model_dump(exclude_none=True, exclude={"base_tick", "scenario_id"})
            return {"scenario_id": scenario_id, "state": serialize_state(
                state, fab, reason=f"{request.action or 'TUNED'}:{station}", action=details)}

    @api.post("/api/fab/simulate")
    def batch(request: SimulateRequest):
        with closing(open_scenarios(scenario_db)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            scenario_id, state, fab, config = load_scenario(db, request, data_dir)
            snapshots, maintenance = [], []
            for _ in range(request.ticks):
                events = fab.pop("pending_maintenance", [])
                triggers = []
                state, consumed = simulate(state, config, event="none", fab=fab, maintenance=events, triggers=triggers)
                record = deliberate(state, consumed, mode="mock", fab=fab, triggers=triggers)
                action = record["final_action"]
                for station, eq in fab["equipments"].items():
                    if action[f"{station}_action"] == "SCHEDULE_PM_NOW":
                        execute_preventive_maintenance(fab["equipments"], station, events, state.cycle)
                    eq["feed_rate"] = action.get(f"{station}_feed_rate", eq["feed_rate"])
                config = FactoryConfig.model_validate(record["applied_config"])
                unit_cost = (round((state.power_kwh * .12 + 45 + sum(e[-1] for e in events)) / state.wafer_output, 2)
                             if state.wafer_output else None)
                snapshot = serialize_state(state, fab, reason=record["trigger_reason"], debate=record["debate_transcript"],
                                           action=action, unit_cost=unit_cost, oht=fab["oht_metrics"])
                # The scenario is mutated on the following tick. Freeze this tick now.
                snapshots.append(json.loads(json.dumps(snapshot, allow_nan=False)))
                maintenance.extend(events)

            run_id = str(uuid4())
            save_scenario(db, scenario_id, state, fab, config)
            db.execute("INSERT INTO fab_scenario_runs VALUES (?, ?, ?, ?)",
                       (run_id, scenario_id, json.dumps(snapshots), json.dumps(maintenance)))
            return {"scenario_id": scenario_id, "run_id": run_id, "state": snapshots[-1], "snapshots": snapshots}

    return api


app = create_app()
