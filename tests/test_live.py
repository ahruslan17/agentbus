"""Live events: one LISTEN connection, fan-out, replay on Last-Event-ID, keepalive."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentbus import live, store
from agentbus.api import create_app
from agentbus.config import Settings

from .conftest import TEST_URL, TOKEN

KEEPALIVE = 0.2
WAIT = 5.0


def send(client: TestClient, **overrides: Any) -> dict[str, Any]:
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


def create_message(**overrides: Any) -> dict[str, Any]:
    payload = {
        "sender": "cage-claude",
        "recipient": "local-opencode",
        "kind": "task",
        "body": "сделай дело",
    }
    payload.update(overrides)
    with store.connect(TEST_URL) as conn:
        return store.create_message(conn, **payload)


async def _next(queue: asyncio.Queue[Any], timeout: float = WAIT) -> Any:
    return await asyncio.wait_for(queue.get(), timeout=timeout)


def _frames(lines: Iterator[str], deadline: float) -> Iterator[dict[str, str]]:
    """Split an SSE stream into frames: `event`/`id`/`data` fields or a comment."""
    frame: dict[str, str] = {}
    for line in lines:
        if time.monotonic() > deadline:
            raise AssertionError("no SSE frame before the deadline")
        if line == "":
            if frame:
                yield frame
                frame = {}
            continue
        if line.startswith(":"):
            frame["comment"] = line[1:].strip()
            continue
        name, _, value = line.partition(":")
        frame[name.strip()] = value.strip()


def _next_event(frames: Iterator[dict[str, str]]) -> dict[str, str]:
    for frame in frames:
        if frame.get("event") == "agentbus":
            return frame
    raise AssertionError("the stream ended before an agentbus event")


@pytest.fixture()
def live_client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, stream_keepalive_seconds=KEEPALIVE)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client


# 1
def test_hub_delivers_a_notification_from_another_connection(clean: None) -> None:
    hub = live.Hub(TEST_URL)

    async def scenario() -> None:
        queue = hub.subscribe()
        try:
            assert await asyncio.to_thread(hub.wait_ready, WAIT)
            created = await asyncio.to_thread(create_message)
            payload = await _next(queue)
            assert payload["event_id"] > 0
            assert payload["kind"] == "created"
            assert payload["message_id"] == created["id"]
            assert payload["thread_id"] == created["thread_id"]
        finally:
            hub.unsubscribe(queue)

    try:
        asyncio.run(scenario())
    finally:
        hub.close()


# 2
def test_two_subscribers_share_events_and_unsubscribe_is_isolated(clean: None) -> None:
    hub = live.Hub(TEST_URL)

    async def scenario() -> None:
        first = hub.subscribe()
        second = hub.subscribe()
        try:
            assert await asyncio.to_thread(hub.wait_ready, WAIT)
            created = await asyncio.to_thread(create_message)
            from_first = await _next(first)
            from_second = await _next(second)
            assert from_first["event_id"] == from_second["event_id"]
            assert from_first["message_id"] == created["id"]

            hub.unsubscribe(first)
            assert hub.subscriber_count == 1

            later = await asyncio.to_thread(create_message, body="второе дело")
            payload = await _next(second)
            assert payload["message_id"] == later["id"]
            with pytest.raises(asyncio.QueueEmpty):
                first.get_nowait()
        finally:
            hub.unsubscribe(first)
            hub.unsubscribe(second)

    try:
        asyncio.run(scenario())
    finally:
        hub.close()


# 3
def test_full_queue_disconnects_only_that_subscriber(clean: None) -> None:
    hub = live.Hub(TEST_URL, queue_size=64)

    async def scenario() -> None:
        slow = hub.subscribe(maxsize=1)
        fast = hub.subscribe(maxsize=8)
        try:
            assert await asyncio.to_thread(hub.wait_ready, WAIT)
            for event_id in (1, 2, 3):
                hub.publish({"event_id": event_id, "kind": "created"})
            await asyncio.sleep(0)

            assert slow.get_nowait() is live.CLOSED
            assert hub.subscriber_count == 1
            received = [await _next(fast) for _ in range(3)]
            assert received == [
                {"event_id": 1, "kind": "created"},
                {"event_id": 2, "kind": "created"},
                {"event_id": 3, "kind": "created"},
            ]
        finally:
            hub.unsubscribe(slow)
            hub.unsubscribe(fast)

    try:
        asyncio.run(scenario())
    finally:
        hub.close()


# 4
def test_stream_sends_hello_and_then_a_live_event(live_client: TestClient) -> None:
    with live_client.stream("GET", "/v1/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

        frames = _frames(response.iter_lines(), deadline=time.monotonic() + WAIT)
        hello = next(frames)
        assert hello["event"] == "hello"
        hello_cursor = json.loads(hello["data"])["last_event_id"]
        assert hello_cursor >= 0

        message = send(live_client)
        event = _next_event(frames)
        payload = json.loads(event["data"])
        assert event["event"] == "agentbus"
        assert event["id"] == str(payload["event_id"])
        assert payload["event_id"] > hello_cursor
        assert payload["kind"] == "created"
        assert payload["message_id"] == message["id"]
        assert payload["thread_id"] == message["thread_id"]


# 5
def test_last_event_id_replays_missed_events_without_duplicates(
    live_client: TestClient,
) -> None:
    messages = [send(live_client, body=f"задача {number}") for number in range(3)]
    ids = [event["id"] for event in live_client.get("/v1/events").json()["events"]]
    assert len(ids) == 3

    headers = {"Last-Event-ID": str(ids[0])}
    with live_client.stream("GET", "/v1/stream", headers=headers) as response:
        frames = _frames(response.iter_lines(), deadline=time.monotonic() + WAIT)
        hello = next(frames)
        assert json.loads(hello["data"])["last_event_id"] == ids[-1]

        replayed = [_next_event(frames) for _ in range(2)]
        assert [int(frame["id"]) for frame in replayed] == ids[1:]
        assert [json.loads(frame["data"])["message_id"] for frame in replayed] == [
            message["id"] for message in messages[1:]
        ]
        # The first event is behind the cursor and must not be sent again.
        for _ in range(3):
            assert next(frames).get("event") != "agentbus"


# 6
def test_keepalive_ping_arrives(live_client: TestClient) -> None:
    with live_client.stream("GET", "/v1/stream") as response:
        frames = _frames(response.iter_lines(), deadline=time.monotonic() + WAIT)
        assert next(frames)["event"] == "hello"
        assert next(frames).get("comment") == "ping"
