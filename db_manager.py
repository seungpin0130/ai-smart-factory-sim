"""SQLite journal and web replay export for the v3 simulation."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from storage import write_json

DB_PATH = Path("data/m16_fab_history.db")
REPLAY_PATH = Path("web/replay_history.json")


def init_db(db_path=DB_PATH):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry_snapshots (
                tick_id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                eds_yield REAL NOT NULL,
                power_kw REAL NOT NULL,
                unit_cost REAL,
                wip_buffers TEXT NOT NULL,
                equipment_states TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oht_telemetry (
                tick_id INTEGER PRIMARY KEY REFERENCES telemetry_snapshots(tick_id),
                metrics TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_decisions (
                decision_id INTEGER PRIMARY KEY,
                tick_id INTEGER NOT NULL UNIQUE REFERENCES telemetry_snapshots(tick_id),
                trigger_reason TEXT,
                debate_transcript TEXT NOT NULL,
                final_action TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS maintenance_logs (
                log_id INTEGER PRIMARY KEY,
                tick_id INTEGER NOT NULL REFERENCES telemetry_snapshots(tick_id),
                equip_id TEXT NOT NULL,
                maint_type TEXT NOT NULL CHECK (maint_type IN ('PM', 'BM')),
                rul_before REAL NOT NULL,
                downtime_ticks INTEGER NOT NULL,
                cost_impact REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS maintenance_by_tick ON maintenance_logs(tick_id);
        """)


def load_tick(tick_id, db_path=DB_PATH):
    if not Path(db_path).is_file():
        return None
    with closing(sqlite3.connect(db_path)) as db:
        row = db.execute(
            "SELECT wip_buffers, equipment_states FROM telemetry_snapshots WHERE tick_id = ?",
            (tick_id,),
        ).fetchone()
    if row:
        return {"wip_buffers": json.loads(row[0]), "equipments": json.loads(row[1])}
    return None


def save_tick(state, fab, record, maintenance, db_path=DB_PATH):
    """Replace one tick and its children together, including on retry."""
    init_db(db_path)
    tick = state.cycle
    power_kw = round(state.power_kwh / 0.5, 2)
    maintenance_cost = sum(event[-1] for event in maintenance)
    unit_cost = None
    if state.wafer_output:
        unit_cost = round((state.power_kwh * 0.12 + 45 + maintenance_cost) / state.wafer_output, 2)

    def encode(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("""
            INSERT INTO telemetry_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tick_id) DO UPDATE SET timestamp=excluded.timestamp,
                eds_yield=excluded.eds_yield, power_kw=excluded.power_kw, unit_cost=excluded.unit_cost,
                wip_buffers=excluded.wip_buffers, equipment_states=excluded.equipment_states
        """, (tick, state.timestamp, state.eds_yield, power_kw, unit_cost,
              encode(fab["wip_buffers"]), encode(fab["equipments"])))
        db.execute("""
            INSERT INTO ai_decisions (tick_id, trigger_reason, debate_transcript, final_action)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tick_id) DO UPDATE SET trigger_reason=excluded.trigger_reason,
                debate_transcript=excluded.debate_transcript, final_action=excluded.final_action
        """, (tick, record["trigger_reason"], encode(record["debate_transcript"]),
              json.dumps(record["final_action"], ensure_ascii=False, indent=2, allow_nan=False)))
        if "oht_metrics" in fab:
            db.execute("""
                INSERT INTO oht_telemetry (tick_id, metrics) VALUES (?, ?)
                ON CONFLICT(tick_id) DO UPDATE SET metrics=excluded.metrics
            """, (tick, encode(fab["oht_metrics"])))
        else:
            db.execute("DELETE FROM oht_telemetry WHERE tick_id = ?", (tick,))
        db.execute("DELETE FROM maintenance_logs WHERE tick_id = ?", (tick,))
        db.executemany("""
            INSERT INTO maintenance_logs (tick_id, equip_id, maint_type, rul_before, downtime_ticks, cost_impact)
            VALUES (?, ?, ?, ?, ?, ?)
        """, maintenance)


def read_replay(db_path=DB_PATH, limit=200, *, through_tick=None):
    """Read existing snapshots without initializing or updating the journal."""
    if not Path(db_path).is_file():
        return []
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "telemetry_snapshots" not in tables:
            return []
        oht_column = "o.metrics" if "oht_telemetry" in tables else "NULL"
        oht_join = "LEFT JOIN oht_telemetry o USING (tick_id)" if "oht_telemetry" in tables else ""
        rows = db.execute(f"""
            SELECT t.*, a.trigger_reason, a.debate_transcript, a.final_action, {oht_column} AS oht_metrics
            FROM telemetry_snapshots t LEFT JOIN ai_decisions a USING (tick_id)
            {oht_join}
            WHERE (? IS NULL OR t.tick_id <= ?)
            ORDER BY t.tick_id DESC LIMIT ?
        """, (through_tick, through_tick, min(200, max(0, limit)))).fetchall()
    return [dict(row) for row in reversed(rows)]


def export_replay_json(limit=200, output_path=REPLAY_PATH, db_path=DB_PATH, *, through_tick=None):
    """Export the most recent ticks in playback order."""
    write_json(Path(output_path), read_replay(db_path, limit, through_tick=through_tick))
