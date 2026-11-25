"""`GET /v1/mailboxes/{name}/status`: what one mailbox may know about the queue."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi.testclient import TestClient

LEASE_URL = "/v1/inbox/local-opencode/lease"


def send(client: TestClient, **overrides) -> dict:
    payload = {
        "sender": "cage-claude",
        "recipient": "local-opencode",
        "kind": "task",
        "body": "сделай дело",
    }
    payload.update(overrides)
    response = client.post("/v1/messages", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def lease(client: TestClient, **params) -> dict:
    response = client.post(LEASE_URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def report_slot(
    client: TestClient,
    slot_id: str = "local-opencode#0",
    *,
    message_id: str | None = None,
    state: str = "busy",
) -> dict:
    # `#` starts the URL fragment, so the slot id must be percent-encoded.
    path = quote(slot_id, safe="")
    response = client.put(
        f"/v1/workers/{path}",
        json={
            "agent": "local-opencode",
            "state": state,
            "message_id": message_id,
            "model": "deepseek",
            "harness": "opencode",
            "pid": 1,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def status_of(client: TestClient, mailbox: str = "cage-claude") -> dict:
    response = client.get(f"/v1/mailboxes/{mailbox}/status")
    assert response.status_code == 200, response.text
    return response.json()


def test_pending_shows_positions_the_lease_and_the_result(client: TestClient) -> None:
    first = send(client, meta={"ticket": "T11"})
    second = send(client, body="вторая задача")

    pending = {item["id"]: item for item in status_of(client)["pending"]}
    assert pending[first["id"]]["state"] == "queued"
    assert pending[first["id"]]["queue_position"] == 1
    assert pending[first["id"]]["ticket"] == "T11"
    assert pending[first["id"]]["result_id"] is None
    assert pending[second["id"]]["queue_position"] == 2

    leased = lease(client)
    assert leased["id"] == first["id"]

    pending = {item["id"]: item for item in status_of(client)["pending"]}
    assert pending[first["id"]]["state"] == "leased"
    assert pending[first["id"]]["lease_owner"] == "local-opencode"
    assert pending[first["id"]]["attempt"] == 1
    assert pending[first["id"]]["queue_position"] is None
    assert pending[second["id"]]["queue_position"] == 1

    result = client.post(
        f"/v1/messages/{first['id']}/reply",
        json={"sender": "local-opencode", "kind": "result", "body": "готово"},
    ).json()

    pending = {item["id"]: item for item in status_of(client)["pending"]}
    assert pending[first["id"]]["state"] == "done"
    assert pending[first["id"]]["result_id"] == result["id"]
    assert result["id"] not in pending, "answers of the executor are not this mailbox work"


def test_foreign_slot_exposes_only_the_sender(client: TestClient) -> None:
    secret = "секретный текст чужой задачи"
    task = send(client, sender="bob", body=secret, meta={"ticket": "SECRET-1"})
    lease(client, owner="local-opencode#0")
    report_slot(client, message_id=task["id"])

    status = status_of(client, mailbox="alice")
    assert status["pending"] == []
    slot = next(item for item in status["workers"] if item["id"] == "local-opencode#0")
    assert slot["message_id"] == task["id"]
    assert slot["sender"] == "bob"
    assert "ticket" not in slot
    assert "thread_id" not in slot
    dumped = json.dumps(status, ensure_ascii=False)
    assert secret not in dumped
    assert "SECRET-1" not in dumped


def test_own_slot_shows_ticket_and_thread(client: TestClient) -> None:
    task = send(client, meta={"ticket": "T11"})
    lease(client)
    report_slot(client, message_id=task["id"])

    status = status_of(client)
    slot = next(item for item in status["workers"] if item["id"] == "local-opencode#0")
    assert slot["state"] == "busy"
    assert slot["stale"] is False
    assert slot["ticket"] == "T11"
    assert slot["thread_id"] == task["thread_id"]
    assert "sender" not in slot


def test_status_never_contains_message_bodies(client: TestClient) -> None:
    task = send(client, body="конфиденциальный текст задачи")
    lease(client)
    report_slot(client, message_id=task["id"])

    dumped = json.dumps(status_of(client), ensure_ascii=False)
    assert "конфиденциальный текст задачи" not in dumped


def test_events_cover_only_own_threads(client: TestClient) -> None:
    task = send(client, meta={"ticket": "T11"})
    lease(client)
    send(client, sender="bob", body="чужая задача")

    status = status_of(client)
    assert {event["thread_id"] for event in status["events"]} == {task["thread_id"]}
    assert [event["kind"] for event in status["events"]] == ["created", "leased"]


def test_queues_match_the_overview(client: TestClient) -> None:
    send(client)
    lease(client)

    status = status_of(client)
    overview = client.get("/v1/overview").json()
    assert status["queues"] == overview["queues"]


def test_invalid_mailbox_name_is_rejected(client: TestClient) -> None:
    for name in ("Bad_Name", "x", "with space"):
        response = client.get(f"/v1/mailboxes/{name}/status")
        assert response.status_code in (400, 422), name
