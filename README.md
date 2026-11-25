# agentbus

Локальная очередь сообщений между агентами: Claude в виртуалке (cage) и
OpenCode на хосте. Один процесс владеет состоянием — Postgres. Агенты только
говорят с ним по HTTP.

Задача этого инструмента — цикл «план → исполнение → ревью»: Claude ставит задачу,
OpenCode выполняет её в отдельном git-worktree и возвращает отчёт с диффом, Claude
читает и выдаёт вердикт. Никакого общего диска и файловых блокировок.

Ящиков и исполнителей может быть много. Ящик — папка в общем каталоге: своя сессия
Claude в своём продукте, без настройки. Исполнителей — пул слотов на хосте: каждый
слот берёт свой тред, поэтому независимые тикеты идут параллельно. Что происходит
сейчас и что было раньше, видно в локальном дашборде; полный журнал событий
(`events`) и реестр слотов (`workers`) лежат в Postgres.

```
┌─ VM (cage) ──────────────┐        ┌─ хост ─────────────────────────────────┐
│ Claude + cb, N ящиков    │  файлы │ API (FastAPI)  →  Postgres (журнал)     │
│  send / wait / reply     │◄──────►│ bridge         →  .agentbus/<ящик>/    │
└──────────────────────────┘        │ worker: N слотов → opencode run        │
                                    │ дашборд: 127.0.0.1:8787/ui/            │
                                    └────────────────────────────────────────┘
```

## Установка

```bash
uv sync
cp .env.example ~/.config/agentbus/env
# вписать AGENTBUS_TOKEN: openssl rand -hex 24
set -a; . ~/.config/agentbus/env; set +a

docker compose up -d db
uv run python -m agentbus.migrate
```

Запуск (два терминала или systemd-юниты из `deploy/`):

```bash
make api      # API на 127.0.0.1:8787
make worker   # пул исполнителей: AGENTBUS_WORKER_SLOTS слотов
```

Юниты пользователя ставятся симлинками:

```bash
ln -sf ~/personal/agentbus/deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentbus-api agentbus-bridge agentbus-worker
```

Проверки, которые не зовут модель:

```bash
make smoke        # одна задача: lease → reply → wait
make smoke-pool   # два ящика и два параллельных lease
```

## Как пользоваться

Claude (в VM) ставит задачу:

```bash
export AGENTBUS_SELF=cage-claude
ab send --to local-opencode \
        --repo /home/ruslan/personal/real-inflation \
        --meta '{"ticket":"T1","acceptance":["make check зелёный"],"scope_files":["src/"],"checks":true}' \
        "Реализуй T1 по docs/specs/T1.md"
```

Возвращается `{"id": "m_...", "thread_id": "t_..."}`. Дальше:

```bash
ab wait m_... --text        # ждёт ответ исполнителя, печатает отчёт
ab thread t_... --text      # вся переписка
ab file m_... src/agentbus/api.py
ab artifact m_... patch.diff
ab reply m_... --kind verdict --body "правь X и Y"
ab lease cage-claude --wait 60 --owner 'local-opencode#0'
```

Независимые тикеты в разных тредах можно отправлять сразу — пул слотов разберёт их
параллельно; зависимый отправляй после merge предыдущего. Один тред = один
worktree = одна ветка `agentbus/<thread_id>` = одна сессия OpenCode, поэтому правки
по ревью ложатся туда же, где была задача, и две задачи одного треда не пересекутся.

Исполнитель — обычный цикл: `lease → opencode run → reply`. Ветку треда worker
коммитит сам; ревью и merge — за человеком, `git merge agentbus/<thread_id>`.

## Cage без сети: файловый мост

Если виртуалке нельзя давать сеть вообще, она работает через общий каталог. Хост-часть
моста забирает файлы и раскладывает ответы, HTTP остаётся только на хосте.

Один процесс моста обслуживает **все** ящики сразу: ящик — это папка внутри
`.agentbus/`, имя папки — имя агента. Отдельный bridge на каждый ящик не нужен, и новая
папка подхватывается на следующем тике без рестарта.

```
<share_root>/<agent>/         # корень: AGENTBUS_SHARE_ROOT или родитель AGENTBUS_SHARE
    outbox/                   # VM кладёт <любое>.json  -> сообщение в очереди
    sent/  failed/            # мост переносит сюда (в failed рядом лежит .error)
        <stem>.receipt.json   # квитанция: id, thread_id, kind, recipient
    inbox/<message_id>/       # мост кладёт ответ
        message.json
        patch.diff, checks.txt, run.log
    read/                     # VM переносит сюда обработанное (необязательно)
    status.json               # состояние очереди глазами этого ящика (см. ниже)
```

`AGENTBUS_SHARE` — это **папка агента** (например `.agentbus/cage-claude`), а
`AGENTBUS_SHARE_ROOT` — корень со всеми ящиками. Если задан только `AGENTBUS_SHARE`,
корнем считается его родитель, поэтому старые env-файлы продолжают работать. Ящики с
именами не по шаблону `^[a-z0-9][a-z0-9-]{1,40}$` (включая папки с точкой) мост
пропускает. `AGENTBUS_BRIDGE_AGENT` больше не используется: агент всегда равен имени
папки.

`cb` сам находит свой ящик, по приоритету:

1. `AGENTBUS_SHARE` — полный путь к папке ящика;
2. `AGENTBUS_MAILBOX=<имя>` — `<корень>/<имя>`;
3. внутри git-репозитория — `claude-<имя папки репозитория>`: lower-case, всё кроме
   `[a-z0-9-]` заменяется на `-`, повторы схлопываются, до 33 символов
   (`My_Repo.v2` → `claude-my-repo-v2`);
4. иначе `cage-claude` — старые настройки продолжают работать.

Две сессии в разных продуктах разводятся без настройки. `cb env` печатает выбранные
`mailbox` и `source`, `cb init [имя]` создаёт ящик (`outbox/ inbox/ sent/ read/`) и
печатает путь. `send`, `reply` и `init` создают папку сами, остальные команды на
отсутствующий ящик ругаются.

В cage нужен только `bin/cb` и `python3`; jq, curl и токен там не нужны — сетевого
доступа у моста нет по построению:

```bash
export AGENTBUS_SHARE=/mnt/cage-bridge/cage-claude

cb init
cb send --to local-opencode --repo /home/ruslan/personal/real-inflation \
        --meta '{"acceptance":["make check зелёный"],"checks":true,"ticket":"T2"}' \
        "Реализуй T2 по docs/specs/T2.md"      # в выводе id и thread_id

cb inbox                  # что пришло
cb show m_... --text      # сообщение, тикет и diff_stat
cb patch m_...            # сам diff
cb wait m_... [m_...] [--all] [--timeout 600]   # ответы по parent_id
cb status [--json] [--log] [m_...]              # очередь, слоты, мои сообщения
cb reply m_... --kind verdict "правь X"   # meta.decision = "rework"
cb accept m_... --note "принято"          # decision.json + перенос в read/
cb read m_...             # убрать из inbox
```

Мост запускается на хосте: `make bridge` (или `deploy/agentbus-bridge.service`). Корень
берётся из `AGENTBUS_SHARE_ROOT`, иначе из родителя `AGENTBUS_SHARE`. Строка старта
печатает корень и найденные ящики. Скорость — один цикл в `AGENTBUS_BRIDGE_INTERVAL`
секунд (по умолчанию 2).

После успешной отправки рядом с файлом появляется квитанция
`sent/<stem>.receipt.json` с `id` и `thread_id`: мастер узнаёт id сообщения, не заглядывая
в API. `cb send` и `cb reply` ждут её (`--wait-id`, по умолчанию 15 секунд, `0` — не
ждать) и печатают `id` и `thread_id` в том же JSON. Если квитанции нет — печатается всё,
что известно, плюс предупреждение в stderr, код выхода 0; если файл уехал в `failed/` —
печатается текст `.error` и код выхода 1. Отправитель всегда принудительно равен имени
ящика — подписаться чужим ящиком нельзя.

`cb wait <id> ...` ждёт в `inbox/` ответы с `parent_id` из переданных id: по умолчанию
возвращается после первого, с `--all` — после всех, `--timeout` (по умолчанию 3600
секунд) завершает с кодом 2. Строка ответа содержит тикет, `checks=ok|fail|-` и
`timeout=true|false`.

`meta.ticket` отправленного сообщения виден в `cb inbox` и `cb show --text` для ответов
на него: связь идёт по цепочке `parent_id` → квитанция в `sent/` → исходный файл.
Вердикт, отправленный через `cb reply --kind verdict`, по умолчанию получает
`meta.decision = "rework"` и наследует `ticket` от сообщения, на которое отвечает.
`cb accept <id>` пишет `read/<id>/decision.json` и переносит папку в `read/`; мост
закрывает сообщение заметкой `accept` или `accept: <note>` вместо `read in the VM`.

Раз в `AGENTBUS_STATUS_INTERVAL` секунд (по умолчанию 5, `0` — выключить) мост просит у
API состояние и атомарно переписывает `<ящик>/status.json`: слоты пула, очереди по
ящикам, сообщения этого ящика (`queued`, `leased`, последние 20 завершённых за сутки) и
последние 30 событий его тредов. У занятого сообщения моста есть `log_tail` — последние
15 строк `run.log` без сырых данных, чтобы видеть, что исполнитель делает сейчас. Токена
и тел сообщений в файле нет; про чужой слот известно только имя ящика (`sender`). Если
API недоступен, мост пишет `{"generated_at", "error"}` и продолжает возить outbox/inbox.

`cb status` читает этот файл: первой строкой — его возраст (`status: 3s ago`), затем
слоты (`local-opencode#0 busy m_… [T11] 4m`), затем свои сообщения
(`m_… [T11] leased by local-opencode#1, attempt 1`; `queued, ~2nd in line` — оценка по
очереди, lease может её обогнать; `done → result m_…`). `--json` печатает файл как есть,
`--log` — `log_tail` занятых, `id ...` фильтрует сообщения. Файл старше
`max(30, 6*interval)` секунд или отсутствует — `status is stale` и код 2. `cb wait`, если
файл есть, печатает в stderr переходы состояния ожидаемых сообщений
(`m_… queued → leased by …`). `cb env` показывает возраст файла.

Доставка идемпотентна: имя файла в `outbox` становится ключом идемпотентности, а
признак «уже доставлено» хранится на хосте, поэтому рестарт моста ничего не дублирует.
Битые файлы и отвергнутые сообщения уезжают в `failed/` с текстом ошибки и не
зацикливаются.

Шар живёт внутри каталога, который Cage уже отдаёт в VM — ничего в конфигурации
виртуалки менять не нужно. Cage монтирует открытые проектные каталоги по их же
хостовым путям (reverse-sshfs, writable), поэтому путь один и тот же с обеих сторон:

```
хост:  /home/ruslan/personal/.agentbus/cage-claude
гость: /home/ruslan/personal/.agentbus/cage-claude
```

`cb` вообще не требует переменных: он вычисляет шар от собственного расположения
(`<...>/agentbus/bin/cb` → `<...>/.agentbus/cage-claude`), а `AGENTBUS_SHARE` нужен
только если шар лежит в другом месте. Проверено изнутри VM:

```bash
cage run /home/ruslan/personal/agentbus/bin/cb inbox
```

Оговорка: Cage отдаёт только открытые проектные каталоги. Пока открыт другой проект,
`~/personal` в VM не виден — тогда либо открыть его тоже, либо добавить строку в
`~/.agent-vm/volumes` (родной механизм Cage: `source[:destination][:ro|rw[:проект]]`),
например `/home/ruslan/personal/.agentbus:rw`.

Ещё одно следствие: токен и `AGENTBUS_URL` в cage не попадают вообще. Скомпрометировать
мост можно только записью файлов в шар, а он и так доступен только этой VM.

## Дашборд

На хосте, только чтение: `http://127.0.0.1:8787/ui/login?token=<AGENTBUS_TOKEN>`.
Вход ставит HttpOnly cookie, дальше страница ходит с ней. Проверка `Host` не даёт
браузеру обратиться к API по чужому имени.

- «Сейчас» — карточка на каждый слот: свободен или занят, ящик, тикет, репозиторий,
  сколько идёт, последний tool call, токены; ниже — очереди по ящикам.
- «Ящики» — сколько тикетов отправлено, в работе, ждёт ревью и принято.
- «Тред» — таймлайн `task → result → verdict → fix`, дифф по файлам, `checks.txt`,
  лента `run.log` (у занятого треда — в реальном времени), длительность и токены.
- «История» — все треды с фильтрами по ящику, репозиторию, состоянию и тексту.
- «Статистика» — по ящику, репозиторию и модели: медиана очереди и исполнения, токены,
  доля принятых с первого ревью, число доработок, таймауты и сбои.

Данные обновляются через SSE (`GET /v1/stream`) с откатом на опрос; все `/v1/*`-эндпоинты
только читают. Bearer-токен для `ab`, `cb` и worker не меняется.

## Скилл для агента в cage

`skills/agentbus/SKILL.md` — инструкция для Claude в изоляции: как ставить задачу,
что класть в `meta`, как читать патч и как выдавать вердикт. Ставится симлинком,
поэтому обновления из репозитория подхватываются сами:

```bash
# внутри VM
mkdir -p ~/.claude/skills
ln -sfn /home/ruslan/personal/agentbus/skills/agentbus ~/.claude/skills/agentbus

# на хосте, если тот же скилл нужен OpenCode
mkdir -p ~/.config/opencode/skills
ln -sfn ~/personal/agentbus/skills/agentbus ~/.config/opencode/skills/agentbus
```

Проверка, что скилл виден:

```bash
head -3 ~/.claude/skills/agentbus/SKILL.md
```

## Что в отчёте исполнителя

`meta` ответа: `worktree`, `branch`, `repo`, `files_changed`, `diff_stat`, `base_commit`,
`head_commit`, `attempt_files_changed`, `exit_code`, `duration_seconds`, `timeout`,
`usage`, `checks`, `session_id`, `model`, `harness`, `artifacts`.

Артефакты лежат в `~/.local/state/agentbus/artifacts/<message_id>/`: `run.log`
(сырой JSONL OpenCode), `patch.diff` (накопленный дифф треда от базового коммита),
`attempt.diff` (только последняя попытка), `checks.txt`. Читаются через `ab artifact`.
После ответа каталог переезжает под id результата.

## Эксплуатация

```bash
# юниты пользователя
ln -sf ~/personal/agentbus/deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentbus-api agentbus-bridge agentbus-worker

# миграции Postgres (идемпотентны)
uv run python -m agentbus.migrate

# перезапуск worker: SIGTERM не берёт новые задачи, дожидается текущих
systemctl --user restart --no-block agentbus-worker
```

`KillMode=mixed` в `agentbus-worker.service` шлёт SIGTERM только самому worker:
запущенные `opencode run` успевают закончить в пределах `TimeoutStopSec`
(`AGENTBUS_STOP_GRACE` плюс запас). Поэтому рестарт не рвёт текущие прогоны.

Переменные (полный список — `.env.example`):

| Переменная | Что делает |
|---|---|
| `AGENTBUS_WORKER_SLOTS` | число слотов пула; 1 по умолчанию, рекомендовано 3–4 |
| `AGENTBUS_PER_SENDER_LEASES` | потолок одновременных lease одного отправителя (0 — без) |
| `AGENTBUS_ALLOWED_HOSTS` | допустимые `Host` дашборда через запятую |
| `AGENTBUS_COMMIT_AUTHOR` | автор коммитов worker, `Name <email>` |
| `AGENTBUS_STOP_GRACE` | сколько ждать текущие задачи при SIGTERM (по умолчанию `AGENTBUS_TASK_TIMEOUT`) |
| `AGENTBUS_SHARE_ROOT` | корень ящиков для bridge; иначе родитель `AGENTBUS_SHARE` |
| `AGENTBUS_STATUS_INTERVAL` | период `status.json` в секундах (5 по умолчанию, 0 — выключить) |

### Уборка

Растут worktree, артефакты и журнал. Закрытый тред — тот, в котором нет сообщений
`queued`/`leased`. `make prune` оставляет последние `KEEP` закрытых тредов (по
последнему `updated_at`, по умолчанию 50), остальным удаляет
`~/.local/state/agentbus/worktrees/<thread>` через `git worktree remove --force`,
`artifacts/<message_id>` и `threads/<thread>.json`. Строки в БД не трогаются.

Worktree с несохранённой работой пропускается вместе с артефактами и base-файлом:
грязный `git status` (`uncommitted changes: N files`, игнорируемое вроде `.venv/` не
считается) или detached HEAD с коммитами, которых нет в `master`/`main`. Ветка треда
коммиты сохраняет, поэтому обычный HEAD проверять не нужно.

```bash
make prune PRUNE_ARGS=--dry-run   # только план, в том числе пропущенные грязные
make prune KEEP=20                # удалить лишнее
make prune PRUNE_ARGS=--delete-merged-branches  # плюс ветки, влитые в master/main
make prune PRUNE_ARGS=--force-dirty  # удалить и worktree с несохранённой работой
```

Ветка удаляется только через `git branch -d`, то есть лишь если она целиком влита в
`master`/`main`; репозиторий вне `AGENTBUS_REPOS` пропускается с предупреждением.
Из VM `git worktree prune` не запускать: worktree созданы на хосте и в VM выглядят
`prunable`.

## Модель данных

`threads` — репозиторий, базовая ветка, id сессии OpenCode.
`messages` — сообщение: отправитель, получатель, тип, тело, `meta`, состояние,
`parent_id`, `attempt`, `lease_owner`, `lease_until`.
`events` — append-only журнал: создание, lease, ответ, смена состояния, возврат
протухшего lease. По нему считаются время в очереди и исполнения, повторы и статистика.
`workers` — реестр слотов: `<agent>#<slot>`, хост, pid, модель, harness, состояние,
текущее сообщение, heartbeat.

Типы: `task`, `fix`, `question`, `note` (работа), `result`, `verdict`, `ack` (ответы).
Состояния: `queued → leased → done | failed | cancelled`.

Выдача идёт под коротким advisory-lock: один тред — одно исполнение (в треде не может
быть два `leased`), сначала отправитель с меньшим числом текущих lease, затем самые
старые сообщения. `AGENTBUS_PER_SENDER_LEASES` ограничивает число одновременных lease
одной сессии. Протухший lease возвращается в очередь при следующей выдаче, `attempt`
растёт.

## Сеть и безопасность

- API по умолчанию слушает `127.0.0.1`. Для VM: `AGENTBUS_BIND_HOST=<bridge-ip>`,
  например адрес `virbr0`. Наружу не выставлять.
- Каждый запрос требует `Authorization: Bearer $AGENTBUS_TOKEN`.
- Воркер отказывается работать с репозиторием вне `AGENTBUS_REPOS`.
- `/file` читает только внутри worktree сообщения; выход за пределы — 400.
- Воркер никогда не пушит и не мержит. Ревью — Claude, мерж — человек.

## Границы v2

Сознательно не сделано: авторизация по агентам, ретраи по классам ошибок, автомерж,
метрики, отмена тикета из дашборда, Kafka. Транспорт — Postgres; журнал событий живёт
рядом с таблицей состояний и переживает удаление сообщений.
