from fastapi.testclient import TestClient

from ravens_nest.app import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_stub():
    response = client.get("/")
    assert response.status_code == 200
    assert "The Raven's Nest" in response.text


def test_sync_endpoints(tmp_path, monkeypatch, run_git):
    repo = tmp_path / "solo"
    run_git(tmp_path, "init", "--initial-branch=main", str(repo))
    (repo / "data" / "events").mkdir(parents=True)
    monkeypatch.setenv("RAVENS_NEST_DATA", str(repo / "data"))
    app.state.sync_manager = None
    try:
        with TestClient(app) as c:  # runs lifespan: startup pull + apply
            status = c.get("/sync/status")
            assert status.status_code == 200
            body = status.json()
            assert body["has_remote"] is False
            assert set(body) >= {
                "has_remote",
                "remote_reachable",
                "last_pull",
                "last_push",
                "unpushed_events",
            }

            response = c.post("/sync")
            assert response.status_code == 200
            assert response.json()["last_pull"]["detail"] == "no remote configured"
    finally:
        app.state.sync_manager = None
