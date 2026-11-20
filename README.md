# agentbus

Локальная очередь сообщений между двумя агентами: Claude в виртуалке (cage) и
OpenCode на хосте. Один процесс владеет состоянием — Postgres. Оба агента только
говорят с ним по HTTP.

Задача этого инструмента — цикл «план → исполнение → ревью»: Claude ставит задачу,
OpenCode выполняет её в отдельном git-worktree и возвращает отчёт с диффом, Claude
читает и выдаёт вердикт. Никакого общего диска и файловых блокировок.

```
┌─ VM (cage) ──────────┐        ┌─ хост ─────────────────────────────┐
│ Claude + ab          │  HTTP  │ API (FastAPI)  →  Postgres         │
│  send / wait / reply │◄──────►│ worker (Python) → opencode run     │
└──────────────────────┘ bridge └────────────────────────────────────┘
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
make worker   # исполнитель OpenCode
```

Проверка, что петля живая, без вызова модели:

```bash
make smoke
```

## Как пользоваться

Claude (в VM) ставит задачу:

```bash
export AGENTBUS_SELF=cage-claude
ab send --to local-opencode \
        --repo /home/ruslan/personal/real-inflation \
        --meta '{"acceptance":["make check зелёный"],"checks":true}' \
        "Реализуй T1 по docs/specs/T1.md"
```

Возвращается `{"id": "m_...", "thread_id": "t_..."}`. Дальше:

```bash
ab wait m_... --text        # ждёт ответ исполнителя, печатает отчёт
ab thread t_... --text      # вся переписка
ab file m_... src/agentbus/api.py
ab artifact m_... patch.diff
ab reply m_... --kind verdict --body "правь X и Y"
ab lease cage-claude --wait 60
```

Исполнитель — обычный цикл: `lease → opencode run → reply`. Один тред = один
worktree = одна ветка `agentbus/<thread_id>` = одна сессия OpenCode, поэтому
правки по ревью ложатся туда же, где была задача.

## Cage без сети: файловый мост

Если виртуалке нельзя давать сеть вообще, она работает через общий каталог. Хост-часть
моста забирает файлы и раскладывает ответы, HTTP остаётся только на хосте.

```
<share>/cage-claude/          # AGENTBUS_SHARE — одинаковый путь внутри VM
    outbox/                   # VM кладёт <любое>.json  -> сообщение в очереди
    sent/  failed/            # мост переносит сюда (в failed рядом лежит .error)
    inbox/<message_id>/       # мост кладёт ответ
        message.json
        patch.diff, checks.txt, run.log
    read/                     # VM переносит сюда обработанное (необязательно)
```

`AGENTBUS_SHARE` — это **папка агента**, а не корень шара; одно и то же значение
на хосте и в гостевой системе, поэтому путаницы с путями нет.

В cage нужен только `bin/cb` и `python3`; jq, curl и токен там не нужны — сетевого
доступа у моста нет по построению:

```bash
export AGENTBUS_SHARE=/mnt/cage-bridge/cage-claude

cb send --to local-opencode --repo /home/ruslan/personal/real-inflation \
        --meta '{"acceptance":["make check зелёный"],"checks":true}' \
        "Реализуй T1 по docs/specs/T1.md"

cb inbox                  # что пришло
cb show m_... --text      # сообщение и diff_stat
cb patch m_...            # сам diff
cb reply m_... --kind verdict "правь X"
cb read m_...             # убрать из inbox
```

Мост запускается на хосте: `make bridge` (или `deploy/agentbus-bridge.service`).
Скорость — один цикл в `AGENTBUS_BRIDGE_INTERVAL` секунд (по умолчанию 2).

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


`meta` ответа: `worktree`, `branch`, `files_changed`, `diff_stat`, `base_commit`,
`exit_code`, `duration_seconds`, `usage`, `checks`, `session_id`, `artifacts`.

Артефакты лежат в `~/.local/state/agentbus/artifacts/<message_id>/`: `run.log`
(сырой JSONL OpenCode), `patch.diff`, `checks.txt`. Читаются через `ab artifact`.

## Модель данных

`threads` — репозиторий, базовая ветка, id сессии OpenCode.
`messages` — сообщение: отправитель, получатель, тип, тело, `meta`, состояние,
`parent_id`, `attempt`, `lease_until`.

Типы: `task`, `fix`, `question`, `note` (работа), `result`, `verdict`, `ack` (ответы).
Состояния: `queued → leased → done | failed | cancelled`.

Выдача — `update ... where id = (select ... for update skip locked)`, поэтому
несколько воркеров не возьмут одно сообщение. Протухший лиз возвращается в
очередь при следующем `lease`, `attempt` растёт.

## Сеть и безопасность

- API по умолчанию слушает `127.0.0.1`. Для VM: `AGENTBUS_BIND_HOST=<bridge-ip>`,
  например адрес `virbr0`. Наружу не выставлять.
- Каждый запрос требует `Authorization: Bearer $AGENTBUS_TOKEN`.
- Воркер отказывается работать с репозиторием вне `AGENTBUS_REPOS`.
- `/file` читает только внутри worktree сообщения; выход за пределы — 400.
- Воркер никогда не пушит и не мержит. Ревью — Claude, мерж — человек.

## Границы v1

Сознательно не сделано: авторизация по агентам, ретраи по классам ошибок,
автомерж, метрики, веб-интерфейс, Kafka. Транспорт — Postgres; если появится
второй потребитель того же потока или несколько машин, к нему добавляется
журнал событий, а таблица состояний остаётся.
