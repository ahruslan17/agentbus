"""Live events: one `LISTEN agentbus` connection per API process, fanned out.

The listener owns a single Postgres connection in its own thread. Subscribers are
asyncio queues living in the event loop that asked for them; the thread only ever
touches them through `loop.call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import psycopg

log = logging.getLogger(__name__)

CHANNEL = "agentbus"
QUEUE_SIZE = 1000
POLL_TIMEOUT = 0.5
RECONNECT_PAUSE = 2.0

CLOSED = object()
"""Sentinel put into a subscriber queue when the hub drops or shuts it down."""

Payload = dict[str, Any]


class Hub:
    """One `LISTEN agentbus` connection, many subscribers, one thread.

    The thread starts lazily at the first `subscribe()` and stops in `close()`.
    A payload that does not fit into a slow subscriber's queue disconnects that
    subscriber only: its queue is drained and gets `CLOSED` so the reader ends.
    """

    def __init__(
        self,
        database_url: str,
        *,
        queue_size: int = QUEUE_SIZE,
        poll_timeout: float = POLL_TIMEOUT,
        reconnect_pause: float = RECONNECT_PAUSE,
    ) -> None:
        self._database_url = database_url
        self._queue_size = queue_size
        self._poll_timeout = poll_timeout
        self._reconnect_pause = reconnect_pause
        self._subscribers: dict[asyncio.Queue[Any], asyncio.AbstractEventLoop] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self, *, maxsize: int | None = None) -> asyncio.Queue[Any]:
        """Register a new queue and start the listener thread if it is not running."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=self._queue_size if maxsize is None else maxsize
        )
        with self._lock:
            self._subscribers[queue] = loop
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._listen, name="agentbus-live", daemon=True
                )
                self._thread.start()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        with self._lock:
            self._subscribers.pop(queue, None)

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Block until the first LISTEN is established. Call from a worker thread."""
        return self._ready.wait(timeout)

    def publish(self, payload: Payload) -> None:
        """Fan a payload out to every subscriber. Safe to call from any thread."""
        with self._lock:
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(self._enqueue, queue, payload)
            except RuntimeError:  # the loop is gone already
                self.unsubscribe(queue)

    def close(self) -> None:
        """Stop the listener thread and wake every subscriber."""
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._poll_timeout + self._reconnect_pause)
        with self._lock:
            subscribers = list(self._subscribers.items())
        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(self._enqueue_closed, queue)
            except RuntimeError:
                self.unsubscribe(queue)

    def _listen(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                with psycopg.connect(self._database_url, autocommit=True) as conn:
                    conn.execute(f"listen {CHANNEL}")
                    self._ready.set()
                    if not first:
                        log.info("live: reconnected to postgres")
                        self.publish({"type": "reconnected"})
                    first = False
                    while not self._stop.is_set():
                        for notify in conn.notifies(timeout=self._poll_timeout):
                            self._deliver_raw(notify.payload)
            except psycopg.Error as exc:
                if self._stop.is_set():
                    break
                log.warning(
                    "live: listen failed, retrying in %.1fs: %s", self._reconnect_pause, exc
                )
            self._stop.wait(self._reconnect_pause)

    def _deliver_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            log.warning("live: cannot parse notification: %r", raw)
            return
        if isinstance(payload, dict):
            self.publish(payload)

    def _enqueue(self, queue: asyncio.Queue[Any], payload: Payload) -> None:
        """Runs on the subscriber's loop."""
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("live: subscriber queue is full, disconnecting it")
            self.unsubscribe(queue)
            self._enqueue_closed(queue)

    @staticmethod
    def _enqueue_closed(queue: asyncio.Queue[Any]) -> None:
        """Make room for `CLOSED` so a blocked reader wakes up and stops."""
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            queue.put_nowait(CLOSED)
        except asyncio.QueueFull:  # pragma: no cover - only if maxsize is zero
            pass
