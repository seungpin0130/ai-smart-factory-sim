import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main
from backend.models import AuditTrailLog, AiCouncilDecisionLog, Base, SimulationTickLog
from backend.simulator import M16SemiconFabSimulator


def actor(role, actor_id="TEST-01"):
    return {"X-Actor-Id": actor_id, "X-Actor-Role": role}


@pytest.fixture
def enterprise(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'fab.db'}")
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", sessions)
    monkeypatch.setattr(main, "init_db", lambda: Base.metadata.create_all(engine))
    monkeypatch.setattr(main, "sim_engine", M16SemiconFabSimulator(seed=2026))
    monkeypatch.setattr(main, "ai_control_mode", "AUTO_PILOT")
    monkeypatch.setattr(main, "live_streaming_enabled", False)
    monkeypatch.setattr(main, "pending_ai_decision", None)
    with TestClient(main.app) as client:
        yield client, sessions
    engine.dispose()


def test_rbac_and_batch_persistence(enterprise):
    client, sessions = enterprise
    assert client.get("/").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/api/fab/state").status_code == 200
    url = "/api/fab/control"
    request = {"tool_id": "ETCH_01", "action": "PM"}
    assert client.post(url, headers=actor("engineer"), json=request).status_code == 403
    assert client.post(url, json=request).status_code == 403
    assert client.post(url, headers=actor("maintenance"), json=request).status_code == 200
    assert client.post(url, headers=actor("maintenance"), json={"tool_id": "ETCH_01", "feed_rate": .5}).status_code == 403
    assert client.post(url, headers=actor("engineer"), json={"tool_id": "ETCH_01", "rul_pct": 40}).status_code == 403
    batch = client.post("/api/fab/simulate", json={"ticks": 6})
    assert batch.status_code == 200, batch.text
    assert len(batch.json()["snapshots"]) == 6
    with sessions() as db:
        assert db.query(SimulationTickLog).filter_by(run_mode="BATCH").count() == 6
        assert db.query(AuditTrailLog).filter_by(allowed=False).count() == 4
        assert db.query(AuditTrailLog).filter_by(allowed=True).count() == 1


def test_benchmark_is_repeatable_and_isolated(enterprise):
    client, sessions = enterprise
    state = client.get("/api/fab/state").json()["snapshot"]
    response = client.post("/api/fab/benchmark", json={"ticks": 12, "seed": 2026})
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["baseline"]["series"]) == len(result["ai_autopilot"]["series"]) == 12
    assert result["baseline"]["summary"]["proactive_pm_count"] == 0
    assert result == client.post("/api/fab/benchmark", json={"ticks": 12, "seed": 2026}).json()
    assert state == client.get("/api/fab/state").json()["snapshot"]
    with sessions() as db:
        assert db.query(SimulationTickLog).count() == 0


def test_websocket_and_hitl_approval_persist(enterprise):
    client, sessions = enterprise
    mode = {"ai_control_mode": "HITL"}
    assert client.post("/api/fab/mode", headers=actor("engineer"), json=mode).status_code == 403
    assert client.post("/api/fab/mode", headers=actor("manager"), json=mode).status_code == 200
    with client.websocket_connect("/ws/fab/live") as ws:
        assert ws.receive_json()["type"] == "INIT"
        ws.send_text("STEP_NOW")
        event = ws.receive_json()
        assert event["type"] == "TICK_UPDATE"
        assert event["pending_ai_decision"] is not None
        decision_id = event["pending_ai_decision"]["decision_id"]
        assert client.post("/api/fab/ai/approve", headers=actor("engineer")).status_code == 403
        approved = client.post("/api/fab/ai/approve", headers=actor("manager", "APPROVER"))
        assert approved.json()["status"] == "APPROVED"
        assert ws.receive_json()["pending_ai_decision"] is None
    with sessions() as db:
        row = db.get(AiCouncilDecisionLog, decision_id)
        assert row.applied and row.approved_by == "APPROVER"
        assert db.query(SimulationTickLog).filter_by(run_mode="LIVE").count() == 1


def test_automatic_live_tick_is_persisted(enterprise):
    client, sessions = enterprise
    assert client.post("/api/fab/mode", headers=actor("manager"), json={"live_streaming": True}).status_code == 200
    with client.websocket_connect("/ws/fab/live") as ws:
        initial = ws.receive_json()
        tick = ws.receive_json()
        assert tick["type"] == "TICK_UPDATE"
        assert tick["snapshot"]["tick"] == initial["snapshot"]["tick"] + 1
    with sessions() as db:
        assert db.query(SimulationTickLog).filter_by(run_mode="LIVE").count() >= 1
