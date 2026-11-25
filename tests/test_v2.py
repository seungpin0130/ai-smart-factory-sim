import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ai_council import LiveCouncil, deliberate
from factory_sim import simulate, run_simulation
from run_cycle import run
from models import Decision
from test_factory import state, config, NoEvent, FakeProvider

class V2Tests(unittest.TestCase):
    def test_back_end_capacity_and_wip_conservation(self):
        before=state(hbm_wip_buffer=100)
        after,_=simulate(before,config(euv_throughput=100),NoEvent(),'none')
        self.assertEqual(after.hbm_wip_buffer,112)
        self.assertLessEqual(after.wafer_output,48)
        self.assertEqual(before.foup_wafer_stock-after.foup_wafer_stock,60)

    def test_pm_does_not_lose_wip_or_hide_heat(self):
        after,c=simulate(state(chamber_temp=100.0,hbm_wip_buffer=100),config(schedule_pm=True),NoEvent(),'none')
        self.assertEqual(after.hbm_wip_buffer,100)
        self.assertEqual(after.status,'CRITICAL')
        self.assertFalse(c.schedule_pm)
        self.assertEqual(after.ramp_up_remaining,2)

    def test_ramp_up_reduces_front_end_for_two_cycles(self):
        initial=state(hbm_wip_buffer=0)
        pm,c=simulate(initial,config(schedule_pm=True),NoEvent(),'none')
        one,_=simulate(pm,c,NoEvent(),'none')
        two,_=simulate(one,c,NoEvent(),'none')
        three,_=simulate(two,c,NoEvent(),'none')
        self.assertEqual([pm.foup_wafer_stock-one.foup_wafer_stock,one.foup_wafer_stock-two.foup_wafer_stock,two.foup_wafer_stock-three.foup_wafer_stock],[27,36,45])
        self.assertEqual([one.ramp_up_remaining,two.ramp_up_remaining],[1,0])

    def test_hold_has_zero_api_calls(self):
        with patch('ai_council.LiveCouncil',side_effect=AssertionError):
            record=deliberate(state(cycle=2),config(),'live')
        self.assertEqual(record['mode'],'hold')
        self.assertFalse(record['committee_convened'])
        self.assertEqual(record['transcript'],[])

    def test_force_overrides_stable_interval(self):
        record=deliberate(state(cycle=2),config(),'live',FakeProvider(),force=True)
        self.assertEqual(record['mode'],'live')
        self.assertEqual(record['chair'],'ChatGPT')

    def test_odd_chair_calls_gemini(self):
        council=LiveCouncil.__new__(LiveCouncil)
        decision=Decision(new_config=config(),action_reason='합의',create_issue_alert=False)
        calls=[]
        def generate(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text=decision.model_dump_json())
        council.gemini=SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        council.gemini_model='gemini-2.5-flash'
        result=council.chair('context',[],cycle=1)
        self.assertEqual(result,decision)
        self.assertEqual(len(calls),1)

    def test_low_stock_forces_safety_even_on_hold(self):
        record=deliberate(state(cycle=2,foup_wafer_stock=130),config(euv_throughput=100),'mock')
        self.assertEqual(record['mode'],'hold')
        self.assertTrue(record['applied_config']['auto_foup_order'])

    def test_energy_rises_with_chiller(self):
        low,_=simulate(state(),config(chiller_power=20),NoEvent(),'none')
        high,_=simulate(state(),config(chiller_power=100),NoEvent(),'none')
        self.assertGreater(high.power_kwh,low.power_kwh)

    def test_queued_stale_config_is_rejected_without_advancing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            before = state().model_dump_json()
            (root/'state.json').write_text(before)
            (root/'config.json').write_text(config().model_dump_json())
            with self.assertRaises(ValueError):
                run(root,control_json=config().model_dump_json(),expected_config_sha='a'*40)
            self.assertEqual((root/'state.json').read_text(), before)

    def test_queued_fresh_config_and_history_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'state.json').write_text(state(cycle=99).model_dump_json())
            raw=config().model_dump_json().encode()
            (root/'config.json').write_bytes(raw)
            (root/'telemetry_history.json').write_text(json.dumps([state(cycle=i).model_dump() for i in range(100)]))
            sha=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
            record=run(root,control_json=config(euv_throughput=20).model_dump_json(),expected_config_sha=sha)
            self.assertEqual(record['previous_config']['euv_throughput'],20)
            self.assertEqual(len(json.loads((root/'telemetry_history.json').read_text())),100)

if __name__=='__main__':unittest.main()
