# M16 Fab Enterprise Digital Twin v7.0

합성 반도체 공정 모델을 사용하는 로컬 디지털 트윈입니다. 대시보드에서
설비 상태와 FOUP 흐름을 보고, 규칙 기반 제어의 효과를 A/B 벤치마크로 비교합니다.

## 실행

Python 3.11 이상에서 실행합니다.

```bash
python -m pip install -r requirements.txt
uvicorn backend.main:app --reload
```

또는 Docker Compose로 실행합니다.

```bash
docker compose up --build -d
```

- 관리자 대시보드 http://localhost:8000/
- Swagger REST API http://localhost:8000/docs
- SQLite 기본 경로 `data/m16_fab_v7.db`. Compose는 `./data`를 마운트합니다.
- 외부 PostgreSQL을 쓰려면 `DATABASE_URL`을 `postgresql+psycopg://...`로 설정합니다.
- 개발 테스트는 프로젝트 루트에서 `python -m pytest -q`로 실행합니다.

사내 TLS 검사 환경에서는 신뢰할 수 있는 CA 번들을 Docker 빌드의
`pip_ca` BuildKit secret으로 전달할 수 있습니다.

## 기능과 운용 범위

- **실시간 텔레메트리** — `/ws/fab/live`에 구독자가 있고 Live 모드가 켜져 있으면
  2초마다 가상 팹 시간 15분을 진행합니다. 틱과 의사결정은 DB에 저장됩니다.
  서버 재시작 시 실행 중이던 시뮬레이터와 미승인 제안은 초기화됩니다.
- **폐루프 제어** — Auto-Pilot은 RUL·FDC·OHT·Q-Time 규칙으로 PM 또는 감속을
  적용합니다. HITL은 관리자가 승인할 때 적용하고 승인자를 DB에 기록합니다.
  두 공급자 키를 설정하면 의견 생성에만 OpenAI·Gemini를 사용합니다.
  실행 제어와 A/B 벤치마크는 결정론적 규칙을 사용합니다.
- **RBAC 시연** — 쓰기 제어 요청에 `X-Actor-Id`와 `X-Actor-Role`을 전달합니다.
  `engineer`는 Feed·Load·온도, `maintenance`는 PM·BM·RUL,
  `manager`는 전체 제어·모드 전환·AI 승인이 가능합니다.
  서버는 허용 및 거부 기록을 남깁니다. 역할 헤더는 클라이언트가 지정하는 시연값입니다.
  배포 시 인증된 사용자에게 서버가 역할을 부여하는 인증 계층이 필요합니다.
- **A/B 벤치마크** — `POST /api/fab/benchmark`에 `{"ticks":24,"seed":2026}`을
  보내면 동일 초기 시드에서 두 시나리오를 실행합니다. 현재 샘플에서 UDT는
  1→0회, Q-Time 위반은 25→11건, 평균 단위원가는 1,351.00→1,325.40 USD,
  완료 웨이퍼는 100→75매입니다. 결과는 합성 모델과 선택한 시드에 한정됩니다.


## 3D 설비 보기

8개 설비는 공정별 대표 구조를 바탕으로 모델링했습니다. 설비 이름과 상태등으로
구분할 수 있고, 개별 부품을 클릭하면 해당 설비의 계측과 제어 화면이 열립니다.
`선택 설비 확대`로 구조를 확인하고 `전체 보기`로 복귀할 수 있습니다.
모델은 실제 M16 CAD나 제조사 치수에 기반한 복제본이 아닙니다.


## Lot 투입과 공정 산출 이력

**3D 실시간 관제·제어** 화면 아래에서 웨이퍼 1~25매를 FOUP 한 개에 넣고
일반·긴급 우선순위로 투입할 수 있습니다. 자동 투입은 25매이며 이미 진행 중인
초기 재공과 별도로 집계합니다. 공정 중인 설비에는 Lot·FOUP ID, 공정명, 수량,
진행률이 표시됩니다. 3D 반송은 기록된 이송 이벤트를 짧게 재생합니다.

Lot 이력에는 다음 정보가 남습니다.

- 투입 수량, Lot·FOUP ID, 우선순위와 스토커
- 출발지·로드포트, OHT 소요 시간·혼잡도, 대기시간·Q-Time 초과
- 공정별 투입·산출 수량, 처리시간, 온도 전후, 압력·RF, 목표·예측 CD,
  FDC T²·APC 오프셋, 다음 공정. 중단 시 재대기 수량과 재시도 횟수
- EDS 완료 후 양품 스토커와 QA 보류로 분류된 수량, 모델 다이 수율,
  총 경과 시간, Q-Time 초과 건수

중간 공정은 동일한 웨이퍼 수를 다음 공정으로 넘깁니다. 최종 웨이퍼 분류는
모델 다이 수율을 Lot 수량에 적용해 반올림한 교육용 근사입니다. 실제 웨이퍼·다이
검사 등급이나 개별 검사 데이터를 재현하는 방식은 아닙니다.

서버는 이력을 `lot_trace_events` 테이블에 저장합니다. 현재 실행 이력은
`GET /api/fab/lot-events`에서 조회하고, `lot_id`로 검색할 수 있습니다.
서버 재시작 후 이전 실행은 `trace_id`를 함께 지정해 조회합니다.
`POST /api/fab/lots`에는 예를 들어 `{"wafers":17,"priority":"HOT_LOT"}`를 보내며,
`engineer` 또는 `manager` 역할이 필요합니다.
