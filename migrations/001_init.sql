-- agentbus schema, v1.
-- One table for the conversation, one for threads (repo + opencode session continuity).

create table if not exists threads (
    id          text primary key,
    repo        text,
    base_ref    text,
    session_id  text,
    created_by  text not null,
    created_at  timestamptz not null default now()
);

create table if not exists messages (
    id               text primary key,
    thread_id        text references threads (id) on delete set null,
    sender           text not null,
    recipient        text not null,
    kind             text not null,
    body             text not null,
    meta             jsonb not null default '{}'::jsonb,
    state            text not null default 'queued',
    parent_id        text references messages (id) on delete set null,
    attempt          integer not null default 0,
    lease_owner      text,
    lease_until      timestamptz,
    idempotency_key  text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    constraint messages_kind_valid check (
        kind in ('task', 'fix', 'question', 'note', 'result', 'verdict', 'ack')
    ),
    constraint messages_state_valid check (
        state in ('queued', 'leased', 'done', 'failed', 'cancelled')
    )
);

create unique index if not exists messages_idempotency_key_uniq
    on messages (idempotency_key)
    where idempotency_key is not null;

create index if not exists messages_inbox_idx
    on messages (recipient, state, created_at);

create index if not exists messages_thread_idx
    on messages (thread_id, created_at);

create index if not exists messages_parent_idx
    on messages (parent_id);
