import json
import tempfile
import unittest
from pathlib import Path

from ai_council import deliberate
from db_manager import DB_PATH, REPLAY_PATH, save_tick
from factory_sim import TICK_HOURS, initialize_equipment, simulate
from oht_logistics import simulate_oht_logistics
from run_cycle import run
from storage import read_json, write_json
from test_factory import config, state


class OHTTests(unittest.TestCase):
    def test_queue_increases_delay_and_station_maintenance_stops_dispatch(self):
        fab = initialize_equipment(state())
        equipment = fab["equipments"]
        baseline = simulate_oht_logistics({"ETCH": 20}, equipment)
        traffic = simulate_oht_logistics({"ETCH": 100}, equipment)
        route = "PHOTO_EUV_TO_ETCH"
        self.assertGreater(traffic["routes"][route]["transfer_time_sec"],
                           baseline["routes"][route]["transfer_time_sec"])
        self.assertTrue(traffic["alerts"][0].startswith(f"OHT_CONGESTION:{route}"))
        equipment["PHOTO_EUV"]["status"] = "MAINTENANCE"
        stopped = simulate_oht_logistics({"ETCH": 100}, equipment)
        self.assertLess(stopped["oht_power_kw"], traffic["oht_power_kw"])
        self.assertFalse(stopped["alerts"])

    def test_congestion_reaches_council_and_persists_in_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before, cfg = state(cycle=4), config()
            write_json(root / "state.json", before.model_dump())
            write_json(root / "config.json", cfg.model_dump())
            fab = initialize_equipment(before)
            fab["wip_buffers"]["ETCH"] = 79
            fab["equipments"]["ETCH"].update(status="MAINTENANCE", maint_remain=2, maint_type="PM")
            save_tick(before, fab, deliberate(before, cfg, fab=fab), [], root / DB_PATH)

            result = run(root, event="none")
            self.assertIn("OHT_CONGESTION:PHOTO_EUV_TO_ETCH", result["trigger_reason"])
            self.assertEqual(result["mode"], "mock")
            latest = read_json(root / REPLAY_PATH)[-1]
            oht = json.loads(latest["oht_metrics"])
            self.assertIn(oht["alerts"][0], result["trigger_reason"])
            self.assertEqual(latest["power_kw"] * TICK_HOURS, result["state"]["power_kwh"])

    def test_energy_increment_uses_kw_to_kwh_conversion(self):
        from unittest.mock import patch

        before, cfg = state(), config()
        fab = initialize_equipment(before)
        data, _ = simulate(before, cfg, event="none", fab=fab, maintenance=[], triggers=[])
        with patch("factory_sim.simulate_oht_logistics", return_value={"oht_power_kw": 0, "alerts": []}):
            control, _ = simulate(before, cfg, event="none", fab=initialize_equipment(before),
                                  maintenance=[], triggers=[])
        self.assertAlmostEqual(data.power_kwh - control.power_kwh,
                               fab["oht_metrics"]["oht_power_kw"] * TICK_HOURS)
