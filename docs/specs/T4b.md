# T4b — worker: коммит результата, полный патч треда, живой лог, учёт токенов

Контекст: `docs/plan-parallel-and-dashboard.md`, разделы 2–3. Опирается на T4.

Проблемы сейчас:
- worker делает только `git add -A` и берёт `git diff --cached`: ветка
  `agentbus/<thread>` пустая, а если исполнитель сам закоммитил — `patch.diff` пустой
  (так было в T3b);
- `run.log` пишется буферизованно и сбрасывается в конце — смотреть вживую нельзя;
- `usage` берётся из последнего `step_finish`, а не суммой — токены занижены;
- в meta результата нет модели и harness — нельзя сравнивать.

## Что сделать (`src/agentbus/worker.py`)

### 1. База треда

При создании worktree (`prepare_worktree`, новая ветка) запомнить `base_commit` —
`git rev-parse HEAD` сразу после `worktree add` — в
`<state_dir>/threads/<thread_id>.json` (`{"base_commit": ..., "branch": ...}`).
Для уже существующего worktree читать оттуда; если файла нет (треды до этой версии) —
`git merge-base HEAD <base_ref или master/main, что есть>`.

### 2. Коммит попытки

После `run_opencode` (не по таймауту):
- `git add -A`; если есть что коммитить — коммит с сообщением
  `agentbus: <meta.ticket или thread_id> attempt <attempt>` и автором
  `AGENTBUS_COMMIT_AUTHOR` (по умолчанию `agentbus executor <agentbus@localhost>`),
  через `-c user.name/-c user.email`, без хуков (`--no-verify`);
- если исполнитель сам сделал коммиты — их не трогать.

### 3. Патчи

- `patch.diff` — **накопленный** дифф треда `base_commit..HEAD` (это то, что ревьюер
  мержит);
- `attempt.diff` — дифф этой попытки: `HEAD` до запуска opencode → `HEAD` после
  коммита (запомнить `HEAD` перед запуском);
- meta: `base_commit`, `head_commit`, `diff_stat`, `files_changed`, `patch_bytes` —
  по накопленному диффу; `attempt_files_changed` — по попытке;
  `artifacts` += `attempt.diff`.

### 4. Живой лог

В `run_opencode` писать каждую строку с `flush()` сразу (поток `pump`).

### 5. Учёт

- `usage`: сумма по **всем** `step_finish`: `tokens.input`, `tokens.output`,
  `tokens.reasoning`, `tokens.cache.read`, `tokens.cache.write`, `cost`; плюс `steps`
  (число `step_finish`) и `tool_calls` (число событий `tool_use`).
- meta результата: `model` (`settings.model`), `harness` (`"opencode"`).

### 6. Промпт

В правилах `build_prompt` для задач с репозиторием добавить строку: «Не коммить —
коммит сделает worker». (Исполнитель всё равно может закоммитить — п. 2 и 3 это
переживают.)

## Тесты — дополнить `tests/test_worker.py`

1. Задача → в ветке треда ровно один новый коммит с автором по умолчанию,
   `patch.diff` непустой, `files_changed` верный.
2. fix в том же треде → второй коммит; `patch.diff` содержит изменения обеих
   попыток, `attempt.diff` — только второй.
3. Фейковый opencode сам коммитит → `patch.diff` всё равно содержит его изменения.
4. Пустая попытка (ничего не изменил) → коммита нет, `patch_bytes` = 0.
5. `usage` суммирует три `step_finish`, `tool_calls` считает `tool_use`; в meta есть
   `model` и `harness`.
6. Живой лог: пока фейковый opencode спит после первой строки, `run.log` уже содержит
   её (проверка из другого потока).
7. Тред без файла базы (как до этой версии) → база через merge-base, патч корректный.

## Чего НЕ делать

- Не трогать `store.py`, `api.py`, `bridge.py`, `bin/cb`, миграции.
- Не менять формат текста отчёта и разбор `text`/`error`.
- Не пушить и не мержить ничего — коммиты только в ветке треда.

## Файлы

`src/agentbus/worker.py`, `src/agentbus/config.py` (если нужна настройка автора),
`tests/test_worker.py`, `.env.example`.

## Критерии приёмки

- `make check` зелёный, 7 новых тестов есть и проходят, тесты T4 не ослаблены.
- Изменены только файлы из списка.
- **Не коммить** в этой задаче (worker текущей версии соберёт патч из рабочего дерева).
- В отчёте: хвост `make check`, пример meta результата из теста 2.
