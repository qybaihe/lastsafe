from pathlib import Path

from fastapi.testclient import TestClient

from lastsafe.api import create_app
from lastsafe.config import Settings


def test_replay_api_end_to_end(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=tmp_path / "api.db"))

    with TestClient(app) as client:
        health = client.get("/health")
        bootstrap = client.get("/api/bootstrap")
        run = client.post(
            "/api/runs",
            json={
                "scenario": {
                    "spot_shift_pct": 0,
                    "buying_power_pct": 100,
                    "minutes_to_close": 95,
                    "as_of_date": "2026-09-04",
                },
                "execute": True,
            },
        )
        history = client.get("/api/runs")
        page = client.get("/")
        script = client.get("/app.js")
        styles = client.get("/styles.css")
        fixture = client.get("/replay.json")

    assert health.json() == {"status": "ok", "mode": "replay", "paper": "locked"}
    assert bootstrap.status_code == 200
    assert bootstrap.json()["evaluation"]["policy_action"] == "ROLL"
    assert run.status_code == 200
    assert run.json()["decision"]["action"] == "ROLL"
    assert run.json()["execution"]["status"] == "simulated"
    assert len(history.json()) == 1
    assert "The agent that starts" in page.text
    assert "rel=\"icon\"" in page.text
    assert script.status_code == 200
    assert styles.status_code == 200
    assert fixture.json()["source"] == "replay"


def test_execution_token_protects_runs_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "protected.db",
        execution_enabled=True,
        execution_token="secret-token",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        denied = client.post("/api/runs", json={"execute": False})
        allowed = client.post(
            "/api/runs",
            json={"execute": False},
            headers={"X-LastSafe-Execution-Token": "secret-token"},
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200
