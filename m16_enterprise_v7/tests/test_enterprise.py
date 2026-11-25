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


def test_independent_reference_and_material_inspection():
    from backend.validation import validate_reference
    result = validate_reference()
    assert result["passed"], result["mismatches"]
    assert result["steps"] == 12
    assert result["accepted_wafers"] + result["qa_hold_wafers"] == 25


def test_logistics_reserves_stocker_slots_and_serializes_one_vehicle():
    sim = M16SemiconFabSimulator(automatic_releases=False, random_failures=False)
    sim.wip_lots.clear()
    sim.lot_events.clear()
    clean = sim.release_lot()
    euv = sim.release_lot()
    euv.route_idx = 1
    sim.stocker_slots["STOCKER_BAY_01"] = 0
    sim.oht_vehicles_total = 1
    sim.step()
    assert clean.location == "STOCKER_A"
    assert euv.location == "LOADPORT_EUV_01"
    assert sim.oht_dispatch_waiting > 0
    sim.stocker_slots["STOCKER_BAY_01"] = 1
    metrology = sim.release_lot()
    metrology.route_idx = 5
    sim.step()
    moves = [event for event in sim.lot_events if event["tick"] == sim.tick and event["event"] == "OHT_TRANSFER"]
    assert len(moves) == 2
    assert moves[1]["fab_minute"] >= moves[0]["fab_minute"] + moves[0]["transfer_sec"] / 60 - .002
    assert all(sim.stocker_occupancy()[place] <= capacity for place, capacity in sim.stocker_slots.items())


def test_checkpoint_restores_pending_approval_and_active_lots(enterprise):
    from backend import checkpoint
    from backend.models import FabCheckpoint

    client, sessions = enterprise
    assert client.post("/api/fab/mode", headers=actor("manager"), json={"ai_control_mode": "HITL"}).is_success
    assert client.post("/api/fab/simulate", json={"ticks": 2}).is_success
    saved = client.get("/api/fab/state").json()
    assert saved["pending_ai_decision"] is not None
    with sessions() as db:
        payload = db.get(FabCheckpoint, 1).payload
    expected = checkpoint.restore(payload["sim"])
    before = expected.step()
    with TestClient(main.app) as reloaded:
        recovered = reloaded.get("/api/fab/state").json()
        assert recovered["snapshot"] == saved["snapshot"]
        assert recovered["pending_ai_decision"] == saved["pending_ai_decision"]
        actual = reloaded.post("/api/fab/simulate", json={"ticks": 1}).json()["snapshots"][0]
        assert actual == before
        assert reloaded.post("/api/fab/ai/approve", headers=actor("manager", "SHIFT-B")).json()["status"] == "APPROVED"


def test_paired_benchmark_uses_same_horizons_and_preserves_live_state(enterprise):
    client, _ = enterprise
    request = {"ticks": 40, "seeds": [11, 22]}
    state = client.get("/api/fab/state").json()["snapshot"]
    response = client.post("/api/fab/benchmark-study", json=request)
    assert response.is_success, response.text
    data = response.json()
    assert [trial["seed"] for trial in data["trials"]] == [11, 22]
    for key, metric in data["paired"].items():
        counts = [trial["ai_autopilot"][key] - trial["baseline"][key] for trial in data["trials"]
                  if trial["ai_autopilot"][key] is not None and trial["baseline"][key] is not None]
        assert metric["compared_seeds"] == len(counts)
        assert metric["mean_delta"] == (round(sum(counts) / len(counts), 2) if counts else None)
    assert data == client.post("/api/fab/benchmark-study", json=request).json()
    assert client.get("/api/fab/state").json()["snapshot"] == state


def test_authenticated_operator_cannot_forge_role(enterprise, tmp_path, monkeypatch):
    import json
    from starlette.websockets import WebSocketDisconnect
    from backend.auth import password_hash

    _, sessions = enterprise
    config = tmp_path / "operators.json"
    password = "simulation-test-password"
    config.write_text(json.dumps({"ENG": {"role": "engineer", "password_hash": password_hash(password)}}))
    monkeypatch.setenv("M16_OPERATOR_FILE", str(config))
    header = actor("manager", "FORGED")
    with TestClient(main.app) as client:
        assert client.get("/api/fab/state", headers=header).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/fab/live"):
                pass
        assert client.post("/api/auth/login", json={"actor_id":"ENG", "password":"wrong"}).status_code == 401
        login = client.post("/api/auth/login", json={"actor_id":"ENG", "password":password})
        assert login.is_success
        assert "HttpOnly" in login.headers["set-cookie"] and "SameSite=strict" in login.headers["set-cookie"]
        assert client.post("/api/fab/mode", headers=header, json={"live_streaming":True}).status_code == 403
        assert client.post("/api/fab/lots", headers={"Origin":"https://elsewhere.test"}, json={"wafers":5}).status_code == 403
        assert client.post("/api/fab/lots", headers=header, json={"wafers":5}).status_code == 201
        with client.websocket_connect("/ws/fab/live") as ws:
            assert ws.receive_json()["type"] == "INIT"
        assert client.post("/api/auth/logout").is_success
        assert client.get("/api/fab/state").status_code == 401
    with sessions() as db:
        release = db.query(AuditTrailLog).filter_by(action_type="LOT_RELEASE").one()
        assert release.actor_id == "ENG"
