# 🏭 Dual-AI Smart Factory 자율 관제 시뮬레이션 명세서

이 문서는 **GitHub Actions**를 실행 엔진으로 사용하고, **ChatGPT(생산·효율 총괄 매니저)**와 **Google Gemini(설비 안전·품질 감사관)**가 3턴 토론을 거쳐 가상 스마트 팩토리를 제어하는 전체 시스템 설계도 및 소스 코드입니다.

---

## 1. ChatGPT 역할 및 운영 지침 (System Context)

당신(ChatGPT)은 이 스마트 팩토리의 **'생산·효율 총괄 매니저(Operations Manager)'**입니다.
- **핵심 목표**: 시간당 생산량 극대화, 납기 준수, 설비 가동률 최적화.
- **협업 구조**:
  - 매 사이클 `factory_sim.py`가 물리 연산을 수행해 `state.json`을 갱신합니다.
  - **1턴(ChatGPT)**: 현재 데이터를 기반으로 생산량 극대화 관점의 `conveyor_speed`(0~100), `cooling_power`(0~100), `auto_restock`(true/false) 초안을 제시합니다.
  - **2턴(Gemini)**: 과열(82°C 이상 위험), 불량률 급증(8% 이상 위험), 원자재 고갈(80 미만 위험) 리스크를 지적하고 안전 마진을 반영한 수정안을 역제안합니다.
  - **3턴(최종 합의)**: 두 AI의 의견을 절충한 최종 JSON 파라미터를 확정해 `config.json`에 반영하고 `logs/council_history.md`에 회의록을 기록합니다.
- **장애 대응 라이프사이클**:
  - `CRITICAL` 상태 발생 시 GitHub Issue(`🚨 [CRITICAL]`)를 자동 생성하고 댓글로 복구 절차를 논의합니다.
  - `NORMAL` 상태로 회복되면 열려 있던 `CRITICAL` Issue에 복구 보고 댓글을 남기고 자동으로 Issue를 종료(`closed`)합니다.

---

## 2. 디렉토리 구조

```text
ai-smart-factory/
├── .github/
│   └── workflows/
│       └── factory_loop.yml   # 30분 주기 자동 실행 및 Git 커밋 파이프라인
├── config.json                # AI 위원회가 제어하는 설비 설정값
├── state.json                 # 공장 실시간 센서 및 생산 상태 데이터
├── requirements.txt           # Python 패키지 의존성
├── factory_sim.py             # 물리 연산 및 돌발 장애 시뮬레이터
├── ai_council.py              # ChatGPT ↔ Gemini 3턴 토론 및 Issue 관리 엔진
├── index.html                 # GitHub Pages 실시간 관제 대시보드
└── logs/
    └── council_history.md     # AI 위원회 누적 토론 회의록 (자동 생성)
```

---

## 3. 전체 소스 코드

### 3.1 `requirements.txt`
```text
openai>=1.50.0
google-genai>=1.0.0
pydantic>=2.0.0
requests>=2.31.0
```

### 3.2 `config.json`
```json
{
  "conveyor_speed": 75,
  "cooling_power": 50,
  "auto_restock": false
}
```

### 3.3 `state.json`
```json
{
  "cycle": 0,
  "timestamp": "2026-11-25T00:00:00Z",
  "temperature": 45.0,
  "output_units": 0,
  "total_output": 0,
  "defect_rate": 1.0,
  "raw_material": 500,
  "status": "NORMAL",
  "last_event": "시스템 초기화 완료"
}
```

### 3.4 `factory_sim.py`
```python
import json
import random
from datetime import datetime, timezone

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run_simulation() -> None:
    config = load_json(CONFIG_FILE)
    state = load_json(STATE_FILE)

    cycle = state["cycle"] + 1
    speed = config["conveyor_speed"]       # 0 ~ 100
    cooling = config["cooling_power"]      # 0 ~ 100
    auto_restock = config["auto_restock"]

    raw_material = state["raw_material"]
    temp = state["temperature"]
    event_log = "정상 가동"

    # 1. 자동 발주 처리 (이전 턴에 AI가 승인한 경우 원자재 입고)
    if auto_restock:
        raw_material += 400
        event_log = "긴급 원자재 400단위 입고 완료"
        config["auto_restock"] = False
        save_json(CONFIG_FILE, config)

    # 2. 돌발 이벤트 발생 (25% 확률)
    roll = random.random()
    heat_spike = 0.0
    defect_spike = 0.0

    if roll < 0.10:
        heat_spike = random.uniform(8.0, 15.0)
        event_log = "[돌발] 냉각수 펌프 일시 효율 저하로 온도 급상승"
    elif roll < 0.18:
        defect_spike = random.uniform(2.5, 5.0)
        event_log = "[돌발] 원자재 Lot 편차로 인한 미세 불량 증가"
    elif roll < 0.25:
        raw_material = max(0, raw_material - 80)
        event_log = "[돌발] 포장 라인 걸림으로 원자재 80단위 폐기"

    # 3. 물리 연산 (생산량, 원자재 소모, 온도, 불량률)
    max_possible_units = int(speed * 1.5)
    actual_units = min(max_possible_units, raw_material)
    raw_material -= actual_units

    # 속도가 빠를수록 발열 증가, 냉각 출력이 높을수록 냉각 증가 (외기 온도 25도 기준)
    heat_gain = (speed * 0.22) + heat_spike
    cooling_loss = (cooling * 0.20) + ((temp - 25.0) * 0.12)
    temp = round(max(25.0, temp + heat_gain - cooling_loss), 1)

    # 온도가 70도를 넘거나 속도가 85를 넘으면 불량률 급증
    base_defect = 0.8 + max(0.0, (temp - 68.0) * 0.25) + max(0.0, (speed - 85) * 0.15)
    defect_rate = round(min(100.0, base_defect + defect_spike), 2)

    # 4. 공장 상태 판정
    if temp >= 82.0 or raw_material < 80 or defect_rate >= 8.0:
        status = "CRITICAL"
    elif temp >= 70.0 or raw_material < 200 or defect_rate >= 4.0:
        status = "WARNING"
    else:
        status = "NORMAL"

    new_state = {
        "cycle": cycle,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "temperature": temp,
        "output_units": actual_units,
        "total_output": state["total_output"] + actual_units,
        "defect_rate": defect_rate,
        "raw_material": raw_material,
        "status": status,
        "last_event": event_log,
    }

    save_json(STATE_FILE, new_state)
    print(f"[Cycle {cycle}] 상태 갱신 완료: {status} | 온도 {temp}°C | 생산 {actual_units} | 재고 {raw_material}")


if __name__ == "__main__":
    run_simulation()
```

### 3.5 `ai_council.py`
```python
import json
import os
import requests
from google import genai
from google.genai import types
from openai import OpenAI
from pydantic import BaseModel

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
LOG_FILE = "logs/council_history.md"
CRITICAL_PREFIX = "🚨 [CRITICAL]"


class FactoryDecision(BaseModel):
    conveyor_speed: int
    cooling_power: int
    auto_restock: bool
    consensus_summary: str


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_github_context() -> tuple[str | None, str | None, dict]:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return token, repo, headers


def post_github_comment(repo: str, issue_number: int, headers: dict, body: str) -> None:
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    resp = requests.post(url, headers=headers, json={"body": body}, timeout=15)
    resp.raise_for_status()


def handle_critical_issue(
    state: dict,
    final_config: dict,
    openai_client: OpenAI,
    gemini_client: genai.Client,
) -> None:
    github_token, repo, headers = get_github_context()
    if not github_token or not repo:
        print("[경고] GITHUB_TOKEN 또는 GITHUB_REPOSITORY 환경변수가 없어 Issue 생성을 건너뜁니다.")
        return

    # 1. 긴급 장애 GitHub Issue 생성
    issue_title = f"{CRITICAL_PREFIX} Cycle {state['cycle']} 설비 임계치 초과 - 긴급 복구 논의"
    issue_body = (
        f"## ⚠️ 스마트 팩토리 긴급 경보 발생\n\n"
        f"- **발생 사이클**: Cycle `{state['cycle']}` (`{state['timestamp']}`)\n"
        f"- **트리거 이벤트**: {state['last_event']}\n\n"
        f"### 📊 현재 텔레메트리 수치\n"
        f"| 항목 | 현재 수치 | 임계 기준 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| **설비 온도** | `{state['temperature']}°C` | 위험: 82.0°C 이상 |\n"
        f"| **불량률** | `{state['defect_rate']}%` | 위험: 8.0% 이상 |\n"
        f"| **원자재 잔고** | `{state['raw_material']} 단위` | 위험: 80 단위 미만 |\n\n"
        f"### ⚙️ 위원회 긴급 적용 설정\n"
        f"- 컨베이어 속도: `{final_config['conveyor_speed']}` / 냉각 출력: `{final_config['cooling_power']}` / "
        f"자동발주: `{final_config['auto_restock']}`\n\n"
        f"---\n"
        f"*아래 댓글에서 ChatGPT(생산 총괄)와 Gemini(안전 감사관)가 현장 복구 절차를 논의합니다.*"
    )

    create_url = f"https://api.github.com/repos/{repo}/issues"
    issue_resp = requests.post(
        create_url,
        headers=headers,
        json={"title": issue_title, "body": issue_body},
        timeout=15,
    )
    issue_resp.raise_for_status()
    issue_number = issue_resp.json()["number"]
    print(f"[CRITICAL] GitHub Issue #{issue_number} 생성 완료")

    # 2. 댓글 1: ChatGPT의 원인 분석 및 긴급 복구 프로토콜 제안
    gpt_emergency = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 스마트 팩토리 '생산·복구 지휘관(ChatGPT)'입니다. "
                    "CRITICAL 장애 상황에서 가동 중단 시간을 최소화하면서 설비를 정상화하기 위한 "
                    "단계별 긴급 복구 조치(1~3단계)를 마크다운 형식으로 간결하게 작성하세요."
                ),
            },
            {"role": "user", "content": f"장애 상황 요약:\n{issue_body}"},
        ],
    )
    gpt_comment_text = gpt_emergency.choices[0].message.content.strip()
    post_github_comment(
        repo,
        issue_number,
        headers,
        f"### 🤖 ChatGPT (생산·복구 지휘관) — 1차 긴급 복구안\n\n{gpt_comment_text}",
    )

    # 3. 댓글 2: Gemini의 2차 사고 예방 검증 및 현장 안전 체크리스트
    gemini_emergency = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f"[장애 현황]\n{issue_body}\n\n"
            f"[ChatGPT의 1차 복구안]\n{gpt_comment_text}\n\n"
            "위 복구안을 실행할 때 발생할 수 있는 2차 사고(열충격, 재가동 시 불량 연쇄 발생, 안전사고)를 "
            "경고하고, 현장 작업자 및 시스템이 재가동 전 반드시 확인해야 할 최종 체크리스트(체크박스 형식)를 작성하세요."
        ),
        config=types.GenerateContentConfig(
            system_instruction=(
                "당신은 스마트 팩토리 '설비 안전·품질 감사관(Gemini)'입니다. "
                "성급한 라인 재가동을 막고 2차 설비 파손 및 안전사고를 차단하는 검증 역할을 수행합니다."
            )
        ),
    )
    gemini_comment_text = gemini_emergency.text.strip()
    post_github_comment(
        repo,
        issue_number,
        headers,
        f"### ✨ Gemini (설비 안전·품질 감사관) — 안전 검증 및 재가동 체크리스트\n\n{gemini_comment_text}",
    )
    print(f"[CRITICAL] Issue #{issue_number}에 듀얼 AI 복구 토론 댓글 등록 완료")


def resolve_critical_issues(
    state: dict,
    final_config: dict,
    gemini_client: genai.Client,
) -> None:
    github_token, repo, headers = get_github_context()
    if not github_token or not repo:
        return

    # 1. 현재 열려 있는 CRITICAL 이슈 조회
    list_url = f"https://api.github.com/repos/{repo}/issues"
    resp = requests.get(
        list_url,
        headers=headers,
        params={"state": "open", "per_page": 30},
        timeout=15,
    )
    resp.raise_for_status()

    open_critical_issues = [
        issue
        for issue in resp.json()
        if "pull_request" not in issue and issue["title"].startswith(CRITICAL_PREFIX)
    ]

    if not open_critical_issues:
        return

    # 2. 안전·품질 감사관(Gemini)의 최종 정상화 승인 소견 생성
    signoff_resp = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f"공장이 CRITICAL 상태에서 NORMAL 등급으로 완전히 회복되었습니다.\n"
            f"- 복구 시점: Cycle {state['cycle']} ({state['timestamp']})\n"
            f"- 현재 온도: {state['temperature']}°C / 불량률: {state['defect_rate']}% / "
            f"원자재 잔고: {state['raw_material']}단위\n"
            f"- 적용 설정: 속도 {final_config['conveyor_speed']}, 냉각 {final_config['cooling_power']}\n\n"
            "설비 안전·품질 감사관으로서 정상 가동 복귀를 최종 승인하는 2~3문장의 공식 클로징 소견을 작성하세요."
        ),
        config=types.GenerateContentConfig(
            system_instruction="당신은 스마트 팩토리 '설비 안전·품질 감사관(Gemini)'입니다. 간결하고 신뢰감 있는 어조로 복구 승인을 기록하세요."
        ),
    )
    signoff_note = signoff_resp.text.strip()

    # 3. 각 미해결 이슈에 복구 완료 댓글 작성 후 Close 처리
    for issue in open_critical_issues:
        issue_number = issue["number"]
        recovery_comment = (
            f"## ✅ 설비 정상화 완료 및 장애 이슈 자동 종료\n\n"
            f"- **복구 확인 사이클**: Cycle `{state['cycle']}` (`{state['timestamp']}`)\n"
            f"- **현재 상태 등급**: `NORMAL`\n\n"
            f"### 📊 회복된 텔레메트리 수치\n"
            f"| 항목 | 복구 수치 | 정상 기준 |\n"
            f"| :--- | :--- | :--- |\n"
            f"| **설비 온도** | `{state['temperature']}°C` | 70.0°C 미만 |\n"
            f"| **불량률** | `{state['defect_rate']}%` | 4.0% 미만 |\n"
            f"| **원자재 잔고** | `{state['raw_material']} 단위` | 200 단위 이상 |\n\n"
            f"### ✨ Gemini (설비 안전·품질 감사관) 최종 승인 소견\n"
            f"> {signoff_note}\n\n"
            f"*모든 지표가 정상 범위로 복귀하여 본 장애 이슈를 자동으로 닫습니다.*"
        )

        post_github_comment(repo, issue_number, headers, recovery_comment)

        close_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
        close_resp = requests.patch(
            close_url,
            headers=headers,
            json={"state": "closed", "state_reason": "completed"},
            timeout=15,
        )
        close_resp.raise_for_status()
        print(f"[NORMAL 복구] GitHub Issue #{issue_number} 복구 댓글 등록 및 자동 종료 완료")


def run_council() -> None:
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    state = load_json(STATE_FILE)
    config = load_json(CONFIG_FILE)

    context_summary = (
        f"현재 사이클: {state['cycle']} ({state['timestamp']})\n"
        f"- 공장 상태 등급: {state['status']}\n"
        f"- 최근 이벤트: {state['last_event']}\n"
        f"- 설비 온도: {state['temperature']}°C (안전 권장치: 70°C 이하, 위험: 82°C 이상)\n"
        f"- 금번 생산량: {state['output_units']}개 (누적: {state['total_output']}개)\n"
        f"- 불량률: {state['defect_rate']}% (목표: 3.0% 이하)\n"
        f"- 원자재 잔고: {state['raw_material']}단위 (200 미만 시 발주 검토 필요)\n"
        f"- 현재 설정(config): 컨베이어 속도={config['conveyor_speed']}, "
        f"냉각 출력={config['cooling_power']}, 자동발주={config['auto_restock']}"
    )

    # Turn 1: ChatGPT 초안 제시
    gpt_turn1_resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 스마트 팩토리의 '생산·효율 총괄 매니저(ChatGPT)'입니다. "
                    "시간당 생산량 극대화와 납기 준수가 최우선 목표입니다. "
                    "현재 공장 데이터를 보고 다음 사이클의 컨베이어 속도(0~100), 냉각 출력(0~100), "
                    "원자재 자동발주(true/false) 초안을 한국어 3~4문장으로 간결하게 제안하세요."
                ),
            },
            {"role": "user", "content": context_summary},
        ],
    )
    turn1_text = gpt_turn1_resp.choices[0].message.content.strip()

    # Turn 2: Gemini 검증 및 반론
    gemini_prompt = (
        f"[현재 공장 데이터]\n{context_summary}\n\n"
        f"[생산 총괄(ChatGPT)의 1차 제안]\n{turn1_text}\n\n"
        "위 제안에 대해 과열, 불량률 급증, 원자재 고갈, 전력 낭비 관점에서 리스크를 날카롭게 지적하고 "
        "안전 마진을 확보한 수정 수치(컨베이어 속도, 냉각 출력, 자동발주 여부)를 한국어 3~4문장으로 역제안하세요."
    )
    gemini_turn2_resp = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=gemini_prompt,
        config=types.GenerateContentConfig(
            system_instruction=(
                "당신은 스마트 팩토리의 '설비 안전·품질 감사관(Gemini)'입니다. "
                "설비 수명 보호와 불량률 최소화가 최우선 목표이며, 공격적인 생산 목표에 보수적으로 제동을 겁니다."
            )
        ),
    )
    turn2_text = gemini_turn2_resp.text.strip()

    # Turn 3: 최종 합의 및 JSON 구조화 출력
    gpt_turn3_resp = openai_client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 스마트 팩토리 운영 위원회 의장입니다. "
                    "생산 총괄(ChatGPT)과 안전 감사관(Gemini)의 토론을 종합해 최적의 균형점을 찾고 "
                    "최종 제어 파라미터를 JSON으로 확정하세요. "
                    "conveyor_speed와 cooling_power는 0에서 100 사이의 정수여야 합니다."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"[공장 데이터]\n{context_summary}\n\n"
                    f"[1턴 - ChatGPT 제안]\n{turn1_text}\n\n"
                    f"[2턴 - Gemini 반론 및 수정안]\n{turn2_text}\n\n"
                    "두 의견을 절충한 최종 설정값과 1~2문장의 합의 요약(consensus_summary)을 출력하세요."
                ),
            },
        ],
        response_format=FactoryDecision,
    )
    decision: FactoryDecision = gpt_turn3_resp.choices[0].message.parsed

    final_config = {
        "conveyor_speed": max(0, min(100, decision.conveyor_speed)),
        "cooling_power": max(0, min(100, decision.cooling_power)),
        "auto_restock": decision.auto_restock,
    }
    save_json(CONFIG_FILE, final_config)

    # 회의록 누적 기록
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("# 🏭 듀얼 AI 스마트 팩토리 운영 위원회 회의록\n\n")

    log_entry = (
        f"## Cycle {state['cycle']} (`{state['timestamp']}`) — 상태: **{state['status']}**\n"
        f"- **센서 요약**: 온도 `{state['temperature']}°C` | 생산 `{state['output_units']}개` | "
        f"불량률 `{state['defect_rate']}%` | 재고 `{state['raw_material']}` | 이벤트: {state['last_event']}\n\n"
        f"1. **🤖 ChatGPT (생산 총괄)**: {turn1_text}\n"
        f"2. **✨ Gemini (안전·품질 감사)**: {turn2_text}\n"
        f"3. **🤝 최종 합의**: {decision.consensus_summary}\n"
        f"   - **적용된 Config**: `속도 {final_config['conveyor_speed']}` / "
        f"`냉각 {final_config['cooling_power']}` / `자동발주 {final_config['auto_restock']}`\n\n"
        f"---\n\n"
    )

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_entry)

    # 상태별 GitHub Issue 라이프사이클 처리
    if state["status"] == "CRITICAL":
        handle_critical_issue(state, final_config, openai_client, gemini_client)
    elif state["status"] == "NORMAL":
        resolve_critical_issues(state, final_config, gemini_client)

    print(f"[Cycle {state['cycle']}] AI 합의 완료 -> {final_config}")


if __name__ == "__main__":
    run_council()
```

### 3.6 `.github/workflows/factory_loop.yml`
```yaml
name: Dual-AI Smart Factory Simulation

on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:

permissions:
  contents: write
  issues: write

jobs:
  run-factory-cycle:
    runs-on: ubuntu-latest

    steps:
      - name: 1. Checkout Repository
        uses: actions/checkout@v4

      - name: 2. Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: 3. Install Dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: 4. Run Virtual Factory Simulation
        run: python factory_sim.py

      - name: 5. Run Dual-AI Council (ChatGPT & Gemini)
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: python ai_council.py

      - name: 6. Commit & Push Updated State, Config, and Logs
        run: |
          git config --local user.email "github-actions[bot]@users.noreply.github.com"
          git config --local user.name "Dual-AI Factory Bot"
          git add state.json config.json logs/council_history.md
          if ! git diff --staged --quiet; then
            CYCLE=$(python -c "import json; print(json.load(open('state.json'))['cycle'])")
            STATUS=$(python -c "import json; print(json.load(open('state.json'))['status'])")
            git commit -m "🏭 [Cycle ${CYCLE}] 상태: ${STATUS} - AI 위원회 설정 갱신"
            git push
          else
            echo "변경 사항이 없습니다."
          fi
```

### 3.7 `index.html` (GitHub Pages 실시간 대시보드)
```html
<!DOCTYPE html>
<html lang="ko" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Dual-AI Smart Factory 관제 대시보드</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body { background-color: #0b1120; color: #e2e8f0; font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
    .card { background-color: #131c31; border: 1px solid #1e293b; border-radius: 0.75rem; }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: #0f172a; }
    ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
  </style>
</head>
<body class="min-h-screen p-4 md:p-8">
  <div class="max-w-7xl mx-auto space-y-6">
    <header class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-800 pb-5">
      <div>
        <div class="flex items-center gap-3">
          <span class="text-2xl">🏭</span>
          <h1 class="text-2xl font-bold tracking-tight text-white">Dual-AI Smart Factory 관제 센터</h1>
          <span id="status-badge" class="px-3 py-1 text-xs font-bold rounded-full bg-slate-800 text-slate-300">LOADING...</span>
        </div>
        <p class="text-sm text-slate-400 mt-1">
          ChatGPT(생산 총괄) &amp; Gemini(안전·품질 감사) 실시간 자율 제어 시뮬레이션
        </p>
      </div>
      <div class="flex items-center gap-3">
        <span id="last-updated" class="text-xs text-slate-400">마지막 갱신: -</span>
        <button onclick="loadDashboard()" class="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium rounded-lg transition">
          새로고침
        </button>
      </div>
    </header>

    <section class="grid grid-cols-2 lg:grid-cols-5 gap-4">
      <div class="card p-4">
        <p class="text-xs text-slate-400">현재 사이클 / 누적 생산</p>
        <p class="text-2xl font-bold text-white mt-1"><span id="kpi-cycle">-</span> <span class="text-sm font-normal text-slate-400">Cycle</span></p>
        <p class="text-xs text-emerald-400 mt-2">누적 생산: <span id="kpi-total-output">-</span> 개</p>
      </div>
      <div class="card p-4">
        <p class="text-xs text-slate-400">설비 온도 (임계 82°C)</p>
        <p id="kpi-temp" class="text-2xl font-bold text-white mt-1">- °C</p>
        <p class="text-xs text-sky-400 mt-2">냉각 출력: <span id="kpi-cooling">-</span>%</p>
      </div>
      <div class="card p-4">
        <p class="text-xs text-slate-400">금번 사이클 생산량</p>
        <p id="kpi-output" class="text-2xl font-bold text-white mt-1">- 개</p>
        <p class="text-xs text-indigo-400 mt-2">컨베이어 속도: <span id="kpi-speed">-</span>%</p>
      </div>
      <div class="card p-4">
        <p class="text-xs text-slate-400">공정 불량률 (목표 ≤ 3%)</p>
        <p id="kpi-defect" class="text-2xl font-bold text-white mt-1">- %</p>
        <p id="kpi-event" class="text-xs text-amber-400 mt-2 truncate" title="">-</p>
      </div>
      <div class="card p-4 col-span-2 lg:col-span-1">
        <p class="text-xs text-slate-400">원자재 잔고</p>
        <p id="kpi-material" class="text-2xl font-bold text-white mt-1">- 단위</p>
        <p class="text-xs text-slate-400 mt-2">자동발주 예약: <span id="kpi-restock" class="font-semibold">-</span></p>
      </div>
    </section>

    <section class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div class="card p-5">
        <h2 class="text-sm font-semibold text-slate-300 mb-4">🌡️ 사이클별 설비 온도 (°C) 및 불량률 (%) 추이</h2>
        <div class="h-64"><canvas id="tempChart"></canvas></div>
      </div>
      <div class="card p-5">
        <h2 class="text-sm font-semibold text-slate-300 mb-4">📦 사이클별 생산량 (개) 및 원자재 잔고 추이</h2>
        <div class="h-64"><canvas id="outputChart"></canvas></div>
      </div>
    </section>

    <section class="card p-6">
      <div class="flex items-center justify-between mb-4">
        <h2 class="text-base font-bold text-white">💬 듀얼 AI 운영 위원회 토론 회의록 (최신순)</h2>
        <span id="log-count" class="text-xs text-slate-400">총 0건의 회의 기록</span>
      </div>
      <div id="council-logs" class="space-y-4 max-h-[680px] overflow-y-auto pr-2">
        <p class="text-sm text-slate-400">데이터를 불러오는 중입니다...</p>
      </div>
    </section>
  </div>

  <script>
    let tempChartInstance = null;
    let outputChartInstance = null;

    function getStatusStyle(status) {
      if (status === 'CRITICAL') return 'bg-rose-500/20 text-rose-400 border border-rose-500/40';
      if (status === 'WARNING') return 'bg-amber-500/20 text-amber-400 border border-amber-500/40';
      return 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/40';
    }

    function parseCouncilMarkdown(mdText) {
      const blocks = mdText.split(/^## Cycle /m).slice(1);
      const parsed = [];
      for (const block of blocks) {
        const headerMatch = block.match(/^(\d+)\s*\(`([^`]+)`\)\s*—\s*상태:\s*\*\*([A-Z]+)\*\*/);
        const sensorMatch = block.match(/온도 `([\d.]+)°C` \| 생산 `(\d+)개` \| 불량률 `([\d.]+)%` \| 재고 `(\d+)` \| 이벤트: (.+)/);
        const gptMatch = block.match(/1\.\s*\*\*🤖 ChatGPT \(생산 총괄\)\*\*:\s*(.+)/);
        const geminiMatch = block.match(/2\.\s*\*\*✨ Gemini \(안전·품질 감사\)\*\*:\s*(.+)/);
        const consensusMatch = block.match(/3\.\s*\*\*🤝 최종 합의\*\*:\s*(.+)/);
        const configMatch = block.match(/\*\*적용된 Config\*\*:\s*(.+)/);
        if (headerMatch && sensorMatch) {
          parsed.push({
            cycle: Number(headerMatch[1]),
            timestamp: headerMatch[2],
            status: headerMatch[3],
            temperature: Number(sensorMatch[1]),
            output: Number(sensorMatch[2]),
            defectRate: Number(sensorMatch[3]),
            material: Number(sensorMatch[4]),
            event: sensorMatch[5].trim(),
            gpt: gptMatch ? gptMatch[1].trim() : '',
            gemini: geminiMatch ? geminiMatch[1].trim() : '',
            consensus: consensusMatch ? consensusMatch[1].trim() : '',
            appliedConfig: configMatch ? configMatch[1].trim() : ''
          });
        }
      }
      return parsed;
    }

    function renderCharts(history) {
      const labels = history.map(d => `C${d.cycle}`);
      const commonOptions = {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { color: '#cbd5e1', font: { size: 12 } } } },
        scales: {
          x: { ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } },
          y: { ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } },
          y1: { position: 'right', ticks: { color: '#94a3b8' }, grid: { drawOnChartArea: false } }
        }
      };

      if (tempChartInstance) tempChartInstance.destroy();
      tempChartInstance = new Chart(document.getElementById('tempChart'), {
        type: 'line',
        data: {
          labels,
          datasets: [
            { label: '설비 온도 (°C)', data: history.map(d => d.temperature), borderColor: '#f43f5e', backgroundColor: 'rgba(244, 63, 94, 0.15)', fill: true, tension: 0.3, yAxisID: 'y' },
            { label: '불량률 (%)', data: history.map(d => d.defectRate), borderColor: '#fbbf24', backgroundColor: 'rgba(251, 191, 36, 0.15)', borderDash: [4, 4], tension: 0.3, yAxisID: 'y1' }
          ]
        },
        options: commonOptions
      });

      if (outputChartInstance) outputChartInstance.destroy();
      outputChartInstance = new Chart(document.getElementById('outputChart'), {
        type: 'bar',
        data: {
          labels,
          datasets: [
            { label: '생산량 (개)', data: history.map(d => d.output), backgroundColor: 'rgba(99, 102, 241, 0.75)', borderRadius: 4, yAxisID: 'y' },
            { type: 'line', label: '원자재 잔고', data: history.map(d => d.material), borderColor: '#34d399', tension: 0.3, yAxisID: 'y1' }
          ]
        },
        options: commonOptions
      });
    }

    function renderLogs(history) {
      const container = document.getElementById('council-logs');
      document.getElementById('log-count').textContent = `총 ${history.length}건의 회의 기록`;
      if (history.length === 0) {
        container.innerHTML = `<p class="text-sm text-slate-400">아직 기록된 AI 토론 내역이 없습니다.</p>`;
        return;
      }
      container.innerHTML = [...history].reverse().map(item => `
        <article class="bg-slate-900/80 border border-slate-800 rounded-xl p-5 space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
            <div class="flex items-center gap-2.5">
              <span class="font-bold text-white">Cycle #${item.cycle}</span>
              <span class="px-2.5 py-0.5 text-xs font-semibold rounded-full ${getStatusStyle(item.status)}">${item.status}</span>
              <span class="text-xs text-slate-400">${item.timestamp}</span>
            </div>
            <div class="text-xs text-slate-300 bg-slate-800 px-3 py-1 rounded-md">
              온도 <b>${item.temperature}°C</b> · 생산 <b>${item.output}개</b> · 불량 <b>${item.defectRate}%</b> · 재고 <b>${item.material}</b>
            </div>
          </div>
          <p class="text-xs text-amber-300/90">📌 이벤트: ${item.event}</p>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1">
            <div class="bg-emerald-950/30 border border-emerald-800/40 rounded-lg p-3.5">
              <p class="text-xs font-bold text-emerald-400 mb-1">🤖 ChatGPT (생산·효율 총괄)</p>
              <p class="text-sm text-slate-200 leading-relaxed">${item.gpt}</p>
            </div>
            <div class="bg-sky-950/30 border border-sky-800/40 rounded-lg p-3.5">
              <p class="text-xs font-bold text-sky-400 mb-1">✨ Gemini (설비 안전·품질 감사)</p>
              <p class="text-sm text-slate-200 leading-relaxed">${item.gemini}</p>
            </div>
          </div>
          <div class="bg-indigo-950/30 border border-indigo-800/40 rounded-lg p-3.5 flex flex-col md:flex-row md:items-center md:justify-between gap-2">
            <div>
              <span class="text-xs font-bold text-indigo-300">🤝 최종 합의: </span>
              <span class="text-sm text-slate-100">${item.consensus}</span>
            </div>
            <span class="text-xs font-mono text-indigo-300 bg-indigo-950 px-2.5 py-1 rounded shrink-0">${item.appliedConfig}</span>
          </div>
        </article>
      `).join('');
    }

    async function loadDashboard() {
      const cacheBuster = `?t=${Date.now()}`;
      try {
        const [stateRes, configRes, logRes] = await Promise.all([
          fetch(`./state.json${cacheBuster}`),
          fetch(`./config.json${cacheBuster}`),
          fetch(`./logs/council_history.md${cacheBuster}`)
        ]);
        if (stateRes.ok) {
          const state = await stateRes.json();
          document.getElementById('kpi-cycle').textContent = state.cycle;
          document.getElementById('kpi-total-output').textContent = state.total_output.toLocaleString();
          document.getElementById('kpi-temp').textContent = `${state.temperature} °C`;
          document.getElementById('kpi-output').textContent = `${state.output_units} 개`;
          document.getElementById('kpi-defect').textContent = `${state.defect_rate} %`;
          document.getElementById('kpi-material').textContent = `${state.raw_material} 단위`;
          document.getElementById('kpi-event').textContent = state.last_event;
          document.getElementById('kpi-event').title = state.last_event;
          document.getElementById('last-updated').textContent = `데이터 기준: ${state.timestamp}`;
          const badge = document.getElementById('status-badge');
          badge.textContent = state.status;
          badge.className = `px-3 py-1 text-xs font-bold rounded-full ${getStatusStyle(state.status)}`;
        }
        if (configRes.ok) {
          const config = await configRes.json();
          document.getElementById('kpi-cooling').textContent = config.cooling_power;
          document.getElementById('kpi-speed').textContent = config.conveyor_speed;
          document.getElementById('kpi-restock').textContent = config.auto_restock ? 'ON (입고 대기)' : 'OFF';
        }
        if (logRes.ok) {
          const history = parseCouncilMarkdown(await logRes.text());
          renderCharts(history);
          renderLogs(history);
        }
      } catch (err) {
        console.error('대시보드 데이터 로드 실패:', err);
      }
    }
    loadDashboard();
  </script>
</body>
</html>
```

---

## 4. 빠른 배포 체크리스트

1. **GitHub Secrets 등록**: `Settings > Secrets and variables > Actions`에 `OPENAI_API_KEY`, `GEMINI_API_KEY` 등록.
2. **GitHub Pages 활성화**: `Settings > Pages`에서 `main` 브랜치 `/ (root)` 경로로 배포 설정.
3. **워크플로우 실행**: `Actions` 탭에서 `Dual-AI Smart Factory Simulation`을 수동 실행(`Run workflow`)하여 초기 동작 확인.