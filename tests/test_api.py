import time

import pytest
from fastapi.testclient import TestClient

from api import create_app


@pytest.fixture
def client(scheduler):
    app = create_app(scheduler, poll_interval=None)
    with TestClient(app) as client:
        yield client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_list_actions(client):
    assert client.get("/actions").json() == ["backup", "delete", "fail", "record", "sync"]


class TestUsers:
    def test_list(self, client):
        assert client.get("/users").json() == [
            {"name": "alice", "quota": 2, "executed": 0, "remaining": 2},
            {"name": "bob", "quota": 1, "executed": 0, "remaining": 1},
        ]

    def test_create(self, client):
        resp = client.post("/users", json={"name": "carol", "quota": 4})
        assert resp.status_code == 201
        assert resp.json() == {"name": "carol", "quota": 4, "executed": 0, "remaining": 4}
        assert client.get("/users/carol").status_code == 200

    def test_create_duplicate_conflicts(self, client):
        resp = client.post("/users", json={"name": "alice", "quota": 1})
        assert resp.status_code == 409
        assert resp.json() == {"detail": "User already exists: alice"}

    @pytest.mark.parametrize("body", [{"name": "x", "quota": -1}, {"name": "", "quota": 1}, {"name": "x"}])
    def test_create_invalid_body(self, client, body):
        assert client.post("/users", json=body).status_code == 422

    def test_get_unknown_is_404(self, client):
        resp = client.get("/users/ghost")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "User not found: ghost"}

    def test_delete(self, client):
        assert client.delete("/users/bob").status_code == 204
        assert client.get("/users/bob").status_code == 404

    def test_delete_user_with_tasks_conflicts(self, client):
        client.post("/tasks", json={"user": "bob", "at": "12:00", "action": "record", "target": "/a"})
        resp = client.delete("/users/bob")
        assert resp.status_code == 409
        assert "1 task(s)" in resp.json()["detail"]

    def test_reset_quotas(self, client, users):
        users.get_user("alice").consume()
        resp = client.post("/users/reset-quotas")
        assert resp.status_code == 200
        assert all(u["executed"] == 0 for u in resp.json())


class TestTasks:
    def create(self, client, **overrides):
        body = {"user": "alice", "at": "12:00", "action": "record", "target": "/a"} | overrides
        return client.post("/tasks", json=body)

    def test_create_and_get(self, client):
        resp = self.create(client)
        assert resp.status_code == 201
        task = resp.json()
        assert task == {"id": 1, "user": "alice", "at": "12:00", "action": "record", "target": "/a", "last_run": None}
        assert client.get("/tasks/1").json() == task

    def test_ids_increment(self, client):
        assert self.create(client).json()["id"] == 1
        assert self.create(client).json()["id"] == 2

    def test_list_and_filter_by_user(self, client):
        self.create(client, user="alice")
        self.create(client, user="bob")
        assert len(client.get("/tasks").json()) == 2
        assert [t["user"] for t in client.get("/tasks", params={"user": "bob"}).json()] == ["bob"]

    def test_unknown_user_is_404(self, client):
        assert self.create(client, user="ghost").status_code == 404

    def test_unknown_action_is_422(self, client):
        resp = self.create(client, action="format")
        assert resp.status_code == 422
        assert resp.json() == {"detail": "No executor registered for action: format"}

    @pytest.mark.parametrize("at", ["25:00", "12:60", "12-00", "noon"])
    def test_invalid_time_is_422(self, client, at):
        assert self.create(client, at=at).status_code == 422

    def test_delete(self, client):
        self.create(client)
        assert client.delete("/tasks/1").status_code == 204
        assert client.get("/tasks/1").status_code == 404

    def test_delete_unknown_is_404(self, client):
        resp = client.delete("/tasks/99")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Task not found: 99"}


class TestRun:
    def test_runs_due_tasks_at_given_time(self, client, recorder):
        client.post("/tasks", json={"user": "alice", "at": "12:00", "action": "record", "target": "/a"})
        client.post("/tasks", json={"user": "alice", "at": "13:00", "action": "record", "target": "/b"})

        resp = client.post("/run", json={"now": "2026-11-02T12:00:30"})
        assert resp.status_code == 200
        executed = resp.json()["executed"]
        assert [t["target"] for t in executed] == ["/a"]
        assert executed[0]["last_run"] == "2026-11-02"
        assert len(recorder.calls) == 1
        assert client.get("/users/alice").json()["executed"] == 1

    def test_second_run_same_minute_is_noop(self, client):
        client.post("/tasks", json={"user": "alice", "at": "12:00", "action": "record", "target": "/a"})
        client.post("/run", json={"now": "2026-11-02T12:00:00"})
        assert client.post("/run", json={"now": "2026-11-02T12:00:40"}).json() == {"executed": []}

    def test_without_body_uses_server_time(self, client):
        assert client.post("/run").json() == {"executed": []}

    def test_invalid_now_is_422(self, client):
        assert client.post("/run", json={"now": "yesterday"}).status_code == 422


def test_background_poller_runs_and_stops(scheduler, monkeypatch):
    app = create_app(scheduler, poll_interval=0.01)
    calls = []

    async def fake_run_pending(now=None):
        calls.append(now)
        return []

    monkeypatch.setattr(app.state.service, "run_pending", fake_run_pending)

    with TestClient(app):
        deadline = time.monotonic() + 2
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert len(calls) >= 2
    count = len(calls)
    time.sleep(0.05)
    assert len(calls) == count  # poller stopped with the app
