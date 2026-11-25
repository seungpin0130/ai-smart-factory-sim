import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from db_manager import DB_PATH, REPLAY_PATH
from run_cycle import run
from storage import write_json
from test_factory import config, state


@pytest.fixture
def fab_api(tmp_path):
    write_json(tmp_path / "state.json", state(cycle=4).model_dump())
    write_json(tmp_path / "config.json", config().model_dump())
    run(tmp_path, event="none", seed=1, force=True)
    run(tmp_path, event="none", seed=1, force=True)
    with TestClient(create_app(tmp_path)) as client:
        yield client, tmp_path


def test_serves_dashboard_and_sqlite_replay(fab_api):
    client, root = fab_api
    assert "Enterprise MES" in client.get("/").text
    response = client.get("/api/fab/state")
    assert response.status_code == 200
    data = response.json()
    assert [s["tick"] for s in data["history"]] == [5, 6]
    assert data["state"] == data["history"][-1]
    replay = json.loads((root / REPLAY_PATH).read_text())[-1]
    assert data["state"]["oht_metrics"] == json.loads(replay["oht_metrics"])
    assert data["state"]["oht_metrics"]["routes"]["PHOTO_EUV_TO_ETCH"]["oht_power_kw"] >= 0
    assert not (root / "m16_fab.db").exists()


def test_controls_and_batch_are_isolated_from_replay(fab_api):
    client, root = fab_api
    replay = client.get("/api/fab/state").json()
    original_db = (root / DB_PATH).read_bytes()
    original_replay = (root / REPLAY_PATH).read_bytes()
    before = client.post("/api/fab/control", json={
        "base_tick": 5, "station_id": "PHOTO_EUV", "feed_rate": 0.5, "load_ratio": 0.4,
        "chamber_temp": 30, "rul": 50,
    })
    assert before.status_code == 200, before.text
    result = before.json()
    scenario_id = result["scenario_id"]
    equipment = result["state"]["equipments"]["PHOTO_EUV"]
    assert (equipment["feed_rate"], equipment["load_ratio"], equipment["chamber_temp"], equipment["rul"]) == (
        0.5, 0.4, 30, 50,
    )
    for station, action, duration in (("ETCH", "PM", 2), ("HBM_TSV", "BM", 6)):
        response = client.post("/api/fab/control", json={
            "scenario_id": scenario_id, "station_id": station, "action": action,
        })
        assert response.status_code == 200, response.text
        assert response.json()["state"]["equipments"][station]["maint_remain"] == duration
    response = client.post("/api/fab/simulate", json={"scenario_id": scenario_id, "ticks": 6})
    assert response.status_code == 200, response.text
    batch = response.json()
    snapshots = batch["snapshots"]
    assert [s["tick"] for s in snapshots] == list(range(6, 12))
    assert snapshots[0]["equipments"]["ETCH"]["status"] == "MAINTENANCE"
    assert snapshots[-1]["equipments"]["HBM_TSV"]["status"] == "RUNNING"
    assert snapshots[0]["equipments"]["PHOTO_EUV"]["load_ratio"] <= 0.4
    assert snapshots[0]["equipments"]["PHOTO_EUV"]["chamber_temp"] == 30
    assert snapshots[0]["equipments"]["PHOTO_EUV"]["rul"] < 50
    assert all("transfer_time_sec" in s["oht_metrics"]["routes"]["PHOTO_EUV_TO_ETCH"] for s in snapshots)
    assert client.get("/api/fab/state").json() == replay
    assert (root / DB_PATH).read_bytes() == original_db
    assert (root / REPLAY_PATH).read_bytes() == original_replay
    with closing(sqlite3.connect(root / "m16_fab.db")) as db:
        saved = db.execute(
            "SELECT snapshots, maintenance FROM fab_scenario_runs WHERE id = ?", (batch["run_id"],)
        ).fetchone()
        assert json.loads(saved[0]) == snapshots
        assert {row[2] for row in json.loads(saved[1])} >= {"PM", "BM"}


@pytest.mark.parametrize("ticks", [6, 12, 24, 48])
def test_all_batch_sizes_without_existing_journal(tmp_path: Path, ticks: int):
    write_json(tmp_path / "state.json", state(cycle=4).model_dump())
    write_json(tmp_path / "config.json", config().model_dump())
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/fab/state").json()["state"]["tick"] == 4
        response = client.post("/api/fab/simulate", json={"base_tick": 4, "ticks": ticks})
        assert response.status_code == 200, response.text
        assert len(response.json()["snapshots"]) == ticks
    assert not (tmp_path / DB_PATH).exists()


def test_invalid_inputs_and_unknown_sources(fab_api):
    client, _ = fab_api
    assert client.post("/api/fab/simulate", json={"ticks": 7}).status_code == 422
    assert client.post("/api/fab/simulate", json={"ticks": 6, "base_tick": 999}).status_code == 404
    assert client.post("/api/fab/control", json={"station_id": "NOPE", "rul": 50}).status_code == 422
    assert client.post("/api/fab/control", json={"station_id": "ETCH", "feed_rate": 10}).status_code == 422
    assert client.post("/api/fab/control", json={"station_id": "ETCH", "action": "BM", "rul": 50}).status_code == 422
    assert client.post("/api/fab/simulate", json={
        "ticks": 6, "scenario_id": "00000000-0000-0000-0000-000000000000"
    }).status_code == 404
