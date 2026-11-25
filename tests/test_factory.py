import copy
import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pydantic import ValidationError
from ai_council import Audit, Decision, LiveCouncil, deliberate, enforce
from factory_sim import simulate
from github_alerts import MARKER, sync
from models import FactoryConfig, FactoryState, classify
from run_cycle import run

ROOT = Path(__file__).resolve().parents[1]


def state(**updates):
    data = json.loads((ROOT / "state.json").read_text())
    data.update(updates)
    return FactoryState.model_validate(data)


def config(**updates):
    return FactoryConfig.model_validate({"conveyor_speed": 75, "cooling_power": 50,
                                       "auto_restock": False, **updates})


class NoEvent:
    def random(self):
        return .9


class FakeProvider:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []

    def mark(self, role, transcript):
        self.calls.append((role, copy.deepcopy(transcript)))
        if self.fail_at == role:
            raise TimeoutError("SECRET-DO-NOT-LOG")

    def operations(self, context, transcript):
        self.mark("operations", transcript)
        return Decision(new_config=config(conveyor_speed=95),
                        action_reason="생산량 증대", create_issue_alert=False)

    def safety(self, context, transcript):
        self.mark("safety", transcript)
        return Audit(recommended_config=config(conveyor_speed=60, cooling_power=90),
                     risk_analysis="예상 과열 방지", approves_proposal=False)

    def chair(self, context, transcript):
        self.mark("chair", transcript)
        return Decision(new_config=config(conveyor_speed=99, cooling_power=10),
                        action_reason="최종안", create_issue_alert=False)


class FakeGitHub:
    def __init__(self):
        self.issues = []
        self.comments = {}
        self.calls = []

    def pages(self, path, params=None):
        if path == "issues":
            return iter(i for i in self.issues if i.get("state", "open") == "open")
        return iter(self.comments.get(int(path.split('/')[1]), []))

    def request(self, method, path, **kwargs):
        data = kwargs["json"]
        self.calls.append((method, path, data))
        if path == "issues":
            issue = {**data, "number": len(self.issues) + 1, "state": "open",
                     "user": {"login": "github-actions[bot]"}}
            self.issues.append(issue)
            return issue
        number = int(path.split('/')[1])
        if path.endswith("comments"):
            self.comments.setdefault(number, []).append(data)
            return data
        self.issues[number - 1].update(data)
        return self.issues[number - 1]


class SimulationTests(unittest.TestCase):
    def test_material_limits_output(self):
        s, _ = simulate(state(raw_material=12), config(), NoEvent())
        self.assertEqual((s.output_units, s.raw_material), (12, 0))

    def test_restock_consumed_once(self):
        s, c = simulate(state(raw_material=0), config(auto_restock=True, conveyor_speed=0), NoEvent())
        s2, _ = simulate(s, c, NoEvent())
        self.assertEqual((s.raw_material, s2.raw_material), (400, 400))
        self.assertFalse(c.auto_restock)

    def test_critical_interlock_ignores_unsafe_manual_config(self):
        for updates in [{"temperature": 82.0}, {"defect_rate": 8.0}]:
            s, _ = simulate(state(**updates), config(conveyor_speed=100), NoEvent())
            self.assertEqual(s.output_units, 0)
            self.assertIn("생산 정지", s.last_event)

    def test_zero_material_has_no_production_heat(self):
        a, _ = simulate(state(raw_material=0), config(conveyor_speed=100), NoEvent())
        b, _ = simulate(state(raw_material=0), config(conveyor_speed=0), NoEvent())
        self.assertEqual(a.temperature, b.temperature)

    def test_speed_increases_output_and_temperature(self):
        low, _ = simulate(state(), config(conveyor_speed=30), NoEvent())
        high, _ = simulate(state(), config(conveyor_speed=90), NoEvent())
        self.assertGreater(high.output_units, low.output_units)
        self.assertGreater(high.temperature, low.temperature)

    def test_cooling_lowers_temperature(self):
        low, _ = simulate(state(), config(cooling_power=10), NoEvent())
        high, _ = simulate(state(), config(cooling_power=90), NoEvent())
        self.assertLess(high.temperature, low.temperature)

    def test_deterministic_seed(self):
        a, _ = simulate(state(), config(), random.Random(42))
        b, _ = simulate(state(), config(), random.Random(42))
        self.assertEqual(a.model_dump(exclude={"timestamp"}), b.model_dump(exclude={"timestamp"}))

    def test_long_run_inventory_conservation(self):
        s, c = state(), config()
        rng = random.Random(18)
        for _ in range(1000):
            before = s.raw_material + (400 if c.auto_restock else 0)
            s, c = simulate(s, c, rng)
            delta = before - s.raw_material - s.output_units
            self.assertIn(delta, range(81))
            self.assertTrue(25 <= s.temperature <= 250)
            self.assertTrue(0 <= s.defect_rate <= 100)
            record = deliberate(s, c)
            c = FactoryConfig.model_validate(record["applied_config"])


class ControlTests(unittest.TestCase):
    def test_classification_boundaries(self):
        for t, d, m, expected in [(81.9,7.99,80,"WARNING"), (82,0,500,"CRITICAL"),
                (25,8,500,"CRITICAL"), (25,0,79,"CRITICAL"), (70,0,500,"WARNING"),
                (25,4,500,"WARNING"), (25,0,199,"WARNING"), (69.9,3.99,200,"NORMAL")]:
            self.assertEqual(classify(t,d,m), expected)

    def test_reject_invalid_configs(self):
        for updates in [{"conveyor_speed":101}, {"conveyor_speed":-1},
                {"conveyor_speed":"70"}, {"conveyor_speed":True},
                {"cooling_power":math.nan}, {"auto_restock":"false"}, {"extra":1}]:
            with self.assertRaises(ValidationError):
                config(**updates)

    def test_emergency_stop(self):
        c, _ = enforce(state(temperature=82.0), config(conveyor_speed=100, cooling_power=0))
        self.assertEqual((c.conveyor_speed,c.cooling_power), (0,100))

    def test_warning_bands(self):
        for t, speed, cooling in [(75.0,50,90), (70.0,65,75)]:
            c, _ = enforce(state(temperature=t), config(conveyor_speed=100, cooling_power=0))
            self.assertEqual((c.conveyor_speed,c.cooling_power), (speed,cooling))

    def test_predictive_restock(self):
        c, _ = enforce(state(raw_material=300), config(conveyor_speed=80))
        self.assertTrue(c.auto_restock)

    def test_mock_never_instantiates_live_client(self):
        with patch("ai_council.LiveCouncil", side_effect=AssertionError):
            result = deliberate(state(), config())
        self.assertEqual(result["mode"], "mock")
        self.assertEqual(len(result["transcript"]), 3)

    def test_all_provider_failure_stages(self):
        for index, stage in enumerate(["operations","safety","chair"]):
            result = deliberate(state(temperature=90.0), config(), "live", FakeProvider(stage))
            self.assertEqual(result["mode"], "fallback")
            self.assertEqual(len(result["transcript"]), index)
            self.assertEqual(result["applied_config"]["conveyor_speed"], 0)
            self.assertTrue(result["create_issue_alert"])
            self.assertNotIn("SECRET", json.dumps(result))

    def test_real_turn_order_and_audit_enforced(self):
        provider = FakeProvider()
        result = deliberate(state(), config(), "live", provider)
        self.assertEqual([c[0] for c in provider.calls], ["operations","safety","chair"])
        self.assertEqual([len(c[1]) for c in provider.calls], [0,1,2])
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["applied_config"]["conveyor_speed"], 60)
        self.assertEqual(result["applied_config"]["cooling_power"], 90)
        self.assertTrue(result["overrides"])

    def test_missing_keys_falls_back(self):
        with patch.dict("os.environ", {}, clear=True):
            result = deliberate(state(), config(), "live")
        self.assertEqual(result["mode"], "fallback")


class PersistenceTests(unittest.TestCase):
    def test_full_cycle_snapshots_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in [("state.json",state().model_dump()),("config.json",config().model_dump())]:
                (root/name).write_text(json.dumps(value))
            run(root, "mock",42)
            result = run(root, "mock",43)
            snapshot = json.loads((root/"dashboard.json").read_text())
            self.assertEqual(snapshot["state"], result["state"])
            self.assertEqual(snapshot["config"], result["applied_config"])
            self.assertEqual(len(snapshot["history"]),2)
            self.assertTrue((root/"logs/cycles/00000002.json").exists())
            self.assertFalse((root/".cycle.lock").exists())

    def test_lock_prevents_concurrent_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/".cycle.lock").touch()
            with self.assertRaises(FileExistsError):
                run(root)
            self.assertTrue((root/".cycle.lock").exists())

    def test_bad_input_does_not_advance_cycle_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            original=json.dumps(state().model_dump())
            (root/"state.json").write_text(original)
            (root/"config.json").write_text('{}')
            with self.assertRaises(ValidationError):
                run(root)
            self.assertEqual((root/"state.json").read_text(),original)
            self.assertFalse((root/".cycle.lock").exists())


class IncidentTests(unittest.TestCase):
    def record(self, **updates):
        return deliberate(state(**updates),config(),"live",FakeProvider())

    def test_critical_issue_and_turns_are_idempotent(self):
        github=FakeGitHub()
        record=self.record(temperature=90.0)
        sync(record,github)
        count=len(github.calls)
        sync(record,github)
        self.assertEqual(len(github.issues),1)
        self.assertEqual(len(github.comments[1]),3)
        self.assertEqual(len(github.calls),count)

    def test_recovery_closes_managed_only(self):
        github=FakeGitHub()
        github.issues=[{"number":1,"body":"A human issue","user":{"login":"owner"},"state":"open"}]
        sync(self.record(temperature=90.0),github)
        sync(self.record(temperature=45.0,raw_material=500),github)
        self.assertEqual(github.issues[0]["state"],"open")
        self.assertEqual(github.issues[1]["state"],"closed")

    def test_warning_does_not_close(self):
        github=FakeGitHub()
        sync(self.record(temperature=90.0),github)
        sync(self.record(temperature=74.0),github)
        self.assertEqual(github.issues[0]["state"],"open")

    def test_mock_cannot_modify_incidents(self):
        github=FakeGitHub()
        sync(deliberate(state(temperature=90.0),config(),"mock"),github)
        self.assertFalse(github.calls)

    def test_fallback_still_reports_critical(self):
        github=FakeGitHub()
        record=deliberate(state(raw_material=0),config(),"live",FakeProvider("operations"))
        sync(record,github)
        self.assertEqual(len(github.issues),1)
        self.assertEqual(len(github.comments[1]),1)


if __name__ == "__main__":
    unittest.main()
