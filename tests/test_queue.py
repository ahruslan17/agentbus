"""Behaviour of the queue, against a real Postgres."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agentbus import store
from agentbus.api import create_app

from .conftest import TEST_URL, TOKEN


def send(client: TestClient, **overrides):
    payload = {
        "sender": "cage-claude",
        "recipient": "local-opencode",
        "kind": "task",
        "body": "do the work",
    }
    payload.update(overrides)
    response = client.post("/v1/messages", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_auth_is_required(clean: None, artifacts: str, settings) -> None:
    anonymous = TestClient(create_app(settings))
    assert anonymous.get("/v1/messages/x").status_code == 401
    assert anonymous.get("/v1/health").status_code == 200


def test_send_creates_thread_and_message(client: TestClient) -> None:
    message = send(client, repo="/tmp/example")
    assert message["state"] == "queued"
    assert message["thread_id"].startswith("t_")
    thread = client.get(f"/v1/threads/{message['thread_id']}").json()
    assert thread["thread"]["repo"] == "/tmp/example"
    assert [m["id"] for m in thread["messages"]] == [message["id"]]


def test_idempotency_key_does_not_duplicate(client: TestClient) -> None:
    first = send(client, idempotency_key="abc")
    second = send(client, idempotency_key="abc")
    assert first["id"] == second["id"]
    inbox = client.get("/v1/inbox/local-opencode").json()["messages"]
    assert len(inbox) == 1


def test_lease_is_exclusive(client: TestClient) -> None:
    a = send(client, body="first")
    b = send(client, body="second")

    first = client.post("/v1/inbox/local-opencode/lease").json()
    second = client.post("/v1/inbox/local-opencode/lease").json()
    third = client.post("/v1/inbox/local-opencode/lease")

    assert {first["id"], second["id"]} == {a["id"], b["id"]}
    assert third.status_code == 204


def test_lease_respects_kinds(client: TestClient) -> None:
    note = send(client, kind="note", body="just a note")
    task = send(client, kind="task", body="work")
    leased = client.post("/v1/inbox/local-opencode/lease?kinds=task").json()
    assert leased["id"] == task["id"]
    assert client.get(f"/v1/messages/{note['id']}").json()["state"] == "queued"


def test_reply_closes_parent_and_returns_to_sender(client: TestClient) -> None:
    parent = send(client)
    client.post("/v1/inbox/local-opencode/lease")
    reply = client.post(
        f"/v1/messages/{parent['id']}/reply",
        json={
            "sender": "local-opencode",
            "kind": "result",
            "body": "done",
            "meta": {"branch": "agentbus/x", "session_id": "ses_1"},
        },
    ).json()

    assert reply["recipient"] == "cage-claude"
    assert reply["thread_id"] == parent["thread_id"]
    assert reply["parent_id"] == parent["id"]
    assert client.get(f"/v1/messages/{parent['id']}").json()["state"] == "done"

    thread = client.get(f"/v1/threads/{parent['thread_id']}").json()
    assert thread["thread"]["session_id"] == "ses_1"

    leased = client.post("/v1/inbox/cage-claude/lease").json()
    assert leased["id"] == reply["id"]


def test_expired_lease_is_requeued(client: TestClient) -> None:
    message = send(client)
    first = client.post("/v1/inbox/local-opencode/lease").json()
    assert first["attempt"] == 1

    with store.connect(TEST_URL) as conn:
        conn.execute("update messages set lease_until = now() - interval '1 minute'")

    again = client.post("/v1/inbox/local-opencode/lease").json()
    assert again["id"] == message["id"]
    assert again["attempt"] == 2


def test_heartbeat_extends_lease(client: TestClient) -> None:
    message = send(client)
    client.post("/v1/inbox/local-opencode/lease")
    extended = client.post(f"/v1/messages/{message['id']}/heartbeat")
    assert extended.status_code == 200
    assert extended.json()["state"] == "leased"


def test_failed_state_is_terminal(client: TestClient) -> None:
    message = send(client)
    failed = client.post(
        f"/v1/messages/{message['id']}/state", json={"state": "failed", "note": "boom"}
    ).json()
    assert failed["state"] == "failed"
    assert failed["meta"]["state_note"] == "boom"
    assert client.post("/v1/inbox/local-opencode/lease").status_code == 204


def test_file_read_is_scoped_to_worktree(client: TestClient, artifacts: str) -> None:
    worktree = Path(artifacts) / "wt"
    worktree.mkdir()
    (worktree / "README.md").write_text("hello", encoding="utf-8")
    message = send(client, meta={"worktree": str(worktree)})

    ok = client.get(f"/v1/messages/{message['id']}/file", params={"path": "README.md"})
    assert ok.status_code == 200
    assert ok.json()["content"] == "hello"

    listing = client.get(f"/v1/messages/{message['id']}/file", params={"path": ""})
    assert listing.json()["kind"] == "dir"

    escape = client.get(f"/v1/messages/{message['id']}/file", params={"path": "../etc/passwd"})
    assert escape.status_code == 400


def test_artifact_read(client: TestClient, artifacts: str) -> None:
    message = send(client)
    target = Path(artifacts) / message["id"]
    target.mkdir(parents=True)
    (target / "patch.diff").write_text("diff --git a b\n", encoding="utf-8")

    ok = client.get(f"/v1/messages/{message['id']}/artifact", params={"name": "patch.diff"})
    assert ok.json()["content"].startswith("diff --git")

    bad = client.get(f"/v1/messages/{message['id']}/artifact", params={"name": "../x"})
    assert bad.status_code == 400


def test_unknown_thread_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/messages",
        json={
            "sender": "cage-claude",
            "recipient": "local-opencode",
            "kind": "task",
            "body": "x",
            "thread_id": "t_missing",
        },
    )
    assert response.status_code == 400


def test_auth_token_is_checked(settings) -> None:
    wrong = TestClient(create_app(settings), headers={"Authorization": "Bearer nope"})
    assert wrong.get("/v1/health").status_code == 200
    assert wrong.get("/v1/inbox/cage-claude").status_code == 401
    assert TOKEN != "nope"
