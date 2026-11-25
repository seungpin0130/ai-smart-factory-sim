# [프로젝트 인수인계서] SK hynix M16 Fab - Enterprise MES & Digital Twin Admin Portal (v6.0 → 상용 아키텍처 전환)

아래 내용을 ChatGPT에 그대로 붙여넣어 연동 작업을 이어가세요.

## 현재 작업 상태

- `backend/main.py`와 `backend/models.py`에 첨부의 로컬 FastAPI 및 SQLAlchemy 코드를 배치했습니다. `uvicorn backend.main:app --reload`로 실행하며 기본 DB는 프로젝트 루트의 `m16_fab.db`입니다.
- 로그인과 AI 위원회는 모의 구현입니다. 알람 목록은 대시보드 요약에 포함되며 별도 목록 API, 실인증, 실제 LLM 호출과 PostgreSQL 실행 검증은 후속 범위입니다.
- 아래 v6.0 UI 설명은 전달받은 명세입니다. 원본 `M16_Fab_Admin_Dashboard_v6.html`은 이번 첨부에 없으므로 연동 전에 원본 파일과 실제 DOM·이벤트를 확인하세요. 기존 저장소의 `web/index.html`은 v3 리플레이 화면입니다.

---

## 1. 프로젝트 개요 및 목표
- **프로젝트명**: SK hynix M16 Fab Digital Twin & Enterprise MES Admin Portal
- **전달받은 프론트엔드 명세**: 단일 파일 `M16_Fab_Admin_Dashboard_v6.html`
- **다음 목표**: 브라우저 내 임시 데이터(`MOCK_HISTORY`)와 JS 물리 연산을 **Python FastAPI 백엔드 + RDBMS(SQLite/PostgreSQL) + WebSocket + 실제 LLM API(OpenAI & Gemini)** 구조로 전환하여 상용 수준의 서버·DB·API 연동을 검증하는 것.

---

## 2. 현재 구현된 프론트엔드(`v6.0`) 아키텍처 요약
- **UI 프레임워크**: Bootstrap 5.3 + Bootstrap Icons (AdminLTE/CoreUI 스타일, 다크/라이트 테마 토글, 접이식 좌측 사이드바, 상단 통합 검색·알림·계정 드롭다운)
- **화면 구성 (4개 뷰)**:
  1. **로그인 페이지 (`#view-login`)**: Split-Screen 엔터프라이즈 SSO 로그인 (사번, 비밀번호, 직무 권한: `공정기술 엔지니어`, `설비보전 파트장`, `Fab 생산관리 총괄`)
  2. **통합 대시보드 (`#page-dashboard`)**: 4종 핵심 KPI 카드(EDS 수율, OEE, 전력 부하, 단위 제조원가), 수율·전력 실시간 추이 차트, 핵심 설비 RUL Progress Bar, 최근 공정 이벤트 로그 테이블
  3. **3D 실시간 관제·제어 (`#page-3d-control`)**:
     - `Three.js` 기반 3D Fab 뷰포트 (`PHOTO_EUV`, `ETCH`, `HBM_TSV` 설비 3종 + 상단 OHT 반송 트랙/캐리어 6대 + 하단 선택 링)
     - 좌측 설비 개별 인스펙터: 투입 속도 배율(`feed_rate`: 0.2~1.5x), 목표 부하율(`load_ratio`: 30~100%), 챔버 온도(`chamber_temp`: 20~90°C), RUL 설정(5~100%), 예방정비(`PM`), 돌발고장(`BM`), 배치 시뮬레이션 실행(6/12/24/48틱)
     - 우측 듀얼 AI 의사결정 위원회 패널: 임계치 초과 시 ChatGPT(생산기술)와 Gemini(설비·원가)의 토론 로그 및 최종 합의 JSON 표시
  4. **시뮬레이션 리포트 (`#page-reports`) & 알람 이력 (`#page-alarms`)**:
     - `Chart.js` 4종 시계열/누적 막대 차트, Plant Simulation 표준 통계표(Working/Blocked/PM/BM 점유율), CSV 및 HTML 다운로드, 알람 등급별 필터링 및 확인(Acknowledge) 처리

---

## 3. 프론트엔드 ↔ 백엔드 연동 데이터 스키마 (JSON 규격)

### ① 틱 텔레메트리 스냅샷 (`TickSnapshot`)
```json
{
  "tick": 102,
  "timestamp": "2026-11-25 14:00",
  "kpi": {
    "eds_yield": 97.90,
    "power_kw": 1580,
    "unit_cost": 1245
  },
  "wip_buffers": {
    "PHOTO_EUV": 62,
    "ETCH": 86,
    "HBM_TSV": 78
  },
  "equipments": {
    "PHOTO_EUV": { "status": "RUNNING", "rul": 84.2, "chamber_temp": 22.4, "load_ratio": 0.82, "feed_rate": 0.4 },
    "ETCH":      { "status": "RUNNING", "rul": 18.4, "chamber_temp": 74.8, "load_ratio": 0.96, "feed_rate": 1.0 },
    "HBM_TSV":   { "status": "RUNNING", "rul": 87.0, "chamber_temp": 44.5, "load_ratio": 0.88, "feed_rate": 1.0 }
  },
  "trigger_reason": "RUL_CRITICAL:ETCH (18.4%)",
  "debate": [
    { "speaker": "ChatGPT (생산기술)", "turn": 1, "text": "..." },
    { "speaker": "Gemini (설비·원가)", "turn": 2, "text": "..." }
  ],
  "final_action": { "PHOTO_EUV_feed_rate": 0.4, "ETCH_action": "SCHEDULE_PM_NOW" }
}
```

### ② 설비별 기준 상수
- `PHOTO_EUV` (EUV 노광기 #01): 최적 온도 `22.0°C`
- `ETCH` (식각 챔버 #02): 최적 온도 `64.0°C`
- `HBM_TSV` (HBM TSV 적층기): 최적 온도 `42.0°C`
- `PM` 실행 시: `status="MAINTENANCE"`, `maint_type="PM"`, `maint_remain=2`, `rul=100.0`, `unit_cost += 95`
- `BM` 발생 시 (`rul == 0` 또는 수동 주입): `status="MAINTENANCE"`, `maint_type="BM"`, `maint_remain=6`, `eds_yield -= 1.4`, `unit_cost += 380`

---

## 4. ChatGPT에게 요청할 다음 작업 지시사항
> "위 명세를 바탕으로, 기존 `M16_Fab_Admin_Dashboard_v6.html` 프론트엔드와 연동되는 **Python FastAPI 백엔드(`backend/main.py`), SQLAlchemy DB 모델(`backend/models.py`), 그리고 `fetch()` 및 `WebSocket` 연동이 적용된 프론트엔드 수정 코드**를 완성해 줘.
> 1. 로컬에서 즉시 실행 가능하도록 기본 DB는 SQLite(`m16_fab.db`)를 사용하되 `.env`의 `DATABASE_URL` 변경 시 PostgreSQL로 바로 전환 가능하게 설계할 것.
> 2. `POST /api/auth/login`, `GET /api/dashboard/summary`, `PATCH /api/stations/{station_id}/control`, `POST /api/simulations/batch`, `GET /api/alarms`, `PATCH /api/alarms/{alarm_id}/ack`, `GET /api/reports/{run_id}/export.csv`, 그리고 실시간 동기화용 `WS /ws/fab` 엔드포인트를 구현할 것.
> 3. 임계치 초과 시 OpenAI API와 Gemini API를 비동기 병렬 호출하되, API 키가 없을 경우 자동으로 규칙 기반 Fallback 응답을 반환하도록 작성할 것."