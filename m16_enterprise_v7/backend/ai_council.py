import asyncio
import os
from typing import Dict, Optional


async def evaluate_and_decide(snapshot: Dict, control_mode: str = "AUTO_PILOT", *, allow_live: bool = True, q_time_delta: int = 0) -> Optional[Dict]:
    """
    Evaluates Fab state for critical triggers:
    - Any tool RUL < 25% or FDC T2 > 3.8
    - OHT congestion >= 85%
    - Q-Time violations spike
    Uses OpenAI & Gemini APIs if keys exist, otherwise deterministic engineering rules.
    """
    tools = snapshot["tools"]
    oht = snapshot["amhs_oht"]
    triggers = []
    actions = []

    for tid, t in tools.items():
        if t["state"] not in ("SDT", "UDT") and (t["rul_pct"] < 25.0 or t["fdc_t2_score"] > 3.8):
            triggers.append(f"{tid}_HEALTH_ALERT(RUL={t['rul_pct']}%, T2={t['fdc_t2_score']})")
            actions.append({"tool_id": tid, "action": "PM"})
            if tid != "EUV_01":
                actions.append({"tool_id": "EUV_01", "feed_rate": 0.6})

    if oht["congestion_pct"] >= 85.0:
        triggers.append(f"OHT_CONGESTION_HIGH({oht['congestion_pct']}%)")
        actions.append({"tool_id": "EUV_01", "feed_rate": 0.5})

    if q_time_delta > 0:
        triggers.append(f"Q_TIME_NEW({q_time_delta})")
        actions.append({"tool_id": "EUV_01", "feed_rate": 0.6})
    # Merge route throttles conservatively so congestion cannot relax a prior cap.
    feeds = [a["feed_rate"] for a in actions if "feed_rate" in a]
    actions = [a for a in actions if "feed_rate" not in a]
    if feeds:
        actions.append({"tool_id": "EUV_01", "feed_rate": min(feeds)})

    if not triggers:
        return None

    trigger_str = " | ".join(triggers)
    gpt_msg = (
        f"[ChatGPT 생산기술] {trigger_str} 감지. 상류 EUV 투입 속도를 감속하여 WIP 적체 및 Q-Time 초과를 방지해야 합니다."
    )
    gemini_msg = (
        f"[Gemini 설비·원가] FDC 이상 지수 및 RUL 임계치 도달로 돌발고장(UDT) 위험이 큽니다. 즉시 예방정비(PM) 전환을 승인합니다."
    )

    # Optional live LLM call when API keys are configured
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if allow_live and openai_key and gemini_key:
        try:
            from openai import AsyncOpenAI
            from google import genai
            oai = AsyncOpenAI(api_key=openai_key, timeout=5, max_retries=0)
            gai = genai.Client(api_key=gemini_key, http_options={"timeout": 5000})
            prompt = f"M16 Fab 경고: {trigger_str}. 한국어 1문장으로 공정/설비 조치를 권고하세요."
            o_res, g_res = await asyncio.wait_for(asyncio.gather(
                oai.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], max_tokens=120),
                asyncio.to_thread(gai.models.generate_content, model="gemini-2.5-flash", contents=prompt),
            ), timeout=6)
            gpt_msg = f"[ChatGPT] {o_res.choices[0].message.content.strip()}"
            gemini_msg = f"[Gemini] {g_res.text.strip()}"
        except Exception:
            pass

    return {
        "tick": snapshot["tick"],
        "trigger_reason": trigger_str,
        "control_mode": control_mode,
        "gpt_opinion": gpt_msg,
        "gemini_opinion": gemini_msg,
        "agreed_action": actions,
    }
