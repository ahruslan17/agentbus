# T8a — хвосты ревью: release lease, троттлинг лога bridge, тест невалидных ящиков

Контекст: `docs/plan-parallel-and-dashboard.md`, строка T8 (хвосты ревью T1 и T4).

## 1. Вернуть сообщение в очередь (`store.py`, `api.py`, `worker.py`)

Сейчас, если lease вернул сообщение уже во время остановки worker, слот его не
исполняет, и оно висит `leased` до истечения TTL (до 15 минут).

- `store.release(conn, *, message_id, owner)` — если сообщение `leased` и
  `lease_owner = owner`: `state='queued'`, `lease_owner=null`, `lease_until=null`,
  `attempt = attempt - 1` (попытки не было), событие `released` (actor = owner).
  Возвращает строку или `None`, если условие не выполнено.
- `POST /v1/messages/{id}/release` с телом `{"owner": "..."}` (валидация owner как в
  T3) → 200 со строкой или 409, если сообщение не `leased` этим owner.
- Слот (`Slot.run`): ветка «lease вернулся во время остановки» вызывает release вместо
  того, чтобы молча продолжить; ошибка release — лог, не падение.

## 2. Троттлинг лога сломанного ящика (`bridge.py`)

`MultiBridge.run_once` логирует исключение ящика на каждом тике (раз в 2 с). Нужно:
первая ошибка — сразу; та же ошибка (тот же `repr`) того же ящика — не чаще раза в 5
минут с припиской `(repeated N times)`; восстановление — одна строка
`mailbox <name>: recovered`.

## 3. Тест невалидных ящиков (`tests/test_bridge.py`)

В `test_invalid_mailbox_names_are_ignored` добавить проверки: внутри невалидных папок не
создаются `sent/`, `failed/`, `inbox/`; файл в их `outbox/` остаётся на месте.

## Тесты

- `tests/test_queue.py` или `tests/test_lease.py`: release своим owner → `queued`,
  `attempt` вернулся, событие `released`; чужим owner → 409; не leased → 409.
- `tests/test_worker.py`: stop выставлен до возврата lease (подменить `Client.lease`,
  чтобы он ставил stop и возвращал сообщение) → сообщение снова `queued` сразу, а не
  через TTL.
- `tests/test_bridge.py`: троттлинг — 3 тика с одной ошибкой → одна строка лога; после
  починки ящика — строка `recovered` (время подменить через параметр/monkeypatch, без
  реального ожидания 5 минут).

## Файлы

`src/agentbus/store.py`, `src/agentbus/api.py`, `src/agentbus/worker.py`,
`src/agentbus/bridge.py`, `tests/test_lease.py`, `tests/test_worker.py`,
`tests/test_bridge.py`.

## Критерии приёмки

- `make check` зелёный, новые тесты есть и проходят, существующие не ослаблены.
- Изменены только файлы из списка. Не коммить.
