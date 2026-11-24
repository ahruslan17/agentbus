-- agentbus schema, v2: append-only event log and executor slot registry.
-- No foreign keys on events: the journal outlives message deletion.

create table if not exists events (
    id          bigserial primary key,
    at          timestamptz not null default now(),
    message_id  text,
    thread_id   text,
    actor       text,          -- who caused it: sender / lease owner / 'system'
    kind        text not null, -- created | leased | heartbeat_lost | requeued | done | failed | cancelled
    data        jsonb not null default '{}'::jsonb
);
create index if not exists events_thread_idx  on events (thread_id, id);
create index if not exists events_message_idx on events (message_id, id);

create table if not exists workers (
    id            text primary key,   -- '<agent>#<slot>'
    agent         text not null,
    host          text,
    pid           integer,
    model         text,
    harness       text,
    state         text not null,      -- idle | busy | stopping | stopped
    message_id    text,
    started_at    timestamptz not null default now(),
    heartbeat_at  timestamptz not null default now()
);
