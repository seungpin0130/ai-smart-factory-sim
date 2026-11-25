"""Adapter between the process engine and the existing three-station v6 view."""

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta

from m16_semicon_fab_sim import M16_HBM3E_ROUTE, M16SemiconFabSimulator, SemiE10State
from oht_logistics import simulate_oht_logistics

STATION_TO_TOOL = {"PHOTO_EUV": "EUV_01", "ETCH": "ETCH_01", "HBM_TSV": "HBM_TSV_01"}
PROCESS_ROUTE = [asdict(step) for step in M16_HBM3E_ROUTE]


def start_engine(tick, equipment, historical=False):
    sim = M16SemiconFabSimulator()
    sim.tick = tick
    if historical:
        for station, tool_id in STATION_TO_TOOL.items():
            previous = equipment[station]
            tool = sim.tools[tool_id]
            tool.feed_rate = previous["feed_rate"]
            if previous["load_ratio"] > 0:
                tool.load_ratio = previous["load_ratio"]
            tool.chamber_temp_c = previous["chamber_temp"]
            set_rul(tool, previous["rul"])
            if previous["status"] == "MAINTENANCE":
                kind = previous.get("maint_type") or "PM"
                tool.state = SemiE10State.UDT if kind == "BM" else SemiE10State.SDT
                tool.downtime_reason = "REPLAY_MAINTENANCE"
                tool.downtime_remain_min = previous.get("maint_remain", 1) * sim.tick_minutes
    return sim


def set_rul(tool, rul):
    penalty = max(0, (tool.chamber_temp_c - tool.opt_temp_c) * .4)
    tool.rf_on_hours = tool.max_rf_hours * max(0, 1 - (rul + penalty) / 100)


def dashboard_snapshot(sim, observation, timestamp, *, trigger=None, action=None, maintenance=()):
    extra = {**observation, "route": PROCESS_ROUTE}
    tools = observation["tools"]
    wip_counts = Counter(lot.current_step.tool_group for lot in sim.wip_lots if lot.current_step)
    wip = {station: wip_counts[sim.tools[tool_id].tool_group] * 25
           for station, tool_id in STATION_TO_TOOL.items()}
    equipment = {}
    for station, tool_id in STATION_TO_TOOL.items():
        tool = sim.tools[tool_id]
        in_maintenance = tool.state in (SemiE10State.SDT, SemiE10State.UDT)
        equipment[station] = {
            **tools[tool_id],
            "tool_id": tool_id,
            "e10_state": tool.state.value,
            "status": "MAINTENANCE" if in_maintenance else "RUNNING",
            "maint_type": ("BM" if tool.state == SemiE10State.UDT else "PM") if in_maintenance else None,
            "maint_remain": max(0, int(tool.downtime_remain_min / sim.tick_minutes)),
            "chamber_temp": tool.chamber_temp_c,
            "rul": tool.rul_pct,
            "load_ratio": 0 if in_maintenance else tool.load_ratio,
            "feed_rate": tool.feed_rate,
        }
    kpi = observation["kpi"]
    amhs = observation["amhs_oht"]
    oht = simulate_oht_logistics(wip, equipment)
    oht.update(
        active_vehicles=amhs["active_vehicles"], total_vehicles=amhs["total_vehicles"],
        utilization=amhs["congestion_pct"] / 100, oht_power_kw=amhs["oht_power_kw"],
    )
    completed = kpi["completed_wafers_this_tick"]
    good = round(completed * kpi["eds_yield_pct"] / 100)
    power_cost = kpi["power_kw"] * sim.tick_minutes / 60 * .12
    unit_cost = round((power_cost + 45 + sum(event[-1] for event in maintenance)) / good, 2) if good else None
    return {
        "tick": sim.tick, "timestamp": timestamp, "tick_minutes": sim.tick_minutes,
        "wafer_output": good,
        "kpi": {**kpi, "eds_yield": kpi["eds_yield_pct"], "unit_cost": unit_cost},
        "wip_buffers": wip, "equipments": equipment, "oht_metrics": oht,
        "trigger_reason": trigger, "debate": None, "final_action": action,
        "semicon": extra,
    }


def next_timestamp(value, minutes):
    return (datetime.fromisoformat(value) + timedelta(minutes=minutes)).isoformat()
