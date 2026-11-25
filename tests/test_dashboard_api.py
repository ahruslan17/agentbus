"""Read API of the dashboard, against a real Postgres and a temp artifacts dir."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from agentbus.api import create_app

from .conftest import TOKEN

LEASE_URL = "/v1/inbox/local-opencode/lease"


def send(client: TestClient, **overrides) -> dict:
    payload = {
        "sender": "cage-claude",
        "recipient": "local-opencode",
        "kind": "task",
        "body": "do the thing",
    }
    payload.update(overrides)
    response = client.post("/v1/messages", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def lease(client: TestClient, **params) -> dict:
    response = client.post(LEASE_URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def reply(
    client: TestClient,
    message: dict,
    *,
    sender: str = "local-opencode",
    body: str = "done",
    meta: dict | None = None,
) -> dict:
    response = client.post(
        f"/v1/messages/{message['id']}/reply",
        json={"sender": sender, "body": body, "meta": meta or {}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def finish(client: TestClient, message: dict, note: str | None = None) -> dict:
    response = client.post(
        f"/v1/messages/{message['id']}/state",
        json={"state": "done", "note": note},
    )
    assert response.status_code == 200, response.text
    return response.json()


def counts(rows: list[dict], field: str, value: str) -> dict:
    return next(row for row in rows if row[field] == value)


# 3
def test_overview_counts_queue_and_awaiting_review(client: TestClient) -> None:
    task = send(client)
    lease(client)

    overview = client.get("/v1/overview").json()
    assert counts(overview["queues"], "recipient", "local-opencode") == {
        "recipient": "local-opencode",
        "queued": 0,
        "leased": 1,
    }
    assert overview["awaiting_review"] == []
    assert len(overview["recent_events"]) <= 20
    assert overview["recent_events"][-1]["kind"] == "leased"

    reply(client, task)

    overview = client.get("/v1/overview").json()
    assert [row["recipient"] for row in overview["queues"]] == ["cage-claude"]
    assert counts(overview["queues"], "recipient", "cage-claude")["queued"] == 1
    assert counts(overview["awaiting_review"], "recipient", "cage-claude")["count"] == 1
    assert overview["recent_events"][-1]["kind"] == "done"


# 4
def test_mailboxes_show_tasks_adopted_and_awaiting_review(client: TestClient) -> None:
    task = send(client, sender="alice", body="first")
    lease(client)

    boxes = {row["mailbox"]: row for row in client.get("/v1/mailboxes").json()["mailboxes"]}
    assert boxes["alice"]["tasks"] == 1
    assert boxes["alice"]["in_work"] == 1
    assert boxes["alice"]["awaiting_review"] == 0
    assert boxes["alice"]["accepted"] == 0

    result = reply(client, task)

    boxes = {row["mailbox"]: row for row in client.get("/v1/mailboxes").json()["mailboxes"]}
    assert boxes["alice"]["in_work"] == 0
    assert boxes["alice"]["awaiting_review"] == 1

    finish(client, result, note="accept: ok")

    boxes = {row["mailbox"]: row for row in client.get("/v1/mailboxes").json()["mailboxes"]}
    assert boxes["alice"]["accepted"] == 1
    assert boxes["alice"]["awaiting_review"] == 0
    assert boxes["alice"]["last_activity"] is not None

    send(client, sender="bob", body="second")
    boxes = {row["mailbox"]: row for row in client.get("/v1/mailboxes").json()["mailboxes"]}
    assert boxes["bob"]["tasks"] == 1
    assert boxes["bob"]["awaiting_review"] == 0
    assert boxes["bob"]["accepted"] == 0


# 5
def test_threads_filters_ticket_and_title(client: TestClient) -> None:
    first = send(
        client,
        sender="alice",
        repo="/repo/a",
        body="First line\nsecond line",
        meta={"ticket": "T5.1"},
    )
    reply(client, first)
    second = send(client, sender="bob", repo="/repo/b", body="Other work")

    everything = client.get("/v1/threads").json()["threads"]
    assert {row["id"] for row in everything} == {first["thread_id"], second["thread_id"]}

    by_mailbox = client.get("/v1/threads", params={"mailbox": "alice"}).json()["threads"]
    assert [row["id"] for row in by_mailbox] == [first["thread_id"]]
    assert by_mailbox[0]["ticket"] == "T5.1"
    assert by_mailbox[0]["title"] == "First line"
    assert by_mailbox[0]["repo"] == "/repo/a"
    assert by_mailbox[0]["messages"] == 2
    assert by_mailbox[0]["last_kind"] == "result"
    assert by_mailbox[0]["last_state"] == "queued"

    review = client.get("/v1/threads", params={"state": "review"}).json()["threads"]
    assert [row["id"] for row in review] == [first["thread_id"]]

    found = client.get("/v1/threads", params={"q": "SECOND line"}).json()["threads"]
    assert [row["id"] for row in found] == [first["thread_id"]]

    by_repo = client.get("/v1/threads", params={"repo": "/repo/b"}).json()["threads"]
    assert [row["id"] for row in by_repo] == [second["thread_id"]]

    assert client.get("/v1/threads", params={"state": "nope"}).status_code == 422


# 6
def test_timeline_orders_messages_events_and_artifacts(client: TestClient, artifacts: str) -> None:
    task = send(client)
    lease(client)
    result = reply(client, task)

    (Path(artifacts) / task["id"]).mkdir(parents=True)
    (Path(artifacts) / task["id"] / "patch.diff").write_text("diff", encoding="utf-8")
    (Path(artifacts) / result["id"]).mkdir(parents=True)
    (Path(artifacts) / result["id"] / "run.log").write_text("{}", encoding="utf-8")

    timeline = client.get(f"/v1/threads/{task['thread_id']}/timeline").json()

    assert timeline["thread"]["id"] == task["thread_id"]
    assert [message["id"] for message in timeline["messages"]] == [task["id"], result["id"]]
    assert [event["kind"] for event in timeline["events"]] == [
        "created",
        "leased",
        "created",
        "done",
    ]
    assert timeline["messages"][0]["artifacts"] == ["patch.diff"]
    assert timeline["messages"][1]["artifacts"] == ["run.log"]

    assert client.get("/v1/threads/t_missing/timeline").status_code == 404


# 7
def test_log_cursor_reply_fallback_and_running(client: TestClient, artifacts: str) -> None:
    task = send(client)
    directory = Path(artifacts) / task["id"]
    directory.mkdir(parents=True)
    (directory / "run.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "step_start", "timestamp": 1}),
                json.dumps(
                    {"type": "text", "timestamp": 2, "part": {"text": "done", "time": {"end": 2}}}
                ),
                json.dumps(
                    {
                        "type": "tool_use",
                        "part": {
                            "tool": "shell",
                            "state": {"status": "completed", "input": {"command": "make check"}},
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    lease(client)
    first = client.get(f"/v1/messages/{task['id']}/log").json()
    assert [row["type"] for row in first["lines"]] == ["text", "tool"]
    assert first["next"] == 3
    assert first["running"] is True

    tail = client.get(f"/v1/messages/{task['id']}/log", params={"after": first["next"]}).json()
    assert tail["lines"] == []
    assert tail["next"] == first["next"]

    result = reply(client, task)
    shutil.rmtree(directory)
    reply_dir = Path(artifacts) / result["id"]
    reply_dir.mkdir(parents=True)
    (reply_dir / "run.log").write_text(
        json.dumps({"type": "step_finish", "part": {"tokens": {"input": 1, "output": 2}}}) + "\n",
        encoding="utf-8",
    )

    fallback = client.get(f"/v1/messages/{task['id']}/log").json()
    assert [row["type"] for row in fallback["lines"]] == ["step"]
    assert fallback["running"] is False
    assert fallback["next"] == 1

    assert client.get("/v1/messages/m_missing/log").status_code == 404


# 8
def test_stats_by_mailbox_and_model(client: TestClient) -> None:
    accepted = send(client, body="accepted right away")
    lease(client)
    accepted_result = reply(
        client,
        accepted,
        meta={"model": "deepseek", "usage": {"tokens": {"input": 10, "output": 5}}},
    )
    finish(client, accepted_result, note="accept: ok")

    reworked = send(client, body="needs rework")
    lease(client)
    first_result = reply(
        client,
        reworked,
        meta={"model": "deepseek", "usage": {"tokens": {"input": 2, "output": 1}}},
    )
    verdict = client.post(
        "/v1/messages",
        json={
            "sender": "cage-claude",
            "recipient": "local-opencode",
            "kind": "verdict",
            "body": "rework",
            "thread_id": reworked["thread_id"],
            "parent_id": first_result["id"],
            "meta": {"decision": "rework"},
        },
    )
    assert verdict.status_code == 201, verdict.text
    second_result = client.post(
        "/v1/messages",
        json={
            "sender": "local-opencode",
            "recipient": "cage-claude",
            "kind": "result",
            "body": "reworked",
            "thread_id": reworked["thread_id"],
            "parent_id": verdict.json()["id"],
            "meta": {"model": "deepseek", "usage": {"tokens": {"input": 3, "output": 2}}},
        },
    ).json()
    finish(client, second_result, note="accept: ok")

    by_mailbox = client.get("/v1/stats", params={"group": "mailbox"}).json()
    assert by_mailbox["group"] == "mailbox"
    row = counts(by_mailbox["rows"], "key", "cage-claude")
    assert row["tickets"] == 2
    assert row["results"] == 3
    assert row["accepted_first_review"] == 1
    assert row["rework_verdicts"] == 1
    assert row["timeouts"] == 0
    assert row["failed"] == 0
    assert row["tokens_input"] == 15
    assert row["tokens_output"] == 8
    assert row["median_queue_seconds"] is not None
    assert row["median_run_seconds"] is not None

    by_model = client.get("/v1/stats", params={"group": "model"}).json()
    model_row = counts(by_model["rows"], "key", "deepseek")
    assert model_row["tickets"] == 2
    assert model_row["results"] == 3
    assert model_row["accepted_first_review"] == 1
    assert model_row["rework_verdicts"] == 1

    by_repo = client.get("/v1/stats", params={"group": "repo"}).json()
    assert by_repo["rows"] == []  # threads carry no repo here: empty, not a crash

    assert client.get("/v1/stats", params={"group": "week"}).status_code == 422


# 9
def test_host_guard_and_browser_login(settings) -> None:
    client = TestClient(create_app(settings), headers={"Authorization": f"Bearer {TOKEN}"})
    foreign = client.get("/v1/overview", headers={"Host": "evil.example"})
    assert foreign.status_code == 421

    anon = TestClient(create_app(settings))

    assert anon.get("/ui/login", params={"token": "wrong"}).status_code == 401

    login = anon.get("/ui/login", params={"token": TOKEN}, follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/ui/"
    cookie = login.headers["set-cookie"].lower()
    assert "agentbus_token=" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie

    assert anon.get("/v1/overview").status_code == 200
    assert anon.post("/v1/messages", json={"sender": "x", "body": "y"}).status_code == 401

    page = anon.get("/ui/")
    assert page.status_code == 200
    assert "agentbus" in page.text
    assert "UI — T6" in page.text


# 10
def test_log_tail_waits_for_the_unfinished_last_line(client: TestClient, artifacts: str) -> None:
    task = send(client)
    directory = Path(artifacts) / task["id"]
    directory.mkdir(parents=True)
    done = json.dumps(
        {"type": "text", "timestamp": 1, "part": {"text": "done", "time": {"end": 1}}}
    )
    pending = json.dumps({"type": "step_finish", "part": {"tokens": {"input": 1, "output": 2}}})
    log = directory / "run.log"
    log.write_text(done + "\n" + pending, encoding="utf-8")

    first = client.get(f"/v1/messages/{task['id']}/log").json()
    assert [row["type"] for row in first["lines"]] == ["text"]
    assert first["next"] == 1

    log.write_text(done + "\n" + pending + "\n", encoding="utf-8")
    rest = client.get(f"/v1/messages/{task['id']}/log", params={"after": first["next"]}).json()
    assert [row["type"] for row in rest["lines"]] == ["step"]
    assert rest["next"] == 2
