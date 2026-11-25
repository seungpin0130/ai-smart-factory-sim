"""Educational v2 fab surrogate based on the supplied project, not actual fab data."""
import argparse
import random
from datetime import datetime, timezone
from pathlib import Path
from models import FactoryConfig, FactoryState, classify_state
from storage import read_json, write_json

EVENTS = ("auto", "none", "euv_instability", "chiller_flow_loss", "mr_muf_fumes")
ORDER_BATCH = 200  # Virtual delivery batch, not one physical FOUP's capacity.
WAFERS_PER_PERCENT = .6


def nominal_temperature(temperature, throughput, cooling):
    return max(22.0, temperature + throughput * .24 - cooling * .23 - (temperature - 22) * .14)


def simulate(state: FactoryState, config: FactoryConfig, rng=None, event="auto"):
    if event not in EVENTS:
        raise ValueError("Unknown simulated event")
    rng = rng or random.Random()
    data = state.model_dump()
    data.update(cycle=state.cycle + 1, timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    stock = state.foup_wafer_stock + (ORDER_BATCH if config.auto_foup_order else 0)
    events = ["OHT 가상 배치 200매 입고 완료"] if config.auto_foup_order else []
    consumed = config.model_copy(update={"auto_foup_order": False, "schedule_pm": False})
    throughput, chiller = config.euv_throughput, config.chiller_power

    if config.schedule_pm or state.pellicle_health < 20:
        data.update(pellicle_health=100.0, chamber_temp=round(max(24.0, state.chamber_temp - 14), 1),
            cleanroom_class=8, ramp_up_remaining=2, wafer_output=0,
            foup_wafer_stock=stock, power_kwh=round(180.0 + chiller * 1.5, 1),
            unit_cost_index=115.0)
        # No unaccounted WIP disappears during PM. Reclassify even if PM left it hot.
        events.append("계획 예방 정비(PM): 펠리클·라이너 교체 및 퍼지, 이후 2사이클 램프업")
    else:
        if state.chamber_temp >= 68 or state.eds_yield < 88 or state.cleanroom_class >= 45:
            throughput = 0
            chiller = max(chiller, 90)
            events.append("안전 인터록: 전공정 정지, 후공정 WIP 처리와 냉각 유지")
        if event == "auto":
            roll = rng.random()
            event = ("chiller_flow_loss" if roll < .08 else "euv_instability" if roll < .15
                     else "mr_muf_fumes" if roll < .22 else "none")
        heat_spike = yield_penalty = 0.0
        class_spike = 0
        if event == "chiller_flow_loss":
            heat_spike = rng.uniform(7.5, 13)
            events.append("돌발: 식각 챔버 칠러 유량 저하")
        elif event == "euv_instability":
            yield_penalty = rng.uniform(3, 6.5)
            events.append("돌발: EUV 광원 출력 변동")
        elif event == "mr_muf_fumes":
            class_spike = rng.randint(14, 28)
            yield_penalty = rng.uniform(1.5, 3.5)
            events.append("돌발: MR-MUF 흄 유입")
        ramp = state.ramp_up_remaining
        ramp_factor = .6 if ramp == 2 else .8 if ramp == 1 else 1.0
        fe_processed = min(int(throughput * .6 * ramp_factor), stock)
        stock -= fe_processed
        effective_throughput = fe_processed / .6
        be_processed = min(state.hbm_wip_buffer + fe_processed, 48)
        wip = state.hbm_wip_buffer + fe_processed - be_processed
        temp = round(nominal_temperature(state.chamber_temp, effective_throughput, chiller) + heat_spike, 1)
        target_class = max(1, int(8 + effective_throughput * .18 - chiller * .14 + class_spike))
        particles = max(1, min(100, round(state.cleanroom_class * .4 + target_class * .6)))
        wear = (1.2 + effective_throughput * .025 + max(0, temp - 60) * .15) if fe_processed else 0
        health = round(max(0.0, state.pellicle_health - wear), 1)
        eds = round(max(50.0, min(99.5, 98.2 - max(0, temp - 58) * .35 -
            max(0, wip - 60) * .06 - max(0, particles - 20) * .12 -
            max(0, 50 - health) * .08 - yield_penalty - 1.8 * ramp)), 2)
        good = min(be_processed, int(round(be_processed * eds / 100)))
        power = round(250.0 + effective_throughput * 3.8 + chiller * 4.5, 1)
        cost = round(((power * .12 + 45) / good / 2.2) * 100, 1) if good else 200.0
        data.update(chamber_temp=temp, wafer_output=good, total_wafer_output=state.total_wafer_output + good,
            eds_yield=eds, foup_wafer_stock=stock, hbm_wip_buffer=wip, cleanroom_class=particles,
            pellicle_health=health, power_kwh=power, unit_cost_index=cost, ramp_up_remaining=max(0, ramp - 1))
        if ramp:
            events.append(f"램프업 {int(ramp_factor*100)}%, 잔여 {ramp-1}사이클")
    data["last_event"] = " / ".join(events) or "EUV·식각 및 HBM 가상 라인 정상 가동"
    updated = FactoryState.model_validate(data)
    updated.status = classify_state(updated)
    return updated, consumed


def run_simulation(root=None, seed=None, event="auto"):
    root = Path(root or Path(__file__).parent)
    state, config = simulate(FactoryState.model_validate(read_json(root / "state.json")),
        FactoryConfig.model_validate(read_json(root / "config.json")), random.Random(seed), event)
    history_path = root / "telemetry_history.json"
    history = read_json(history_path) if history_path.exists() else []
    write_json(root / "state.json", state.model_dump())
    write_json(root / "config.json", config.model_dump())
    write_json(history_path, (history + [state.model_dump()])[-100:])
    print(f"Cycle {state.cycle}: {state.status}, good={state.wafer_output}, WIP={state.hbm_wip_buffer}")
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--event", choices=EVENTS, default="auto")
    args = parser.parse_args()
    run_simulation(args.data_dir, args.seed, args.event)
