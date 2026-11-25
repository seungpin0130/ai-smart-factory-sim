"""Run an independently specified synthetic reference route."""
import json
from pathlib import Path

from backend.simulator import M16SemiconFabSimulator

REFERENCE = Path(__file__).resolve().parents[1] / "validation" / "hbm3e_reference.json"


def validate_reference(path=REFERENCE):
    reference = json.loads(path.read_text())
    sim = M16SemiconFabSimulator(seed=reference["seed"], automatic_releases=False, random_failures=False)
    sim.wip_lots.clear()
    sim.lot_events.clear()
    sim.initial_wip_wafers = 0
    lot = sim.release_lot(reference["wafers"], source="VALIDATION")
    for _ in range(reference["max_tick_count"]):
        sim.step()
        if lot.finished_minute is not None:
            break
    starts = [row for row in sim.lot_events if row["event"] == "PROCESS_STARTED"]
    finishes = [row for row in sim.lot_events if row["event"] == "PROCESS_COMPLETED"]
    mismatches = []
    if len(starts) != len(reference["steps"]) or len(finishes) != len(reference["steps"]):
        mismatches.append("Wrong number of visited route steps")
    for start, finish, expected in zip(starts, finishes, reference["steps"]):
        actual = (finish["step_name"], start["queue_min"], finish["process_min"], finish["fab_minute"])
        if (actual[0] != expected["step"] or abs(actual[1] - expected["queue_min"]) > .011
                or abs(actual[2] - expected["process_min"]) > .001
                or abs(actual[3] - expected["completed_minute"]) > .002):
            mismatches.append({"expected": expected, "actual": actual})
        if start["wafers_in"] != reference["wafers"] or finish["wafers_out"] != reference["wafers"]:
            mismatches.append(f"{actual[0]} lost incoming wafers")
    inspection = next((row for row in sim.lot_events if row["event"] == "LOT_DISPOSITION"), {})
    counts = inspection.get("good_dies_per_wafer", [])
    accepted = sum(value >= reference["min_good_dies"] for value in counts)
    if (lot.finished_minute is None or inspection.get("dies_per_wafer") != reference["dies_per_wafer"]
            or inspection.get("minimum_good_dies") != reference["min_good_dies"]
            or len(counts) != reference["wafers"] or lot.accepted_wafers != accepted
            or lot.accepted_wafers + lot.rejected_wafers != reference["wafers"]
            or not all(0 <= value <= reference["dies_per_wafer"] for value in counts)):
        mismatches.append("EDS disposition does not match wafer and die acceptance rules")
    return {"passed": not mismatches, "reference_source": reference["source"],
            "steps": len(finishes), "cycle_minutes": round(lot.finished_minute, 3) if lot.finished_minute is not None else None,
            "accepted_wafers": lot.accepted_wafers, "qa_hold_wafers": lot.rejected_wafers, "mismatches": mismatches}


if __name__ == "__main__":
    result = validate_reference()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(not result["passed"])
