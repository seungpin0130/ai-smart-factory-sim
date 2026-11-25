"""Preferred entry point: simulate, deliberate, persist a coherent dashboard."""
import argparse
import os
import random
from pathlib import Path
from ai_council import deliberate
from db_manager import DB_PATH, REPLAY_PATH, export_replay_json, load_tick, save_tick
from factory_sim import initialize_equipment, simulate
from pdm_wip_engine import execute_preventive_maintenance
from models import FactoryConfig, FactoryState
from storage import read_json, write_json


def run(data_dir: Path, mode="mock", seed=None, provider=None, event="auto", force=False,
        control_json="", expected_config_sha=""):
    lock = data_dir / ".cycle.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        state = FactoryState.model_validate(read_json(data_dir / "state.json"))
        config = FactoryConfig.model_validate(read_json(data_dir / "config.json"))
        if control_json:
            import hashlib
            import json
            raw = (data_dir / "config.json").read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if not expected_config_sha or actual != expected_config_sha:
                raise ValueError("Stale configuration: reload current config before retrying")
            config = FactoryConfig.model_validate(json.loads(control_json))
        db_path = data_dir / DB_PATH
        fab = load_tick(state.cycle, db_path) or initialize_equipment(state)
        maintenance = []
        triggers = []
        new_state, config = simulate(state, config, random.Random(seed), event,
                                     fab=fab, maintenance=maintenance, triggers=triggers)
        telemetry_path = data_dir / "telemetry_history.json"
        telemetry = read_json(telemetry_path) if telemetry_path.exists() else []
        telemetry = ([row for row in telemetry if row["cycle"] < new_state.cycle] + [new_state.model_dump()])[-100:]
        record = deliberate(new_state, config, mode, provider, telemetry[-5:],
                            force=force, fab=fab, triggers=triggers)
        action = record["final_action"]
        for stage, equipment in fab["equipments"].items():
            if action[f"{stage}_action"] == "SCHEDULE_PM_NOW":
                execute_preventive_maintenance(fab["equipments"], stage, maintenance, new_state.cycle)
            equipment["feed_rate"] = action.get(f"{stage}_feed_rate", equipment["feed_rate"])
        save_tick(new_state, fab, record, maintenance, db_path)
        export_replay_json(output_path=data_dir / REPLAY_PATH, db_path=db_path, through_tick=new_state.cycle)
        write_json(telemetry_path, telemetry)
        from records import save_record
        save_record(data_dir, record)
        write_json(data_dir / "state.json", new_state.model_dump())
        print(f"Cycle {new_state.cycle}: {new_state.status}, council={record['mode']}")
        return record
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--force-ai-debate", action="store_true")
    parser.add_argument("--control-json", default="")
    parser.add_argument("--expected-config-sha", default="")
    from factory_sim import EVENTS
    parser.add_argument("--event", choices=EVENTS, default="auto")
    args = parser.parse_args()
    run(args.data_dir, args.mode, args.seed, event=args.event,
        force=args.force_ai_debate or os.getenv("FORCE_AI_DEBATE", "false").lower() == "true",
        control_json=args.control_json, expected_config_sha=args.expected_config_sha)
