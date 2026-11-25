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
  1→0회, Q-Time 위반은 20→10건, 평균 단위원가는 1,350.10→1,331.50 USD,
  완료 웨이퍼는 125→100매입니다. 결과는 합성 모델과 선택한 시드에 한정됩니다.
