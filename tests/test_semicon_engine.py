import json
from datetime import datetime

import pytest

from m16_semicon_fab_sim import M16_HBM3E_ROUTE, M16SemiconFabSimulator, SemiE10State
from test_fab_api import fab_api  # pytest fixture


def test_q_time_violation_is_counted_once_per_step_and_harms_yield():
    sim = M16SemiconFabSimulator()
    lot = sim.wip_lots[0]
    sim.wip_lots = [lot]
    sim.tools["EUV_01"].feed_rate = 0
    clean = sim.tools["CLEAN_01"]
    sim.start_maintenance(clean, "PM", 60, "MANUAL_PM")
    lot.wait_min_at_step = M16_HBM3E_ROUTE[0].q_time_limit_min
    original_yield = sim._compute_eds_yield(lot)
    assert sim.step()["kpi"]["q_time_violations"] == 1
    assert sim.step()["kpi"]["q_time_violations"] == 1
    assert lot.wait_min_at_step == 120
    assert lot.violated_steps == [M16_HBM3E_ROUTE[0].step_seq]
    assert sim._compute_eds_yield(lot) < original_yield


def test_r2r_ewma_converges_under_fixed_process_drift():
    sim = M16SemiconFabSimulator()
    sim.rng.gauss = lambda *_: 0.0
    tool = sim.tools["ETCH_01"]
    lot = sim.wip_lots[0]
    step = M16_HBM3E_ROUTE[2]
    expected_bias = tool.rf_on_hours / tool.max_rf_hours * .85 + (tool.chamber_temp_c - tool.opt_temp_c) * .09
    sim._update_fdc_and_apc(tool, step, lot)
    first_error = abs(tool.vm_predicted_cd_nm - step.target_cd_nm)
    for _ in range(15):
        sim._update_fdc_and_apc(tool, step, lot)
    assert tool.apc_bias_offset == pytest.approx(expected_bias, abs=.002)
    assert abs(tool.vm_predicted_cd_nm - step.target_cd_nm) < first_error / 100
    assert tool.fdc_t2_score == pytest.approx(3.45, abs=.01)


def test_oee_uses_e10_time_ideal_completed_work_and_quality():
    sim = M16SemiconFabSimulator()
    tool = sim.tools["CLEAN_01"]
    tool.time_prd, tool.time_sby, tool.time_eng, tool.time_sdt, tool.time_udt = (50, 20, 10, 15, 5)
    tool.ideal_process_min = 64
    lot = sim.wip_lots.pop()
    lot.route_idx = len(M16_HBM3E_ROUTE)
    lot.location = "COMPLETED"
    sim.completed_lots.append(lot)
    kpi = sim.snapshot()["kpi"]
    assert kpi["availability_pct"] == 80
    assert kpi["performance_pct"] == 80
    assert kpi["quality_pct"] == sim._compute_eds_yield(lot)
    assert kpi["oee_pct"] == round(.8 * .8 * kpi["quality_pct"], 2)
    sim.start_maintenance(tool, "PM", 15, "MANUAL_PM")
    assert sim.step()["tools"][tool.tool_id]["state"] == SemiE10State.ENG
    assert sim.step()["tools"][tool.tool_id]["time_min"]["ENG"] == 25


def test_checkpoint_and_api_continuation_keep_foop_identity_and_rng(fab_api):
    client, _ = fab_api
    base = {"base_tick": 5}
    six = client.post("/api/fab/simulate", json={**base, "ticks": 6}).json()
    first_state = six["state"]
    semicon = first_state["semicon"]
    assert len(semicon["route"]) == 12
    assert len(semicon["tools"]) == 8
    assert all(lot["wafer_count"] == 25 for lot in semicon["lots"])
    assert any(t["vm_predicted_cd_nm"] is not None for t in semicon["tools"].values())
    assert semicon["amhs_oht"]["routes"]
    assert first_state["tick_minutes"] == 15
    snapshots = six["snapshots"]
    assert (datetime.fromisoformat(snapshots[1]["timestamp"]) -
            datetime.fromisoformat(snapshots[0]["timestamp"])).total_seconds() == 900

    recovered = client.get("/api/fab/state", params={"scenario_id": six["scenario_id"]}).json()
    assert recovered["state"] == first_state
    assert recovered["history"] == snapshots
    resumed = client.post("/api/fab/simulate", json={"scenario_id": six["scenario_id"], "ticks": 6}).json()
    uninterrupted = client.post("/api/fab/simulate", json={**base, "ticks": 12}).json()
    assert snapshots + resumed["snapshots"] == uninterrupted["snapshots"]

    sim = M16SemiconFabSimulator()
    sim.step()
    restored = sim.restore(json.loads(json.dumps(sim.checkpoint())))
    tool = next(tool for tool in restored.tools.values() if tool.active_lot is not None)
    assert any(lot is tool.active_lot for lot in restored.wip_lots)
    assert restored.step() == sim.step()
