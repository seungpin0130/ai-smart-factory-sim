# M16 가상 반도체 팹 v2

첨부 v2의 3D 장면과 WIP·PM·램프업·전력/원가 모델을 반영한 교육용 시뮬레이션입니다. 실제 SK하이닉스 공정 데이터/공식 설비 모델이 아니며, 200매는 가상 입고 배치입니다.

## 실행

```bash
python -m pip install -r requirements.txt
python run_cycle.py --mode mock --force-ai-debate
python -m http.server 8000
```

http://localhost:8000 에서 3D 화면을 엽니다. 3D/차트/CSS 라이브러리는 첨부본의 외부 CDN을 사용합니다. 화면의 로컬 버튼은 미리보기이고 GitHub 데이터에는 반영되지 않습니다. 별도 Python 실행은 `python factory_sim.py` 다음 `python ai_council.py --mode mock` 순서로도 가능합니다.

## v2 반영

- 제어: euv_throughput, chiller_power, auto_foup_order, schedule_pm.
- 상태: 기존 팹 필드에 hbm_wip_buffer, power_kwh, unit_cost_index, ramp_up_remaining 추가.
- 전공정 투입 `floor(throughput*0.6)`, 후공정 최대 48매/사이클. 잔여 물량은 WIP에 보존합니다.
- wafer_output/total_wafer_output은 v2부터 추정 양품 완료량입니다. v1의 총처리량과 의미가 다릅니다. 이번 초기 상태 교체는 사용자 요청대로 cycle=0에서 시작합니다.
- 수명 <20% 또는 PM 예약이면 한 사이클 생산을 쉬고 수명 100%, 이후 두 사이클 60%/80% 램프업을 적용합니다. PM 중 WIP를 임의로 폐기하지 않습니다.
- 온도/EDS/청정도/펠리클/WIP 위험과 3사이클 주기, 최초 가동, force_ai_debate에 따라 3턴을 소집합니다. 정상 대기 사이클은 hold로 기록하고 공급자 API를 호출하지 않습니다. 모든 사이클에서 SOP는 실행됩니다.
- 1턴 OpenAI 생산안, 2턴 Gemini 안전안, 3턴 짝수 OpenAI/홀수 Gemini 의장입니다. 최근 5사이클 텔레메트리를 제공합니다.
- telemetry_history.json은 최근 100개의 센서 스냅샷, logs/cycles는 전체 의사결정 원장입니다. 로그의 mock/hold/live/fallback을 구분합니다.
- API 실패는 fallback으로 기록합니다. 장애 Issue는 기존 봇 소유 Issue를 재사용하고 실제 회의 응답을 댓글에 사용하므로 복구 보고를 위해 API를 추가 호출하지 않습니다.

## 수정한 경계 조건

첨부 코드의 위험 상태 기준(온도 >=68, 수율 <88, 재고 <50, 청정도 >=45)을 공통 함수로 통일했습니다. 68°C부터 전공정을 정지하고 냉각을 유지합니다. 후공정은 쌓인 WIP를 처리할 수 있습니다. PM 후에도 센서가 위험하면 CRITICAL을 유지합니다. 재고가 없어 생산하지 않으면 생산 발열과 생산 마모도 발생하지 않습니다. 청정도 >30, 펠리클 <35, WIP >85는 WARNING입니다. 펠리클 <22이면 PM 예약을 강제합니다.

전력과 원가는 첨부 계수를 사용한 합성 지표입니다. EDS는 정지 중에도 계산하는 품질 추정치로, 실제 표본 수율이나 HBM 패키지 단위 모사는 아닙니다. WIP 손실은 후공정 처리량 중 추정 양품이 아닌 부분으로 해석합니다.

## 동시성

웹 화면은 config.json을 직접 수정하지 않고 `workflow_dispatch`의 config_json과 expected_config_sha로 변경을 요청합니다. 같은 concurrency 그룹에서 설정 적용과 시뮬레이션을 처리하며, 최신 config blob SHA가 다르면 이전 설정 요청을 거부합니다. 새 화면/Custom GPT 스펙도 이 방식을 사용합니다.

workflow는 계산 **전에** git pull --rebase를 실행합니다. 계산 후 push가 외부 편집과 충돌하면 실패로 남기며 `|| true`나 강제 push로 덮어쓰지 않습니다. concurrency는 같은 그룹의 워크플로우만 직렬화합니다. GitHub의 웹 편집이나 별도 API 직접 쓰기까지 충돌이 절대 없다고 보장하지 않습니다.

## Actions

`Dual-AI SK Hynix Fab Simulation`은 30분 cron과 수동 실행을 지원합니다. schedule에서 두 API 키가 모두 등록되어 있으면 live, 아니면 mock으로 실행합니다. GitHub 스케줄은 지연될 수 있습니다. 수동 실행 기본값은 mock이고, force_ai_debate/alerts를 선택할 수 있습니다. live는 공급자 사용료가 발생할 수 있습니다.

Secrets: OPENAI_API_KEY, GEMINI_API_KEY. 모델 Variables: OPENAI_MODEL, GEMINI_MODEL. GitHub 토큰은 워크플로우가 자동 제공합니다. 실제 API 연결은 키 등록 후 실행 결과로 확인해야 합니다.

## 3D 웹 연동

브라우저의 GitHub API 설정에 저장소 경로와 제한된 토큰을 입력합니다. 이 화면에는 Contents 읽기와 Actions 쓰기 권한이면 충분합니다. 토큰은 메모리에서만 보유하고 페이지를 닫으면 사라집니다. 센서·설정·이력·로그는 하나의 commit SHA로 읽어 서로 다른 사이클이 섞이지 않게 합니다. 로그는 HTML로 실행하지 않고 텍스트로 표시합니다. 설정 요청의 실패 HTTP 상태를 확인하며 dispatch 접수를 완료로 표시하지 않습니다.

GitHub Pages 배포는 기존 설정을 유지합니다. 비공개 저장소가 자동으로 웹에 공개되는 것은 아닙니다. Actions 아티팩트에 화면과 데이터도 저장합니다.

## Custom GPT

openapi_gpt_actions.json과 CUSTOM_GPT_INSTRUCTIONS.md를 사용합니다. v2는 직접 PUT 대신 dispatch의 config_json(전체 네 필드 JSON 문자열) + expected_config_sha(최신 config blob SHA)로 설정을 적용합니다. 인증정보는 GPT Actions 인증 설정에 넣고 프롬프트/파일에 쓰지 않습니다. prepare_config_update.py는 이전 직접 Contents API 방식의 유틸리티로 남겨 두었으며 v2 웹/Custom GPT는 사용하지 않습니다.

## 검증

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

물량/WIP, PM·램프업, 임계값, 에너지, 이벤트 소집과 의장 교대, 공급자 오류, GitHub Issue 중복 방지, 스키마·SHA 충돌을 검증합니다. 공급자 SDK 검사는 모의 HTTP 응답을 사용합니다.
