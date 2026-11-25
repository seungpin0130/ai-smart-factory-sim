"""Versioned snapshots of a single running fab, including random-generator state."""
from dataclasses import asdict

from backend.simulator import EquipmentTool, FoupLot, M16SemiconFabSimulator, SemiE10State


def snapshot(sim):
    values = {key: value for key, value in vars(sim).items()
              if key not in {"tools", "wip_lots", "completed_lots", "rng", "lot_events"}}
    return {
        "version": 1,
        "values": values,
        "rng": sim.rng.getstate(),
        "wip": [asdict(lot) for lot in sim.wip_lots],
        "completed": [asdict(lot) for lot in sim.completed_lots],
        "tools": [{**asdict(tool), "state": tool.state.value,
                   "active_lot": tool.active_lot.lot_id if tool.active_lot else None}
                  for tool in sim.tools.values()],
    }


def restore(payload):
    if payload["version"] != 1:
        raise ValueError("Unsupported fab checkpoint")
    sim = M16SemiconFabSimulator(seed=payload["values"]["seed"])
    vars(sim).update(payload["values"])
    version, state, gaussian = payload["rng"]
    sim.rng.setstate((version, tuple(state), gaussian))
    sim.wip_lots = [FoupLot(**lot) for lot in payload["wip"]]
    sim.completed_lots = [FoupLot(**lot) for lot in payload["completed"]]
    by_id = {lot.lot_id: lot for lot in sim.wip_lots}
    sim.tools = {}
    for raw in payload["tools"]:
        tool = EquipmentTool(**{**raw, "state": SemiE10State(raw["state"]),
                                "active_lot": by_id[raw["active_lot"]] if raw["active_lot"] else None})
        sim.tools[tool.tool_id] = tool
    sim.lot_events.clear()
    return sim
