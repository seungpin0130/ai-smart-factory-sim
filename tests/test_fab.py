import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from jsonschema import Draft202012Validator, ValidationError
from ai_council import deliberate, enforce, main as council_main
from factory_sim import simulate
from generate_openapi import build, REPOSITORY
from migrate_to_fab import migrate
from models import classify_state
from prepare_config_update import make_body
from records import save_record
from test_factory import NoEvent, ROOT, config, state


class FabPhysicsTests(unittest.TestCase):
    def test_euv_instability_reduces_throughput_and_yield(self):
        baseline, _ = simulate(state(), config(), NoEvent(), "none")
        event, _ = simulate(state(), config(), NoEvent(), "euv_instability")
        self.assertLess(event.wafer_output, baseline.wafer_output)
        self.assertLess(event.eds_yield, baseline.eds_yield)

    def test_chiller_fault_increases_temperature(self):
        baseline, _ = simulate(state(), config(), NoEvent(), "none")
        fault, _ = simulate(state(), config(), NoEvent(), "chiller_flow_loss")
        self.assertGreater(fault.chamber_temp, baseline.chamber_temp)

    def test_fumes_raise_particles_and_reduce_yield(self):
        baseline, _ = simulate(state(), config(), NoEvent(), "none")
        fumes, _ = simulate(state(), config(), NoEvent(), "mr_muf_fumes")
        self.assertGreater(fumes.cleanroom_class, baseline.cleanroom_class)
        self.assertLess(fumes.eds_yield, baseline.eds_yield)

    def test_pellicle_wears_during_production(self):
        after, _ = simulate(state(), config(), NoEvent(), "none")
        self.assertLess(after.pellicle_health, 100)

    def test_preventive_maintenance_pauses_one_cycle(self):
        after, c = simulate(state(pellicle_health=19.0), config(), NoEvent(), "none")
        self.assertEqual(after.wafer_output, 0)
        self.assertEqual(after.pellicle_health, 100)
        resumed, _ = simulate(after, c, NoEvent(), "none")
        self.assertGreater(resumed.wafer_output, 0)

    def test_low_yield_interlock_can_recover(self):
        after, _ = simulate(state(eds_yield=87.0), config(chiller_power=100), NoEvent(), "none")
        self.assertEqual(after.foup_wafer_stock,state().foup_wafer_stock)
        self.assertGreaterEqual(after.eds_yield, 94)

    def test_cleanroom_and_pellicle_boundary_classification(self):
        self.assertEqual(classify_state(state(cleanroom_class=30, pellicle_health=35.0)), "NORMAL")
        self.assertEqual(classify_state(state(cleanroom_class=31)), "WARNING")
        self.assertEqual(classify_state(state(pellicle_health=29.99)), "WARNING")

    def test_controller_treats_lower_yield_as_danger(self):
        result, _ = enforce(state(eds_yield=87.99), config())
        self.assertEqual((result.euv_throughput, result.chiller_power), (0, 100))

    def test_controller_maintains_low_pellicle(self):
        result, _ = enforce(state(pellicle_health=19.0), config())
        self.assertTrue(result.schedule_pm)

    def test_controller_accounts_for_cleanroom_and_forecast(self):
        dirty, _ = enforce(state(cleanroom_class=31), config(euv_throughput=100, chiller_power=0))
        hot, reasons = enforce(state(chamber_temp=61.9), config(euv_throughput=100, chiller_power=0))
        for c in [dirty, hot]:
            self.assertLessEqual(c.euv_throughput, 50)
            self.assertGreaterEqual(c.chiller_power, 85)
        self.assertTrue(reasons)

    def test_restock_boundary(self):
        no_order, _ = enforce(state(foup_wafer_stock=120), config(euv_throughput=0))
        order, _ = enforce(state(foup_wafer_stock=119), config(euv_throughput=0))
        self.assertFalse(no_order.auto_foup_order)
        self.assertTrue(order.auto_foup_order)

    def test_invalid_mode_and_event_fail_explicitly(self):
        with self.assertRaises(ValueError):
            deliberate(state(), config(), "typo")
        with self.assertRaises(ValueError):
            simulate(state(), config(), NoEvent(), "typo")


class MigrationAndRecordTests(unittest.TestCase):
    def test_migration_preserves_counters_and_archives_old_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            old={"cycle":1,"timestamp":"2026-11-25T00:00:00Z","temperature":49.0,
                 "output_units":112,"total_output":112,"defect_rate":.8,
                 "raw_material":388,"status":"NORMAL","last_event":"normal"}
            (root/"state.json").write_text(json.dumps(old))
            (root/"config.json").write_text(json.dumps({"conveyor_speed":70,"cooling_power":65,"auto_restock":False}))
            (root/"logs/cycles").mkdir(parents=True)
            (root/"logs/cycles/00000001.json").write_text('{"old":true}')
            self.assertTrue(migrate(root))
            converted=json.loads((root/"state.json").read_text())
            self.assertEqual(converted["cycle"],1)
            self.assertEqual(converted["total_wafer_output"],112)
            self.assertEqual(converted["eds_yield"],99.2)
            self.assertTrue((root/"logs/legacy-general-factory/cycles/00000001.json").exists())
            self.assertFalse(migrate(root))

    def test_unknown_schema_migration_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"state.json").write_text('{}')
            (root/"config.json").write_text('{}')
            with self.assertRaises(ValueError):
                migrate(root)
            self.assertFalse((root/"logs").exists())

    def test_standalone_council_writes_markdown_and_same_cycle_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"state.json").write_text(state().model_dump_json())
            (root/"config.json").write_text(config().model_dump_json())
            with patch("sys.argv",["ai_council.py","--mode","mock","--data-dir",directory]):
                council_main()
            self.assertIn("EDS",(root/"logs/council_history.md").read_text())
            record=deliberate(state(),config())
            save_record(root,record)
            self.assertEqual(len(json.loads((root/"logs/history.json").read_text())),1)

    def test_history_window_retains_per_cycle_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for cycle in range(122):
                save_record(root,deliberate(state(cycle=cycle),config()))
            history=json.loads((root/"logs/history.json").read_text())
            self.assertEqual(len(history),120)
            self.assertEqual(history[0]["cycle"],2)
            self.assertTrue((root/"logs/cycles/00000000.json").exists())


class OpenAPIContractTests(unittest.TestCase):
    def test_generated_schema_is_current(self):
        self.assertEqual(build(),json.loads((ROOT/"openapi_gpt_actions.json").read_text()))

    def test_schema_components_and_live_file_data_validate(self):
        schemas=build()["components"]["schemas"]
        for schema in schemas.values():
            Draft202012Validator.check_schema(schema)
        Draft202012Validator(schemas["FactoryConfig"]).validate(config().model_dump())
        Draft202012Validator(schemas["FactoryState"]).validate(state().model_dump())

    def test_actions_constraints_and_fixed_repository(self):
        spec=build()
        ids=[]
        for path,operations in spec["paths"].items():
            self.assertTrue(path.startswith(REPOSITORY))
            for method,operation in operations.items():
                ids.append(operation["operationId"])
                self.assertLessEqual(len(operation.get("description","")),300)
                self.assertLessEqual(len(operation["summary"]),300)
                self.assertEqual(operation["x-openai-isConsequential"],method in {"put","post"})
                self.assertFalse(any(p["in"]=="header" for p in operation.get("parameters",[])))
        self.assertEqual(len(ids),len(set(ids)))

    def test_config_payload_round_trip_and_validation(self):
        body=make_body(config().model_dump(),"a"*40)
        self.assertEqual(json.loads(base64.b64decode(body["content"])),config().model_dump())
        Draft202012Validator(build()["components"]["schemas"]["ConfigWrite"]).validate(body)
        with self.assertRaises(ValueError):
            make_body(config().model_dump(),"not-a-blob-sha")

    def test_dispatch_and_write_reject_unknown_values(self):
        schema=build()["components"]["schemas"]["DispatchRequest"]
        validator=Draft202012Validator(schema)
        validator.validate({"ref":"main","inputs":{"mode":"mock","alerts":False}})
        for bad in [{"ref":"main","inputs":{"mode":"bad","alerts":False}},
                    {"ref":"other","inputs":{"mode":"mock","alerts":False}}]:
            with self.assertRaises(ValidationError):
                validator.validate(bad)


if __name__ == "__main__":
    unittest.main()
