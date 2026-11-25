import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx
from google import genai
from google.genai import types
from openai import OpenAI

from ai_council import LiveCouncil, deliberate
from db_manager import DB_PATH, REPLAY_PATH, export_replay_json, load_tick, save_tick
from factory_sim import initialize_equipment, simulate
from run_cycle import run
from storage import read_json, write_json
from test_factory import config, state


class V3Tests(unittest.TestCase):
    def test_tick_persists_triggers_actions_and_maintenance_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / DB_PATH
            before = state(cycle=4)
            cfg = config()
            write_json(root / "state.json", before.model_dump())
            write_json(root / "config.json", cfg.model_dump())

            fab = initialize_equipment(before)
            fab["wip_buffers"]["ETCH"] = 86
            fab["equipments"]["ETCH"]["rul"] = 18.4
            initial = deliberate(before, cfg, fab=fab)
            save_tick(before, fab, initial, [], db)

            result = run(root, event="none")
            self.assertIn("RUL_CRITICAL:ETCH", result["trigger_reason"])
            self.assertIn("WIP_BOTTLENECK:ETCH", result["trigger_reason"])
            self.assertEqual(result["final_action"]["ETCH_action"], "SCHEDULE_PM_NOW")
            replay = read_json(root / REPLAY_PATH)
            self.assertEqual([row["tick_id"] for row in replay], [4, 5])
            self.assertEqual([turn["turn"] for turn in json.loads(replay[-1]["debate_transcript"])], [1, 2, 3])
            self.assertEqual(json.loads(replay[-1]["final_action"]), result["final_action"])
            self.assertEqual(replay[-1]["timestamp"], read_json(root / "state.json")["timestamp"])
            export_replay_json(1, root / REPLAY_PATH, db)
            self.assertEqual([row["tick_id"] for row in read_json(root / REPLAY_PATH)], [5])
            with closing(sqlite3.connect(db)) as conn:
                event = conn.execute(
                    "SELECT tick_id, equip_id, maint_type, downtime_ticks, cost_impact FROM maintenance_logs"
                ).fetchone()
                self.assertEqual(event, (5, "ETCH", "PM", 2, 1200.0))
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))

            for tick, remain in ((6, 1), (7, 0)):
                run(root, event="none")
                etch = load_tick(tick, db)["equipments"]["ETCH"]
                self.assertEqual(etch["load_ratio"], 0)
                self.assertEqual(etch["maint_remain"], remain)

    def test_breakdown_keeps_wafers_and_blocks_the_next_tick(self):
        before = state(cycle=4)
        fab = initialize_equipment(before)
        fab["equipments"]["ETCH"]["rul"] = 0.5
        maintenance, triggers = [], []
        after, consumed = simulate(before, config(), event="none", fab=fab,
                                   maintenance=maintenance, triggers=triggers)
        self.assertIn("BM_FAILURE:ETCH", triggers)
        self.assertEqual(maintenance, [(5, "ETCH", "BM", 0.5, 6, 4500.0)])
        etch_buffer = fab["wip_buffers"]["ETCH"]
        next_state, _ = simulate(after, consumed, event="none", fab=fab,
                                 maintenance=[], triggers=[])
        self.assertEqual(fab["equipments"]["ETCH"]["maint_remain"], 5)
        self.assertEqual(fab["equipments"]["ETCH"]["load_ratio"], 0)
        self.assertEqual(fab["wip_buffers"]["ETCH"] - etch_buffer,
                         after.foup_wafer_stock - next_state.foup_wafer_stock)

    def test_both_chairs_send_and_parse_structured_requests(self):
        action = {
            "PHOTO_EUV_feed_rate": 1.2, "ETCH_feed_rate": 1.0,
            "PHOTO_EUV_action": "KEEP_RUNNING", "ETCH_action": "SCHEDULE_PM_NOW",
            "HBM_TSV_action": "KEEP_RUNNING", "target_temp": 62.0,
        }
        decision = {"new_config": config().model_dump(), "action_reason": "<b>생산안</b>",
                    "create_issue_alert": False, "equipment_action": action}
        audit = {"recommended_config": config().model_dump(), "risk_analysis": "정비 권고",
                 "approves_proposal": False, "equipment_action": action}
        calls = []

        def openai_handler(request):
            body = json.loads(request.content)
            self.assertIn("equipment_action", body["text"]["format"]["schema"]["properties"])
            calls.append("openai")
            return httpx.Response(200, json={
                "id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
                "model": "gpt-4o-mini", "output": [{
                    "id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps(decision), "annotations": []}],
                }],
            })

        def gemini_handler(request):
            schema = json.loads(request.content)["generationConfig"]["responseJsonSchema"]
            self.assertIn("equipment_action", schema["properties"])
            calls.append("gemini")
            reply = audit if "risk_analysis" in schema["properties"] else decision
            return httpx.Response(200, json={"candidates": [{
                "content": {"role": "model", "parts": [{"text": json.dumps(reply)}]},
                "finishReason": "STOP",
            }]})

        council = LiveCouncil.__new__(LiveCouncil)
        council.openai_model = "gpt-4o-mini"
        council.gemini_model = "gemini-2.5-flash"
        council.openai = OpenAI(api_key="fake", http_client=httpx.Client(
            transport=httpx.MockTransport(openai_handler), trust_env=False))
        council.gemini = genai.Client(api_key="fake", http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(gemini_handler), trust_env=False),
            async_client_args={"trust_env": False}))
        try:
            for cycle, chair_provider in ((4, "openai"), (5, "gemini")):
                with self.subTest(cycle=cycle):
                    calls.clear()
                    sample = state(cycle=cycle)
                    fab = initialize_equipment(sample)
                    fab["equipments"]["ETCH"]["rul"] = 18.4
                    fab["equipments"]["PHOTO_EUV"]["feed_rate"] = 0.4
                    row = deliberate(sample, config(), "live", council, fab=fab,
                                     triggers=("RUL_CRITICAL:ETCH", "WIP_BOTTLENECK:ETCH"))
                    self.assertEqual(row["mode"], "live", row["error_type"])
                    self.assertEqual(calls, ["openai", "gemini", chair_provider])
                    self.assertEqual(row["final_action"]["PHOTO_EUV_feed_rate"], 0.4)
                    self.assertEqual(row["final_action"]["ETCH_action"], "SCHEDULE_PM_NOW")
                    self.assertEqual(row["debate_transcript"][0]["text"], "<b>생산안</b>")
        finally:
            council.openai.close()
            council.gemini.close()
