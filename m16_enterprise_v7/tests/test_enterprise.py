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


def test_lot_release_and_persisted_process_events(enterprise):
    client, _ = enterprise
    path = "/api/fab/lots"
    body = {"wafers": 17, "priority": "HOT_LOT"}
    assert client.post(path, headers=actor("maintenance"), json=body).status_code == 403
    assert client.post(path, headers=actor("engineer"), json={"wafers": 26}).status_code == 422
    release = client.post(path, headers=actor("engineer"), json=body)
    assert release.status_code == 201, release.text
    lot = release.json()
    assert lot["snapshot"]["kpi"]["released_wafers"] == 17
    result = client.post("/api/fab/simulate", json={"ticks": 12})
    assert result.status_code == 200
    response = client.get("/api/fab/lot-events", params={"trace_id": lot["trace_id"], "lot_id": lot["lot_id"]})
    events = response.json()["events"]
    assert events[0]["event"] == "LOT_RELEASED" and events[0]["wafers_in"] == 17
    assert all(e["lot_id"] == lot["lot_id"] and e["foup_id"] == lot["foup_id"] for e in events)
    assert {e["event"] for e in events} >= {"OHT_TRANSFER", "PROCESS_STARTED", "PROCESS_COMPLETED"}
    assert [e["fab_minute"] for e in events] == sorted(e["fab_minute"] for e in events)


def test_full_route_records_material_balance_and_inspection_disposition():
    from backend.simulator import M16_HBM3E_ROUTE

    sim = M16SemiconFabSimulator()
    sim.wip_lots.clear()
    sim.lot_events.clear()
    sim.initial_wip_wafers = 0
    sim.rng.random = lambda: .99  # Isolate release and run without random equipment failures.
    lot = sim.release_lot(25, source="MANUAL")
    for _ in range(70):
        snapshot = sim.step()
        if sim.completed_lots:
            break
    completions = [event for event in sim.lot_events if event["event"] == "PROCESS_COMPLETED"]
    assert [e["step_name"] for e in completions] == [step.step_name for step in M16_HBM3E_ROUTE]
    assert all(e["wafers_out"] == 25 and e["process_min"] > 0 for e in completions)
    assert all("predicted_cd_nm" in e and "temperature_in_c" in e for e in completions)
    disposition = next(e for e in sim.lot_events if e["event"] == "LOT_DISPOSITION")
    assert disposition["lot_id"] == lot.lot_id
    assert disposition["accepted_wafers"] + disposition["rejected_wafers"] == 25
    assert disposition["accepted_destination"] == "FG_STOCKER"
    assert disposition["rejected_destination"] == "QA_HOLD"
    kpi = snapshot["kpi"]
    assert kpi["released_wafers"] == kpi["wip_wafer_count"] + kpi["accepted_wafers"] + kpi["rejected_wafers"]


def test_interrupted_lot_is_held_and_retried_without_extra_release():
    sim = M16SemiconFabSimulator()
    sim.wip_lots.clear()
    sim.lot_events.clear()
    sim.rng.random = lambda: .99
    lot = sim.release_lot(11)
    # EUV's first process duration exceeds one tick.
    lot.route_idx = 1
    sim.step()
    sim.apply_tool_control("EUV_01", action="PM")
    hold = sim.lot_events[-1]
    assert hold["event"] == "PROCESS_INTERRUPTED" and hold["wafers_held"] == 11
    for _ in range(4):
        sim.step()
    starts = [e for e in sim.lot_events if e["event"] == "PROCESS_STARTED" and e["tool_id"] == "EUV_01"]
    assert [e["attempt"] for e in starts] == [1, 2]
    assert sim.released_wafers == 11
