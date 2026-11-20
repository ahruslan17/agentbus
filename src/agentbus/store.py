"""All SQL lives here. Nothing above this module knows about Postgres."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

KINDS = ("task", "fix", "question", "note", "result", "verdict", "ack")
STATES = ("queued", "leased", "done", "failed", "cancelled")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        yield conn


def init_schema(conn: psycopg.Connection) -> list[str]:
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    return applied


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _message(conn: psycopg.Connection, message_id: str) -> dict[str, Any] | None:
    row = conn.execute("select * from messages where id = %s", (message_id,)).fetchone()
    return row


def create_message(
    conn: psycopg.Connection,
    *,
    sender: str,
    recipient: str,
    kind: str,
    body: str,
    meta: dict[str, Any] | None = None,
    thread_id: str | None = None,
    repo: str | None = None,
    base_ref: str | None = None,
    parent_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a message. Returns the message, or the existing one on repeated idempotency key."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")

    if idempotency_key:
        existing = conn.execute(
            "select * from messages where idempotency_key = %s", (idempotency_key,)
        ).fetchone()
        if existing:
            return existing

    parent = None
    if parent_id is not None:
        parent = _message(conn, parent_id)
        if parent is None:
            raise ValueError(f"unknown parent: {parent_id}")
        if thread_id is None:
            thread_id = parent["thread_id"]

    if thread_id is None:
        thread_id = new_id("t")
        conn.execute(
            "insert into threads (id, repo, base_ref, created_by) values (%s, %s, %s, %s)",
            (thread_id, repo, base_ref, sender),
        )
    elif conn.execute("select 1 from threads where id = %s", (thread_id,)).fetchone() is None:
        raise ValueError(f"unknown thread: {thread_id}")

    message_id = new_id("m")
    row = conn.execute(
        """
        insert into messages (id, thread_id, sender, recipient, kind, body, meta,
                              parent_id, idempotency_key)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            message_id,
            thread_id,
            sender,
            recipient,
            kind,
            body,
            Jsonb(meta or {}),
            parent_id,
            idempotency_key,
        ),
    ).fetchone()
    assert row is not None

    if parent is not None:
        _close(conn, parent_id)

    session_id = (meta or {}).get("session_id")
    if session_id and thread_id:
        conn.execute("update threads set session_id = %s where id = %s", (session_id, thread_id))
    return row


def _close(conn: psycopg.Connection, message_id: str | None) -> None:
    if message_id is None:
        return
    conn.execute(
        """
        update messages
           set state = 'done', lease_until = null, lease_owner = null, updated_at = now()
         where id = %s and state in ('queued', 'leased')
        """,
        (message_id,),
    )


def reap_expired_leases(conn: psycopg.Connection) -> int:
    rows = conn.execute(
        """
        update messages
           set state = 'queued', lease_owner = null, lease_until = null, updated_at = now()
         where state = 'leased' and lease_until is not null and lease_until < now()
        returning id
        """
    ).fetchall()
    return len(rows)


def lease_message(
    conn: psycopg.Connection,
    *,
    agent: str,
    ttl_seconds: int,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """Atomically hand the oldest queued message for `agent` to it."""
    reap_expired_leases(conn)
    kind_filter = ""
    params: list[Any] = [agent, ttl_seconds, agent]
    if kinds:
        kind_filter = "and kind = any(%s)"
        params.append(list(kinds))

    row = conn.execute(
        f"""
        update messages
           set state = 'leased',
               lease_owner = %s,
               lease_until = now() + make_interval(secs => %s),
               attempt = attempt + 1,
               updated_at = now()
         where id = (
               select id from messages
                where recipient = %s and state = 'queued' {kind_filter}
                order by created_at
                  for update skip locked
                limit 1
         )
        returning *
        """,
        params,
    ).fetchone()
    return row


def heartbeat(
    conn: psycopg.Connection, *, message_id: str, ttl_seconds: int
) -> dict[str, Any] | None:
    return conn.execute(
        """
        update messages
           set lease_until = now() + make_interval(secs => %s), updated_at = now()
         where id = %s and state = 'leased'
        returning *
        """,
        (ttl_seconds, message_id),
    ).fetchone()


def get_message(conn: psycopg.Connection, message_id: str) -> dict[str, Any] | None:
    return _message(conn, message_id)


def get_thread(conn: psycopg.Connection, thread_id: str) -> dict[str, Any] | None:
    return conn.execute("select * from threads where id = %s", (thread_id,)).fetchone()


def thread_messages(conn: psycopg.Connection, thread_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        "select * from messages where thread_id = %s order by created_at, id", (thread_id,)
    ).fetchall()


def reply(
    conn: psycopg.Connection,
    *,
    parent_id: str,
    sender: str,
    kind: str,
    body: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a reply to `parent` and close the parent as done. Reply goes back to its sender."""
    parent = _message(conn, parent_id)
    if parent is None:
        raise ValueError(f"unknown message: {parent_id}")

    return create_message(
        conn,
        sender=sender,
        recipient=parent["sender"],
        kind=kind,
        body=body,
        meta=meta,
        thread_id=parent["thread_id"],
        parent_id=parent_id,
    )


def set_state(
    conn: psycopg.Connection, *, message_id: str, state: str, note: str | None = None
) -> dict[str, Any] | None:
    if state not in ("done", "failed", "cancelled"):
        raise ValueError(f"terminal state expected, got {state}")
    row = conn.execute(
        """
        update messages
           set state = %s,
               meta = case when %s::text is null then meta
                           else meta || jsonb_build_object('state_note', %s::text) end,
               lease_until = null,
               lease_owner = null,
               updated_at = now()
         where id = %s
        returning *
        """,
        (state, note, note, message_id),
    ).fetchone()
    return row


def inbox(conn: psycopg.Connection, *, agent: str, limit: int = 50) -> list[dict[str, Any]]:
    return conn.execute(
        """
        select * from messages
         where recipient = %s and state in ('queued', 'leased')
         order by created_at
         limit %s
        """,
        (agent, limit),
    ).fetchall()
