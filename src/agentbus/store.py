"""All SQL lives here. Nothing above this module knows about Postgres."""

from __future__ import annotations

import json
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

# One global mutex for the whole lease transaction (reap + pick + update). Serialising
# leases here is cheap: a lease is a single short statement, and without the mutex two
# executors could pick two messages of the same thread or race on the fairness count.
LEASE_LOCK_KEY = 0x61627573  # "abus"


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


def _event(
    conn: psycopg.Connection,
    *,
    kind: str,
    message_id: str | None,
    thread_id: str | None,
    actor: str | None,
    data: dict[str, Any] | None,
) -> None:
    """Append to the journal and notify listeners in the same transaction."""
    row = conn.execute(
        """
        insert into events (message_id, thread_id, actor, kind, data)
        values (%s, %s, %s, %s, %s)
        returning id
        """,
        (message_id, thread_id, actor, kind, Jsonb(data or {})),
    ).fetchone()
    assert row is not None
    payload = json.dumps(
        {
            "event_id": row["id"],
            "kind": kind,
            "message_id": message_id,
            "thread_id": thread_id,
        },
        ensure_ascii=False,
    )
    conn.execute("select pg_notify('agentbus', %s)", (payload,))


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

    _event(
        conn,
        kind="created",
        message_id=message_id,
        thread_id=thread_id,
        actor=sender,
        data={"kind": kind, "recipient": recipient, "parent_id": parent_id},
    )

    if parent is not None:
        _close(conn, parent_id)

    session_id = (meta or {}).get("session_id")
    if session_id and thread_id:
        conn.execute("update threads set session_id = %s where id = %s", (session_id, thread_id))
    return row


def _close(conn: psycopg.Connection, message_id: str | None) -> dict[str, Any] | None:
    """Close the message as done. Returns the row if this call really closed it."""
    if message_id is None:
        return None
    row = conn.execute(
        """
        update messages
           set state = 'done', lease_until = null, lease_owner = null, updated_at = now()
         where id = %s and state in ('queued', 'leased')
        returning id, thread_id
        """,
        (message_id,),
    ).fetchone()
    if row is not None:
        _event(
            conn,
            kind="done",
            message_id=row["id"],
            thread_id=row["thread_id"],
            actor="system",
            data={"reason": "replied"},
        )
    return row


def reap_expired_leases(conn: psycopg.Connection) -> int:
    rows = conn.execute(
        """
        with expired as (
            select id, thread_id, lease_owner
              from messages
             where state = 'leased' and lease_until is not null and lease_until < now()
        )
        update messages m
           set state = 'queued', lease_owner = null, lease_until = null, updated_at = now()
          from expired
         where m.id = expired.id
        returning m.id, expired.thread_id, expired.lease_owner
        """
    ).fetchall()
    for row in rows:
        _event(
            conn,
            kind="requeued",
            message_id=row["id"],
            thread_id=row["thread_id"],
            actor="system",
            data={"previous_owner": row["lease_owner"]},
        )
    return len(rows)


def lease_message(
    conn: psycopg.Connection,
    *,
    agent: str,
    ttl_seconds: int,
    kinds: tuple[str, ...] | None = None,
    owner: str | None = None,
    per_sender_limit: int = 0,
) -> dict[str, Any] | None:
    """Atomically hand one queued message for `agent` to it.

    The whole lease (reap, pick, update) runs in the caller's transaction under
    `LEASE_LOCK_KEY`, so the lock lives until the transaction commits. A message is only
    handed out if nothing else in its thread is leased: one thread means one running
    executor. Among the rest, the sender with the fewest current leases for this
    recipient goes first, then the oldest message. `per_sender_limit > 0` caps how many
    messages of one sender may be leased to this recipient at once.
    """
    if conn.autocommit:
        raise RuntimeError("lease_message needs a transaction: connection must not be autocommit")
    conn.execute("select pg_advisory_xact_lock(%s::bigint)", (LEASE_LOCK_KEY,))
    reap_expired_leases(conn)

    kind_filter = ""
    params: dict[str, Any] = {
        "agent": agent,
        "owner": owner or agent,
        "ttl": ttl_seconds,
        "limit": per_sender_limit,
    }
    if kinds:
        kind_filter = "and m.kind = any(%(kinds)s)"
        params["kinds"] = list(kinds)

    row = conn.execute(
        f"""
        with candidates as (
            select m.id, m.sender, m.created_at,
                   (select count(*)
                      from messages held
                     where held.recipient = %(agent)s
                       and held.state = 'leased'
                       and held.sender = m.sender) as sender_leases
              from messages m
             where m.recipient = %(agent)s
               and m.state = 'queued'
               {kind_filter}
               and (m.thread_id is null
                    or not exists (select 1
                                     from messages busy
                                    where busy.thread_id = m.thread_id
                                      and busy.state = 'leased'))
        )
        update messages
           set state = 'leased',
               lease_owner = %(owner)s,
               lease_until = now() + make_interval(secs => %(ttl)s),
               attempt = attempt + 1,
               updated_at = now()
         where id = (
               select id from candidates
                where %(limit)s <= 0 or sender_leases < %(limit)s
                order by sender_leases, created_at, id
                limit 1
         )
           and state = 'queued'
        returning *
        """,
        params,
    ).fetchone()
    if row is not None:
        _event(
            conn,
            kind="leased",
            message_id=row["id"],
            thread_id=row["thread_id"],
            actor=params["owner"],
            data={"attempt": row["attempt"]},
        )
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


def thread_events(conn: psycopg.Connection, thread_id: str) -> list[dict[str, Any]]:
    return conn.execute(
        "select * from events where thread_id = %s order by id", (thread_id,)
    ).fetchall()


def recent_events(
    conn: psycopg.Connection, after_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    return conn.execute(
        "select * from events where id > %s order by id limit %s",
        (after_id or 0, limit),
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
    if row is not None:
        _event(
            conn,
            kind=state,
            message_id=row["id"],
            thread_id=row["thread_id"],
            actor="system",
            data={"note": note},
        )
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


def upsert_worker(
    conn: psycopg.Connection,
    *,
    id: str,
    agent: str,
    host: str | None,
    pid: int | None,
    model: str | None,
    harness: str | None,
    state: str,
    message_id: str | None,
) -> dict[str, Any]:
    """Insert or refresh a worker slot. `started_at` moves only when `pid` changes."""
    row = conn.execute(
        """
        insert into workers (id, agent, host, pid, model, harness, state, message_id)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (id) do update
           set agent = excluded.agent,
               host = excluded.host,
               pid = excluded.pid,
               model = excluded.model,
               harness = excluded.harness,
               state = excluded.state,
               message_id = excluded.message_id,
               heartbeat_at = now(),
               started_at = case when workers.pid is distinct from excluded.pid
                                 then now() else workers.started_at end
        returning *
        """,
        (id, agent, host, pid, model, harness, state, message_id),
    ).fetchone()
    assert row is not None
    return row


def list_workers(conn: psycopg.Connection, *, lease_ttl_seconds: int) -> list[dict[str, Any]]:
    return conn.execute(
        """
        select *, heartbeat_at < now() - make_interval(secs => %s) as stale
          from workers
         order by id
        """,
        (lease_ttl_seconds * 2,),
    ).fetchall()


# --- dashboard reads -------------------------------------------------------------


def overview(conn: psycopg.Connection, *, lease_ttl_seconds: int) -> dict[str, Any]:
    """Everything the "now" page needs: slots, queues and the last events."""
    queues = conn.execute(
        """
        select recipient,
               count(*) filter (where state = 'queued') as queued,
               count(*) filter (where state = 'leased') as leased
          from messages
         where state in ('queued', 'leased')
         group by recipient
         order by recipient
        """
    ).fetchall()
    awaiting = conn.execute(
        """
        select recipient, count(*) as count
          from messages
         where kind = 'result' and state = 'queued'
         group by recipient
         order by recipient
        """
    ).fetchall()
    return {
        "workers": list_workers(conn, lease_ttl_seconds=lease_ttl_seconds),
        "queues": queues,
        "awaiting_review": awaiting,
        "recent_events": recent_events(conn, limit=20),
    }


def mailboxes(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """One card per session that ever sent a task."""
    return conn.execute(
        """
        with boxes as (
            select sender as mailbox,
                   array_agg(distinct thread_id) filter (where thread_id is not null) as threads
              from messages
             where kind = 'task'
             group by sender
        )
        select b.mailbox,
               (select count(*) from messages m
                 where m.sender = b.mailbox and m.kind = 'task') as tasks,
               (select count(distinct m.thread_id) from messages m
                 where m.thread_id = any(b.threads) and m.state = 'leased') as in_work,
               (select count(*) from messages m
                 where m.thread_id = any(b.threads)
                   and m.kind = 'result' and m.state = 'queued') as awaiting_review,
               (select count(*) from messages m
                 where m.thread_id = any(b.threads)
                   and m.kind = 'result'
                   and m.meta->>'state_note' like 'accept%') as accepted,
               (select count(*) from messages m
                 where m.thread_id = any(b.threads) and m.state = 'failed') as failed,
               (select max(m.updated_at) from messages m
                 where m.thread_id = any(b.threads)) as last_activity
          from boxes b
         order by b.mailbox
        """
    ).fetchall()


THREAD_STATE_FILTERS = {
    "active": (
        "exists (select 1 from messages m "
        "where m.thread_id = t.id and m.state in ('queued', 'leased'))"
    ),
    "review": "last.kind = 'result' and last.state = 'queued'",
    "done": (
        "not exists (select 1 from messages m "
        "where m.thread_id = t.id and m.state in ('queued', 'leased')) "
        "and not exists (select 1 from messages m "
        "where m.thread_id = t.id and m.state = 'failed')"
    ),
    "failed": "exists (select 1 from messages m where m.thread_id = t.id and m.state = 'failed')",
}


def list_threads(
    conn: psycopg.Connection,
    *,
    mailbox: str | None = None,
    repo: str | None = None,
    state: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """History page: one row per thread with the facts a table shows."""
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if mailbox:
        conditions.append(
            "(t.created_by = %(mailbox)s or exists ("
            "select 1 from messages m where m.thread_id = t.id "
            "and m.kind = 'task' and m.sender = %(mailbox)s))"
        )
        params["mailbox"] = mailbox
    if repo:
        conditions.append("t.repo = %(repo)s")
        params["repo"] = repo
    if state:
        if state not in THREAD_STATE_FILTERS:
            raise ValueError(f"unknown thread state: {state}")
        conditions.append(f"({THREAD_STATE_FILTERS[state]})")
    if q:
        conditions.append(
            "exists (select 1 from messages m where m.thread_id = t.id and m.body ilike %(q)s)"
        )
        params["q"] = f"%{q}%"
    where = "where " + " and ".join(conditions) if conditions else ""

    rows = conn.execute(
        f"""
        select t.id, t.repo, t.created_by,
               task.meta->>'ticket' as ticket,
               task.body as first_body,
               counts.messages,
               coalesce(counts.attempts, 0) as attempts,
               last.kind as last_kind,
               last.state as last_state,
               coalesce(counts.updated_at, t.created_at) as updated_at,
               last_result.meta->>'model' as model
          from threads t
          left join lateral (
                select m.meta, m.body from messages m
                 where m.thread_id = t.id and m.kind = 'task'
                 order by m.created_at, m.id
                 limit 1
          ) task on true
          left join lateral (
                select count(*) as messages, max(m.attempt) as attempts,
                       max(m.updated_at) as updated_at
                  from messages m
                 where m.thread_id = t.id
          ) counts on true
          left join lateral (
                select m.kind, m.state from messages m
                 where m.thread_id = t.id
                 order by m.created_at desc, m.id desc
                 limit 1
          ) last on true
          left join lateral (
                select m.meta from messages m
                 where m.thread_id = t.id and m.kind = 'result'
                 order by m.created_at desc, m.id desc
                 limit 1
          ) last_result on true
          {where}
         order by updated_at desc, t.id desc
         limit %(limit)s offset %(offset)s
        """,
        params,
    ).fetchall()
    for row in rows:
        row["title"] = _first_line(row.pop("first_body", None))
    return rows


def thread_timeline(conn: psycopg.Connection, thread_id: str) -> dict[str, Any] | None:
    thread = get_thread(conn, thread_id)
    if thread is None:
        return None
    return {
        "thread": thread,
        "messages": thread_messages(conn, thread_id),
        "events": thread_events(conn, thread_id),
    }


def first_reply_id(conn: psycopg.Connection, message_id: str) -> str | None:
    """The result that answers `message_id`, if any. Artifacts move under its id."""
    row = conn.execute(
        """
        select id from messages
         where parent_id = %s and kind = 'result'
         order by created_at, id
         limit 1
        """,
        (message_id,),
    ).fetchone()
    return row["id"] if row else None


# Per-thread facts shared by every statistic. A thread belongs to one mailbox (the
# sender of its first task), one repo and one model (the one of its latest result).
_STATS_FACTS = """
with facts as (
    select t.id as thread_id,
           t.repo as repo,
           (select m.sender from messages m
             where m.thread_id = t.id and m.kind = 'task'
             order by m.created_at, m.id limit 1) as mailbox,
           (select m.meta->>'model' from messages m
             where m.thread_id = t.id and m.kind = 'result'
             order by m.created_at desc, m.id desc limit 1) as model,
           exists (select 1 from messages m
                    where m.thread_id = t.id and m.kind = 'task') as has_task,
           exists (select 1 from messages m
                    where m.thread_id = t.id and m.state = 'failed') as failed,
           (select count(*) from messages m
             where m.thread_id = t.id and m.kind = 'result') as results,
           coalesce((select case when (m.meta->>'state_note') like 'accept%' then 1 else 0 end
                       from messages m
                      where m.thread_id = t.id and m.kind = 'result'
                      order by m.created_at, m.id limit 1), 0) as accepted_first,
           (select count(*) from messages m
             where m.thread_id = t.id and m.kind = 'verdict'
               and m.meta->>'decision' = 'rework') as rework,
           (select count(*) from messages m
             where m.thread_id = t.id and m.kind = 'result'
               and m.meta->>'timeout' = 'true') as timeouts,
           (select sum(nullif(m.meta->'usage'->'tokens'->>'input', '')::bigint)
              from messages m
             where m.thread_id = t.id and m.kind = 'result') as tokens_input,
           (select sum(nullif(m.meta->'usage'->'tokens'->>'output', '')::bigint)
              from messages m
             where m.thread_id = t.id and m.kind = 'result') as tokens_output
      from threads t
)
"""

_STATS_KEYS = {"mailbox": "mailbox", "repo": "repo", "model": "model"}


def stats(conn: psycopg.Connection, group: str) -> list[dict[str, Any]]:
    """Acceptance and throughput per mailbox, repo or model. Empty cells are null."""
    if group not in _STATS_KEYS:
        raise ValueError(f"unknown stats group: {group}")
    key = f"facts.{_STATS_KEYS[group]}"

    counters = conn.execute(
        _STATS_FACTS
        + f"""
        select {key} as key,
               sum(case when has_task then 1 else 0 end)::bigint as tickets,
               sum(results)::bigint as results,
               sum(accepted_first)::bigint as accepted_first_review,
               sum(rework)::bigint as rework_verdicts,
               sum(timeouts)::bigint as timeouts,
               sum(case when failed then 1 else 0 end)::bigint as failed,
               sum(tokens_input)::bigint as tokens_input,
               sum(tokens_output)::bigint as tokens_output
          from facts
         where {key} is not null
         group by 1
         order by 1
        """
    ).fetchall()

    queue = conn.execute(
        _STATS_FACTS
        + f"""
        select {key} as key,
               percentile_cont(0.5) within group (
                   order by extract(epoch from (leased.at - m.created_at))
               ) as median_queue_seconds
          from messages m
          join lateral (
                select e.at from events e
                 where e.message_id = m.id and e.kind = 'leased'
                 order by e.id
                 limit 1
          ) leased on true
          join facts on facts.thread_id = m.thread_id
         where m.kind in ('task', 'fix') and {key} is not null
         group by 1
         order by 1
        """
    ).fetchall()

    run = conn.execute(
        _STATS_FACTS
        + f"""
        select {key} as key,
               percentile_cont(0.5) within group (
                   order by extract(epoch from (r.created_at - leased.at))
               ) as median_run_seconds
          from messages r
          join lateral (
                select e.at from events e
                 where e.message_id = r.parent_id and e.kind = 'leased'
                   and e.at <= r.created_at
                 order by e.id desc
                 limit 1
          ) leased on true
          join facts on facts.thread_id = r.thread_id
         where r.kind = 'result' and {key} is not null
         group by 1
         order by 1
        """
    ).fetchall()

    rows: dict[str, dict[str, Any]] = {
        row["key"]: {
            **row,
            "median_queue_seconds": None,
            "median_run_seconds": None,
        }
        for row in counters
    }
    for row in queue:
        rows.setdefault(row["key"], {"key": row["key"]})["median_queue_seconds"] = row[
            "median_queue_seconds"
        ]
    for row in run:
        rows.setdefault(row["key"], {"key": row["key"]})["median_run_seconds"] = row[
            "median_run_seconds"
        ]
    for row in rows.values():
        for field in ("median_queue_seconds", "median_run_seconds"):
            value = row.get(field)
            row[field] = round(float(value), 1) if value is not None else None
    return [rows[key_value] for key_value in sorted(rows)]


def _first_line(body: Any) -> str:
    text = str(body or "").strip()
    return text.splitlines()[0][:160] if text else ""
