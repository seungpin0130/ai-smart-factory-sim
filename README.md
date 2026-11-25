# 듀얼 AI 스마트 팩토리

GitHub를 상태 저장소와 실행 엔진으로 쓰는 **가상** 공장입니다. 한 사이클마다 시뮬레이션 → ChatGPT 생산안 → Gemini 안전 감사 → ChatGPT 최종안 → SOP 제어 보정 → Git 커밋 → 장애 Issue 처리를 수행합니다.

## 바로 실행

Python 3.11 이상에서:

```bash
python -m pip install -r requirements.txt
python run_cycle.py --mode mock --seed 42
python -m http.server 8000
```

브라우저에서 `http://localhost:8000`을 엽니다. 모의 모드는 API 키가 필요 없으며 실제 AI가 발언한 것처럼 표시하지 않습니다. 테스트: `python -m unittest discover -s tests -v`.

## GitHub에서 시작

1. 저장소를 생성하고 이 프로젝트 전체를 기본 브랜치에 커밋합니다. `.github/workflows/` 폴더도 포함합니다.
2. **Settings → Secrets and variables → Actions → Secrets**에 `OPENAI_API_KEY`, `GEMINI_API_KEY`를 등록합니다. 키를 코드나 채팅에 적지 마세요. `GITHUB_TOKEN`은 Actions가 제공합니다.
3. **Actions → Dual-AI Factory Loop → Run workflow**에서 `mock`으로 첫 실행을 확인합니다. 모의 실행은 장애 Issue를 만들거나 닫지 않습니다.
4. 같은 화면에서 `live`를 선택하면 실제 API를 호출합니다. 키 누락·거부·타임아웃·잘못된 JSON이면 그 사이클은 `fallback`으로 기록하고 규칙 기반으로 제어합니다.
5. **Variables**에 `FACTORY_ENABLED=true`를 설정하면 15분마다 실제 모드가 실행됩니다. 중지는 `false`로 변경합니다. 최초에는 예약 실행이 꺼져 있습니다.

설정 가능한 Variables:

| 이름 | 기본값 | 의미 |
|---|---|---|
| `FACTORY_ENABLED` | `false` | 예약 실행 허용 |
| `OPENAI_MODEL` | `gpt-4o-mini` | Structured Outputs 지원 모델 |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Google GenAI SDK에서 사용할 모델 |
| `FACTORY_PAGES_ENABLED` | `false` | 선택적 Pages 배포 허용 |

실제 모드는 정상 사이클당 OpenAI 2회, Gemini 1회를 호출합니다. 15분 간격이면 하루 최대 96사이클, 기본 호출 288회이며 SDK 재시도 시 늘어날 수 있습니다. API와 Actions는 계정 요금·사용 한도에 따릅니다. 모델 접근 권한/지원 여부는 사용하는 계정에서 확인하고 Variables로 변경할 수 있습니다.

GitHub Actions 예약은 정시성을 보장하지 않으며 지연·누락될 수 있습니다. 이 프로젝트는 시간을 압축한 이산 사이클 시뮬레이션이며 24시간 실시간 물리 엔진이 아닙니다. cron은 UTC의 매시 7/22/37/52분입니다. 예약은 기본 브랜치의 workflow만 실행합니다. 보호 브랜치가 봇 직접 푸시를 차단하면 전용 실행 저장소 또는 적합한 브랜치 정책이 필요합니다. 워크플로우 간 단일 writer를 적용하고 충돌 시 강제 push하지 않습니다.

## 제어 규칙

첨부 명세서의 분류 기준을 사용합니다. 본문의 75°C 감속 조건도 추가 보호 규칙으로 적용합니다.

| 조건 | 분류/조치 |
|---|---|
| 온도 ≥82°C 또는 불량률 ≥8% | CRITICAL, 속도 0 / 냉각 100 |
| 재고 <80 | CRITICAL, 긴급 발주 |
| 온도 ≥75°C | 속도 최대 50 / 냉각 최소 90 |
| 온도 ≥70°C 또는 불량률 ≥4% | WARNING, 속도 최대 65 / 냉각 최소 75 |
| 재고 <200 | WARNING |
| 다음 생산 후 예상 재고 <200 | 다음 사이클 원자재 400단위 1회 입고 예약 |

생산 목표 120개/사이클, 불량 목표 ≤3%입니다. 생산량은 불량을 포함한 총생산량입니다. 입력과 모델 응답은 엄격한 Pydantic 타입·범위 검증을 거치며 숫자 문자열·범위 밖 값·추가 필드는 거부합니다. Gemini 권고는 속도 상한, 냉각 하한, 발주 요청으로 적용합니다. 이 보정은 **모델의 합의와 별도로** 회의록에 표시됩니다. 최종 제어는 고정 SOP가 우선합니다. API 실패는 외부 응답을 임의로 복구하지 않고 명시적인 대체 모드로 기록합니다.

`auto_restock`은 지속 토글이 아닌 **다음 사이클에 소비하는 1회 발주**입니다. 원자재 1단위는 제품 1개에 대응하며 부족하면 생산량이 줄어듭니다. 냉각 이상/원자재 편차/자재 손실은 확률적 일시 이벤트입니다. 실제 설비나 구매 시스템에는 연결하지 않습니다.

## 기록과 장애 처리

- `state.json`: 현재 센서와 누적 생산량.
- `config.json`: 다음 사이클 적용 제어값.
- `logs/cycles/00000001.json`: 사이클별 전체 공급자 응답과 실제 적용값, 보정 사유.
- `logs/history.json`, `logs/council_history.md`: 최근 120회. JSON 원장이 기준입니다.
- `dashboard.json`: 상태·설정·최근 이력을 담은 단일 화면용 스냅샷.
- `logs/last_decision.json`: 최근 의사결정, Issue 처리 입력.

장애 Issue에는 봇 소유 표시를 넣습니다. CRITICAL 또는 AI 점검 요청 시 기존 열린 Issue를 재사용하고 ChatGPT/Gemini/의장 발언을 **같은 Actions 봇의 역할별 댓글**로 기록합니다. 서로 다른 실제 계정으로 댓글을 다는 구조가 아닙니다. 사이클·턴 표시로 같은 실행을 재처리할 때 댓글이 중복되지 않습니다. NORMAL로 회복되면 봇이 소유한 장애 Issue만 복구 댓글 후 종료합니다. WARNING에서는 유지합니다. API가 실패해도 센서가 위험하면 Issue를 생성합니다. 토론은 이슈에서 별도로 재호출하지 않고 실제 3턴 응답을 재사용합니다.

## 대시보드

로컬 HTTP 서버나 Actions의 `factory-snapshot-*` 아티팩트를 사용할 수 있습니다. 외부 CDN 없이 지표·온도/생산 차트·모드별 회의록을 보여주고 60초마다 데이터를 확인합니다. API 호출 자체는 Actions 사이클 시점에만 발생합니다. AI 응답은 텍스트로 렌더링해 HTML로 실행되지 않습니다.

선택적으로 GitHub Pages를 사용하려면 저장소의 Pages 사용 가능 여부를 확인하고 **Settings → Pages → Source: GitHub Actions**로 설정합니다. 이후 `FACTORY_PAGES_ENABLED=true`와 **Factory dashboard → Run workflow**를 설정하면 화면 전용 두 파일만 배포합니다. 최초 공개는 사용자가 설정해야 합니다. 비공개 저장소의 Pages 지원은 플랜에 따라 다릅니다. Pages는 저장소 접근 범위와 별개로 공개될 수 있으므로 가상 센서·토론 데이터 공개 범위를 확인하세요.

## 파일별 실행

전체 실행에는 `run_cycle.py`를 사용하세요. 개별 디버깅에는 `python factory_sim.py --seed 42`, `python ai_council.py --mode mock`도 지원합니다. 개별 실행은 센서/설정만 갱신하며 대시보드와 전체 이력은 전체 실행에서 생성합니다. 로컬 동시 실행은 `.cycle.lock`으로 차단합니다. 프로세스가 강제 종료되어 잠금이 남으면 다른 실행이 없는지 확인하고 해당 잠금 파일을 제거하세요. 각 JSON 쓰기는 원자적으로 교체하지만, 로컬 파일 전체에 대한 트랜잭션은 제공하지 않습니다. GitHub에서는 성공한 커밋이 공개 상태의 단위입니다.

## 참고 문서

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Google GenAI Python SDK](https://googleapis.github.io/python-genai/)
- [GitHub 예약 이벤트](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

원래 설계서는 `docs/dual_ai_smart_factory.md`에 보존했습니다.
