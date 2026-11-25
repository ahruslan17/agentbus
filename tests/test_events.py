"""Journal of events and executor slots, against a real Postgres."""

from __future__ import annotations

import json

import psycopg
from fastapi.testclient import TestClient

from agentbus import store

from .conftest import TEST_URL

LEASE_URL = "/v1/inbox/local-opencode/lease"


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


def lease(client: TestClient, **params):
    return client.post(LEASE_URL, params=params)


def reply(client: TestClient, message: dict) -> None:
    response = client.post(
        f"/v1/messages/{message['id']}/reply",
        json={"sender": "local-opencode", "body": "done"},
    )
    assert response.status_code == 201, response.text


def events_of(client: TestClient, thread_id: str) -> list[dict]:
    return client.get(f"/v1/threads/{thread_id}/events").json()["events"]


def worker_url(agent: str, slot: int) -> str:
    # '#' is a URL fragment delimiter, so the slot separator must be percent-encoded.
    return f"/v1/workers/{agent}%23{slot}"


def worker_payload(**overrides):
    payload = {
        "agent": "local-opencode",
        "host": "cage",
        "pid": 100,
        "model": "deepseek",
        "harness": "opencode",
        "state": "idle",
        "message_id": None,
    }
    payload.update(overrides)
    return payload


def test_thread_events_follow_the_lifecycle(client: TestClient) -> None:
    task = send(client)
    leased = lease(client).json()
    assert leased["id"] == task["id"]
    reply(client, task)

    events = events_of(client, task["thread_id"])

    assert [event["kind"] for event in events] == ["created", "leased", "created", "done"]
    assert [event["actor"] for event in events] == [
        "cage-claude",
        "local-opencode",
        "local-opencode",
        "system",
    ]
    assert events[0]["message_id"] == task["id"]
    assert events[0]["data"]["recipient"] == "local-opencode"
    assert events[1]["data"]["attempt"] == 1
    assert events[2]["data"]["parent_id"] == task["id"]
    assert events[3]["data"]["reason"] == "replied"


def test_idempotent_create_writes_one_event(client: TestClient) -> None:
    first = send(client, idempotency_key="same")
    second = send(client, idempotency_key="same")
    assert first["id"] == second["id"]

    created = [
        event for event in events_of(client, first["thread_id"]) if event["kind"] == "created"
    ]
    assert len(created) == 1


def test_expired_lease_is_requeued_with_previous_owner(client: TestClient) -> None:
    task = send(client)
    leased = lease(client, owner="local-opencode#2").json()
    assert leased["lease_owner"] == "local-opencode#2"

    with store.connect(TEST_URL) as conn:
        conn.execute("update messages set lease_until = now() - interval '1 minute'")
        assert store.reap_expired_leases(conn) == 1

    requeued = [
        event for event in events_of(client, task["thread_id"]) if event["kind"] == "requeued"
    ]
    assert len(requeued) == 1
    assert requeued[0]["actor"] == "system"
    assert requeued[0]["message_id"] == task["id"]
    assert requeued[0]["data"]["previous_owner"] == "local-opencode#2"


def test_failed_state_is_journalled_with_note(client: TestClient) -> None:
    task = send(client)
    response = client.post(
        f"/v1/messages/{task['id']}/state", json={"state": "failed", "note": "boom"}
    )
    assert response.status_code == 200

    failed = [event for event in events_of(client, task["thread_id"]) if event["kind"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["actor"] == "system"
    assert failed[0]["data"]["note"] == "boom"


def test_listen_gets_notified_after_commit(client: TestClient) -> None:
    with psycopg.connect(TEST_URL, autocommit=True) as listener:
        listener.execute("listen agentbus")
        task = send(client)
        note = next(listener.notifies(timeout=5, stop_after=1), None)

    assert note is not None
    assert note.channel == "agentbus"
    payload = json.loads(note.payload)
    assert payload["event_id"] > 0
    assert payload["kind"] == "created"
    assert payload["message_id"] == task["id"]
    assert payload["thread_id"] == task["thread_id"]


def test_worker_upsert_keeps_started_at_unless_pid_changes(client: TestClient) -> None:
    url = worker_url("local-opencode", 1)

    assert client.put(url, json=worker_payload()).status_code == 200
    with store.connect(TEST_URL) as conn:
        started_at = conn.execute("select started_at from workers").fetchone()["started_at"]
        backdated = conn.execute(
            """
            update workers set heartbeat_at = now() - interval '1 hour'
            returning heartbeat_at
            """
        ).fetchone()["heartbeat_at"]

    assert client.put(url, json=worker_payload(state="busy")).status_code == 200
    with store.connect(TEST_URL) as conn:
        rows = conn.execute("select * from workers").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "busy"
    assert rows[0]["started_at"] == started_at
    assert rows[0]["heartbeat_at"] > backdated

    assert client.put(url, json=worker_payload(pid=101)).status_code == 200
    with store.connect(TEST_URL) as conn:
        restarted = conn.execute("select * from workers").fetchone()
    assert restarted["pid"] == 101
    assert restarted["started_at"] > started_at

    assert client.put(url, json=worker_payload(state="sleeping")).status_code == 422


def test_workers_list_marks_stale_heartbeat(client: TestClient) -> None:
    fresh = worker_url("local-opencode", 1)
    stale = worker_url("local-opencode", 2)
    assert client.put(fresh, json=worker_payload()).status_code == 200
    assert client.put(stale, json=worker_payload()).status_code == 200

    with store.connect(TEST_URL) as conn:
        conn.execute(
            "update workers set heartbeat_at = now() - interval '1 hour' where id = %s",
            ("local-opencode#2",),
        )

    workers = {worker["id"]: worker for worker in client.get("/v1/workers").json()["workers"]}
    assert workers["local-opencode#1"]["stale"] is False
    assert workers["local-opencode#2"]["stale"] is True


def test_events_after_cursor_returns_only_new_ascending(client: TestClient) -> None:
    first = send(client, body="first")
    second = send(client, body="second")

    all_events = client.get("/v1/events").json()["events"]
    assert {event["message_id"] for event in all_events} == {first["id"], second["id"]}

    cursor = all_events[0]["id"]
    newer = client.get("/v1/events", params={"after": cursor}).json()["events"]
    assert newer
    ids = [event["id"] for event in newer]
    assert all(event_id > cursor for event_id in ids)
    assert ids == sorted(ids)

    tail = client.get("/v1/events", params={"after": all_events[-1]["id"]}).json()["events"]
    assert tail == []
