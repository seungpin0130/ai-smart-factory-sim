"""One discrete simulation step; one material unit produces one gross unit."""
import argparse
import random
from datetime import datetime, timezone
from pathlib import Path
from models import FactoryConfig, FactoryState, classify
from storage import read_json, write_json


def simulate(state: FactoryState, config: FactoryConfig, rng=None):
    rng = rng or random.Random()
    material = state.raw_material
    events = []
    if config.auto_restock:
        material += 400
        events.append("원자재 400단위 입고 (1회 발주 소비)")
    consumed_config = config.model_copy(update={"auto_restock": False})
    roll = rng.random()
    heat_spike = defect_spike = 0.0
    if roll < 0.10:
        heat_spike = rng.uniform(8, 15)
        events.append("냉각수 펌프 일시 효율 저하")
    elif roll < 0.18:
        defect_spike = rng.uniform(2.5, 5)
        events.append("원자재 Lot 편차")
    elif roll < 0.25:
        loss = min(80, material)
        material -= loss
        events.append(f"포장 라인 걸림: 원자재 {loss}단위 폐기")

    # A deterministic interlock applies even when someone edits config.json by hand.
    speed = config.conveyor_speed
    if state.temperature >= 82 or state.defect_rate >= 8:
        speed = 0
        events.append("안전 인터록: 생산 정지")
    units = min(int(speed * 1.5), material)
    material -= units
    effective_speed = units / 1.5  # no production heat when material is exhausted
    temperature = round(max(25.0, min(250.0, state.temperature +
        effective_speed * .22 + heat_spike - config.cooling_power * .20 -
        (state.temperature - 25) * .12)), 1)
    defects = round(min(100.0, .8 + max(0, temperature - 68) * .25 +
        max(0, effective_speed - 85) * .15 + defect_spike), 2)
    new_state = FactoryState(
        cycle=state.cycle + 1,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        temperature=temperature, output_units=units,
        total_output=state.total_output + units, defect_rate=defects,
        raw_material=material, status=classify(temperature, defects, material),
        last_event=" / ".join(events) or "정상 가동")
    return new_state, consumed_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    state, config = simulate(
        FactoryState.model_validate(read_json(args.data_dir / "state.json")),
        FactoryConfig.model_validate(read_json(args.data_dir / "config.json")),
        random.Random(args.seed))
    write_json(args.data_dir / "state.json", state.model_dump())
    write_json(args.data_dir / "config.json", config.model_dump())


if __name__ == "__main__":
    main()
