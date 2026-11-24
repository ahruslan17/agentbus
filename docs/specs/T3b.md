# T3b — журнал событий, реестр исполнителей, уведомления

Контекст: `docs/plan-parallel-and-dashboard.md`, раздел 3 («Данные»). Дашборду нужны
история переходов сообщений (таймлайн, время в очереди и в работе, повторы) и состояние
слотов исполнителей. Опирается на T3 (lease с `owner`).

## Что сделать

### 1. Миграция `migrations/002_events_workers.sql` (идемпотентная, как 001)

```sql
create table if not exists events (
    id          bigserial primary key,
    at          timestamptz not null default now(),
    message_id  text,
    thread_id   text,
    actor       text,          -- кто вызвал: sender / lease owner / 'system'
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
```

Без внешних ключей у `events` (журнал переживает удаление сообщений).

### 2. Запись событий (`src/agentbus/store.py`)

Одна внутренняя функция `_event(conn, *, kind, message_id, thread_id, actor, data)`:
вставляет строку и в той же транзакции делает
`select pg_notify('agentbus', <json>)` с `{"event_id", "kind", "message_id",
"thread_id"}`. Вызовы:

| Где | kind | actor | data |
|---|---|---|---|
| `create_message` (новое, не дубль идемпотентности) | `created` | sender | `kind`, `recipient`, `parent_id` |
| `lease_message` (выдано) | `leased` | owner | `attempt` |
| `reap_expired_leases` (каждое) | `requeued` | `system` | `previous_owner` |
| `_close` (реально закрыло) | `done` | `system` | `reason: "replied"` |
| `set_state` | `done` / `failed` / `cancelled` | `system` | `note` |

Heartbeat событий не пишет.

Функции чтения: `thread_events(conn, thread_id)`, `recent_events(conn, after_id,
limit)`.

### 3. Реестр исполнителей

Store: `upsert_worker(conn, *, id, agent, host, pid, model, harness, state,
message_id)` — insert или update всех полей + `heartbeat_at = now()`; `started_at`
обновляется только когда меняется `pid`. `list_workers(conn)`.

API (под тем же токеном):
- `PUT /v1/workers/{worker_id}` — тело `{agent, host, pid, model, harness, state,
  message_id}`; `worker_id` валидируется как `owner` из T3;
  `state ∈ {idle, busy, stopping, stopped}`, иначе 422;
- `GET /v1/workers` — список; к каждому поле `stale: bool` =
  `heartbeat_at < now() - 2 * lease_ttl`;
- `GET /v1/threads/{thread_id}/events` и `GET /v1/events?after=<id>&limit=<n≤500>`.

Worker пока ничего не вызывает — это T4.

### 4. Тесты

- `tests/conftest.py`: `truncate` включает `events, workers`.
- Новый `tests/test_events.py`:
  1. task → lease → reply даёт события треда по порядку: `created(task)`, `leased`,
     `created(result)`, `done`;
  2. дубль по idempotency_key не пишет второе `created`;
  3. просроченный lease → `requeued` с `previous_owner`;
  4. `set_state failed` → `failed` с `note`;
  5. `LISTEN agentbus` на отдельном соединении получает уведомление после
     создания сообщения (коммит), payload парсится и содержит `event_id`;
  6. `PUT /v1/workers/x#1` дважды → одна строка, `heartbeat_at` вырос, `started_at`
     не изменился при том же pid и изменился при новом; невалидный state → 422;
  7. `GET /v1/workers` помечает `stale` для строки со старым `heartbeat_at`;
  8. `GET /v1/events?after=` отдаёт только новые, по возрастанию id.

## Чего НЕ делать

- Не трогать `worker.py`, `bridge.py`, `bin/cb`.
- Не менять поведение и ответы существующих эндпоинтов.
- Не добавлять зависимостей.

## Файлы

`migrations/002_events_workers.sql`, `src/agentbus/store.py`, `src/agentbus/api.py`,
`tests/conftest.py`, `tests/test_events.py`.

## Критерии приёмки

- `make check` зелёный; миграции применяются повторно без ошибок
  (`uv run python -m agentbus.migrate` дважды — в отчёте вывод).
- Все 8 тестов есть и проходят, существующие не ослаблены.
- Изменены только файлы из списка.
