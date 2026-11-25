"""Explicit one-time migration from the general factory, preserving old records."""
import argparse
import shutil
from pathlib import Path
from models import FactoryConfig, FactoryState, classify
from storage import read_json, write_json


def migrate(root: Path):
    old_state = read_json(root / "state.json")
    old_config = read_json(root / "config.json")
    if "chamber_temp" in old_state and "euv_throughput" in old_config:
        state = FactoryState.model_validate(old_state).model_dump()
        config = FactoryConfig.model_validate(old_config).model_dump()
        if state != old_state or config != old_config:
            archive = root / "logs/legacy-fab-v1"
            archive.mkdir(parents=True, exist_ok=False)
            write_json(archive / "state.json", old_state)
            write_json(archive / "config.json", old_config)
            write_json(root / "state.json", state)
            write_json(root / "config.json", config)
            if not (root / "telemetry_history.json").exists():
                write_json(root / "telemetry_history.json", [])
            return True
        return False
    expected_state = {"cycle", "timestamp", "temperature", "output_units", "total_output",
                      "defect_rate", "raw_material", "status", "last_event"}
    expected_config = {"conveyor_speed", "cooling_power", "auto_restock"}
    if set(old_state) != expected_state or set(old_config) != expected_config:
        raise ValueError("Unknown or mixed schema; no files were changed")
    temp = max(20.0, min(150.0, float(old_state["temperature"])))
    eds = max(0.0, min(100.0, 100.0 - float(old_state["defect_rate"])))
    stock = old_state["raw_material"]
    state = FactoryState(cycle=old_state["cycle"], timestamp=old_state["timestamp"],
        chamber_temp=temp, wafer_output=old_state["output_units"],
        total_wafer_output=old_state["total_output"], eds_yield=eds,
        foup_wafer_stock=stock, cleanroom_class=10, pellicle_health=100.0,
        status=classify(temp, eds, stock),
        last_event="일반 공장→가상 팹 스키마 전환; 이전 카운터를 가상 웨이퍼 단위로 재해석")
    config = FactoryConfig(euv_throughput=old_config["conveyor_speed"],
        chiller_power=old_config["cooling_power"], auto_foup_order=old_config["auto_restock"])
    archive = root / "logs/legacy-general-factory"
    if archive.exists():
        raise FileExistsError("Legacy archive already exists; refusing to overwrite")
    archive.mkdir(parents=True)
    for name in ["state.json", "config.json", "dashboard.json"]:
        if (root / name).exists():
            shutil.copy2(root / name, archive / name)
    for name in ["history.json", "council_history.md", "last_decision.json", "cycles"]:
        source = root / "logs" / name
        if source.exists():
            shutil.move(str(source), str(archive / name))
    write_json(root / "state.json", state.model_dump())
    write_json(root / "config.json", config.model_dump())
    write_json(root / "logs/history.json", [])
    write_json(root / "dashboard.json", {"state": state.model_dump(),
                                        "config": config.model_dump(), "history": []})
    (root / "logs/council_history.md").write_text("# 가상 반도체 팹 회의록\n\n기존 기록은 legacy-general-factory에 보존됩니다.\n", encoding="utf-8")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    print("Migrated" if migrate(args.data_dir) else "Fab schema already valid")
