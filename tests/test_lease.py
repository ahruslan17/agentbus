"""Lease behaviour for a pool of parallel executors, against a real Postgres."""

from __future__ import annotations

import threading
from dataclasses import replace

from fastapi.testclient import TestClient

from agentbus import store
from agentbus.api import create_app

from .conftest import TEST_URL, TOKEN

LEASE_URL = "/v1/inbox/local-opencode/lease"


def send(client: TestClient, **overrides):
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


def lease(client: TestClient, **params):
    return client.post(LEASE_URL, params=params)


def reply(client: TestClient, message: dict) -> None:
    response = client.post(
        f"/v1/messages/{message['id']}/reply",
        json={"sender": "local-opencode", "body": "готово"},
    )
    assert response.status_code == 201, response.text


def test_one_thread_is_leased_once_at_a_time(client: TestClient) -> None:
    task = send(client, kind="task", body="работа")
    note = send(client, kind="note", body="заметка", thread_id=task["thread_id"])

    first = lease(client).json()
    assert first["id"] == task["id"]
    assert lease(client).status_code == 204

    reply(client, task)

    second = lease(client).json()
    assert second["id"] == note["id"]


def test_separate_threads_are_leased_back_to_back(client: TestClient) -> None:
    first_sent = send(client, body="первый тред")
    second_sent = send(client, body="второй тред")

    first = lease(client).json()
    second = lease(client).json()

    assert {first["id"], second["id"]} == {first_sent["id"], second_sent["id"]}
    assert first["thread_id"] != second["thread_id"]


def test_lease_is_fair_between_senders(client: TestClient) -> None:
    a1 = send(client, body="A1")
    a2 = send(client, body="A2")
    a3 = send(client, body="A3")
    b1 = send(client, sender="other-claude", body="B1")

    assert lease(client).json()["id"] == a1["id"]
    assert lease(client).json()["id"] == b1["id"]
    assert lease(client).json()["id"] == a2["id"]
    assert client.get(f"/v1/messages/{a3['id']}").json()["state"] == "queued"


def test_per_sender_limit_caps_one_sender(settings) -> None:
    limited = TestClient(
        create_app(replace(settings, per_sender_leases=1)),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    first_sent = send(limited, body="первая")
    second_sent = send(limited, body="вторая")

    first = lease(limited).json()
    assert first["id"] == first_sent["id"]
    assert lease(limited).status_code == 204

    reply(limited, first)

    second = lease(limited).json()
    assert second["id"] == second_sent["id"]


def test_owner_is_recorded_and_validated(client: TestClient) -> None:
    send(client)

    leased = lease(client, owner="local-opencode#2")
    assert leased.status_code == 200
    assert leased.json()["lease_owner"] == "local-opencode#2"

    assert lease(client, owner="плохой владелец").status_code == 422
    assert lease(client, owner="x" * 65).status_code == 422


def test_concurrent_leases_never_hand_out_the_same_message(client: TestClient) -> None:
    shared = send(client, kind="task", body="общий тред 1")
    for number in range(2, 5):
        send(client, kind="note", body=f"общий тред {number}", thread_id=shared["thread_id"])
    for number in range(4):
        send(client, body=f"свой тред {number}")

    barrier = threading.Barrier(8)
    results: list[dict | None] = []
    guard = threading.Lock()

    def run() -> None:
        barrier.wait()
        with store.connect(TEST_URL) as conn:
            row = store.lease_message(conn, agent="local-opencode", ttl_seconds=60)
        with guard:
            results.append(row)

    workers = [threading.Thread(target=run) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    leased = [row for row in results if row is not None]
    assert len(leased) == 5
    assert len({row["id"] for row in leased}) == 5
    assert sum(1 for row in leased if row["thread_id"] == shared["thread_id"]) == 1


def test_expired_lease_frees_the_thread(client: TestClient) -> None:
    task = send(client, kind="task", body="работа")
    send(client, kind="note", body="заметка", thread_id=task["thread_id"])

    assert lease(client).json()["id"] == task["id"]
    assert lease(client).status_code == 204

    with store.connect(TEST_URL) as conn:
        conn.execute("update messages set lease_until = now() - interval '1 minute'")

    again = lease(client).json()
    assert again["id"] == task["id"]
    assert again["attempt"] == 2


def release(client: TestClient, message_id: str, owner: str):
    return client.post(f"/v1/messages/{message_id}/release", json={"owner": owner})


def test_release_by_its_owner_returns_the_message_to_the_queue(client: TestClient) -> None:
    task = send(client, kind="task", body="работа")
    leased = lease(client, owner="local-opencode#2").json()
    assert leased["id"] == task["id"]
    assert leased["attempt"] == 1

    response = release(client, task["id"], "local-opencode#2")

    assert response.status_code == 200
    row = response.json()
    assert row["state"] == "queued"
    assert row["lease_owner"] is None
    assert row["lease_until"] is None
    assert row["attempt"] == 0

    events = client.get(f"/v1/threads/{task['thread_id']}/events").json()["events"]
    assert events[-1]["kind"] == "released"
    assert events[-1]["actor"] == "local-opencode#2"

    # the released message is handed out again right away, with a fresh attempt
    again = lease(client, owner="local-opencode#2").json()
    assert again["id"] == task["id"]
    assert again["attempt"] == 1


def test_release_by_a_foreign_owner_is_rejected(client: TestClient) -> None:
    task = send(client)
    lease(client, owner="local-opencode#2")

    response = release(client, task["id"], "local-opencode#3")

    assert response.status_code == 409
    row = client.get(f"/v1/messages/{task['id']}").json()
    assert row["state"] == "leased"
    assert row["lease_owner"] == "local-opencode#2"


def test_release_of_a_message_that_is_not_leased_is_rejected(client: TestClient) -> None:
    task = send(client)

    assert release(client, task["id"], "local-opencode#2").status_code == 409
    assert client.get(f"/v1/messages/{task['id']}").json()["state"] == "queued"


def test_release_validates_the_owner(client: TestClient) -> None:
    task = send(client)

    assert release(client, task["id"], "плохой владелец").status_code == 422
    assert release(client, task["id"], "x" * 65).status_code == 422
