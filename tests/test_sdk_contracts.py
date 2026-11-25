"""Exercise installed SDK parsing and wire schemas without contacting providers."""
import json
import unittest
import httpx
from google import genai
from google.genai import types
from openai import OpenAI
from ai_council import LiveCouncil, deliberate
from test_factory import state, config


class SDKContractTests(unittest.TestCase):
    def test_full_three_turn_council_through_real_sdk_parsers(self):
        calls=[]
        decision={"new_config":config().model_dump(), "action_reason":"생산 유지", "create_issue_alert":False}
        audit={"recommended_config":config(euv_throughput=60,chiller_power=80).model_dump(),
               "risk_analysis":"보수적인 안전 마진", "approves_proposal":False}

        def openai_handler(request):
            body=json.loads(request.content)
            calls.append(("openai",body))
            self.assertEqual(body["text"]["format"]["type"],"json_schema")
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertFalse(body["store"])
            return httpx.Response(200,json={"id":"resp_test","object":"response","created_at":1,
                "status":"completed","model":"gpt-4o-mini","output":[{"id":"msg_test",
                    "type":"message","role":"assistant","status":"completed",
                    "content":[{"type":"output_text","text":json.dumps(decision),"annotations":[]}]}]})

        def gemini_handler(request):
            body=json.loads(request.content)
            calls.append(("gemini",body))
            self.assertEqual(body["generationConfig"]["responseMimeType"],"application/json")
            self.assertIn("responseSchema",body["generationConfig"])
            return httpx.Response(200,json={"candidates":[{"content":{"role":"model","parts":[
                {"text":json.dumps(audit)}]},"finishReason":"STOP"}]})

        council=LiveCouncil.__new__(LiveCouncil)
        council.openai_model="gpt-4o-mini"
        council.gemini_model="gemini-2.5-flash"
        council.openai=OpenAI(api_key="test-not-a-real-key",http_client=httpx.Client(
            transport=httpx.MockTransport(openai_handler),trust_env=False))
        council.gemini=genai.Client(api_key="test-not-a-real-key",http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(gemini_handler),trust_env=False),
            async_client_args={"trust_env":False}))
        try:
            record=deliberate(state(),config(),"live",council)
        finally:
            council.openai.close()
            council.gemini.close()
        self.assertEqual(record["mode"],"live",record)
        self.assertEqual([name for name,_ in calls],["openai","gemini","openai"])
        self.assertEqual(record["applied_config"]["euv_throughput"],60)
        self.assertEqual(record["applied_config"]["chiller_power"],80)


if __name__ == "__main__":
    unittest.main()
