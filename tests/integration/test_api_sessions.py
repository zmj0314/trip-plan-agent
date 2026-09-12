"""The HTTP surface, against the real store and channel layers."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.channels.local import LocalAdapter
from app.config.settings import Settings
from app.store import Database
from tests.conftest import FULL_REQUEST


def build_client(tmp_path) -> TestClient:
    settings = Settings(llm_offline=True, data_dir=tmp_path)
    settings.ensure_dirs()
    Database(settings.db_path).migrate()
    app = create_app(settings)

    # Keep the API test hermetic: the local adapter is real, everything else is
    # simply absent, which is exactly the "no credentials" deployment.
    app.state.ctx.resolver = None
    app.state.ctx.runner.deps.resolver = None
    return TestClient(app)


def test_health_reports_what_is_actually_available(tmp_path):
    with build_client(tmp_path) as client:
        body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["capabilities"] > 0
    assert body["persistence"] is True


def test_full_conversation_over_http(tmp_path):
    with build_client(tmp_path) as client:
        session_id = client.post("/sessions", json={"user_id": "local"}).json()["session_id"]

        first = client.post(f"/sessions/{session_id}/messages", json={"text": "我想去长城"}).json()
        kinds = [event["type"] for event in first["events"]]
        assert "interrupt" in kinds

        second = client.post(f"/sessions/{session_id}/messages", json={"text": FULL_REQUEST}).json()
        interrupt = next(event for event in second["events"] if event["type"] == "interrupt")
        assert interrupt["data"]["kind"] == "gate2_full"
        plan_hash = interrupt["data"]["plan_hash"]

        snapshot = client.get(f"/sessions/{session_id}/snapshot").json()
        assert snapshot["phase"] == "AWAIT_CONSENT"

        approved = client.post(
            f"/sessions/{session_id}/resume",
            json={"decision": "approve", "plan_hash": plan_hash},
        ).json()
        assert any(event["type"] == "done" for event in approved["events"])

        assert client.get(f"/sessions/{session_id}/snapshot").json()["phase"] == "DONE"


def test_a_stale_plan_hash_is_a_409(tmp_path):
    with build_client(tmp_path) as client:
        session_id = client.post("/sessions", json={}).json()["session_id"]
        client.post(f"/sessions/{session_id}/messages", json={"text": FULL_REQUEST})

        response = client.post(
            f"/sessions/{session_id}/resume",
            json={"decision": "approve", "plan_hash": "deadbeef"},
        )

    assert response.status_code == 409


def test_replayed_events_are_readable_after_a_disconnect(tmp_path):
    with build_client(tmp_path) as client:
        session_id = client.post("/sessions", json={}).json()["session_id"]
        client.post(f"/sessions/{session_id}/messages", json={"text": FULL_REQUEST})

        rows = client.get(f"/sessions/{session_id}/events", params={"since": 0}).json()["events"]
        assert rows
        assert all(row["replay"] is True for row in rows)

        tail = client.get(
            f"/sessions/{session_id}/events", params={"since": rows[-1]["seq"]}
        ).json()["events"]
        assert tail == []


def test_a_duplicate_approve_is_not_resumed_twice(tmp_path):
    """DJ-6: double-clicking the button must not book twice."""

    with build_client(tmp_path) as client:
        session_id = client.post("/sessions", json={}).json()["session_id"]
        events = client.post(f"/sessions/{session_id}/messages", json={"text": FULL_REQUEST}).json()["events"]
        plan_hash = next(e for e in events if e["type"] == "interrupt")["data"]["plan_hash"]

        body = {"decision": "approve", "plan_hash": plan_hash, "client_request_id": "req-1"}
        first = client.post(f"/sessions/{session_id}/resume", json=body).json()
        second = client.post(f"/sessions/{session_id}/resume", json=body).json()

        assert first == second
