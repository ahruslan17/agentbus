# agentbus

A small task bus for the plan → execute → review loop between a planner agent and
executor agents. The planner — Claude Code in a network-less sandbox VM — writes a
ticket; an executor agent (OpenCode) on the host implements it in its own git worktree
and sends back a report with a diff; the planner reads it and issues a verdict.
Postgres is the only state; agents only talk to the API over HTTP.

There can be many mailboxes and many executors. A mailbox is a folder in a shared
directory: one planner session per project, no configuration. Executors are a pool of
slots on the host: each slot takes its own thread, so independent tickets run in
parallel. The local dashboard shows what is running now and what happened before; the
full event log (`events`) and the slot registry (`workers`) live in Postgres.

```
┌─ VM (sandbox) ───────────┐        ┌─ host ─────────────────────────────────┐
│ planner + cb, N mailboxes│  files │ API (FastAPI)  →  Postgres (event log)  │
│  send / wait / reply     │◄──────►│ bridge         →  .agentbus/<mailbox>/  │
└──────────────────────────┘        │ worker: N slots → opencode run         │
                                    │ dashboard: 127.0.0.1:8787/ui/          │
                                    └────────────────────────────────────────┘
```

## Install

```bash
uv sync
cp .env.example ~/.config/agentbus/env
# set AGENTBUS_TOKEN: openssl rand -hex 24
set -a; . ~/.config/agentbus/env; set +a

docker compose up -d db
uv run python -m agentbus.migrate
```

Start it in two terminals (or use the systemd units from `deploy/`):

```bash
make api      # API on 127.0.0.1:8787
make worker   # executor pool: AGENTBUS_WORKER_SLOTS slots
```

Install the user units as symlinks:

```bash
ln -sf ~/agentbus/deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentbus-api agentbus-bridge agentbus-worker
```

The units hard-code the checkout path in `ExecStart`; edit it if your clone lives
elsewhere.

Checks that do not call a model:

```bash
make smoke        # one task: lease → reply → wait
make smoke-pool   # two mailboxes and two parallel leases
```

## Usage

The planner (inside the VM) writes a ticket:

```bash
export AGENTBUS_SELF=cage-claude
ab send --to local-opencode \
        --repo /path/to/repo \
        --meta '{"ticket":"T2.1","acceptance":["make check passes"],"scope_files":["src/"],"checks":true}' \
        "Implement T2.1 per docs/specs/T2.1.md"
```

It returns `{"id": "m_...", "thread_id": "t_..."}`. From there:

```bash
ab wait m_... --text        # wait for the executor's reply, print the report
ab thread t_... --text      # the whole conversation
ab file m_... src/agentbus/api.py
ab artifact m_... patch.diff
ab reply m_... --kind verdict "Rework X and Y"
ab lease cage-claude --wait 60 --owner 'local-opencode#0'
```

Independent tickets in different threads can be sent at once: the slot pool picks them
up in parallel. Send a dependent ticket after the previous one is merged. One thread =
one worktree = one branch `agentbus/<thread_id>` = one OpenCode session, so review fixes
land where the task was and two tasks in the same thread never overlap.

An executor is a plain loop: `lease → opencode run → reply`. The worker commits the
thread branch itself; review and merge stay with humans — `git merge agentbus/<thread_id>`.

## Sandbox without a network: the file bridge

If the VM must not have any network at all, it works through a shared directory. The
host side of the bridge collects files and drops replies back; HTTP stays on the host.
The bridge does not depend on Cage: any directory visible on both sides works.

One bridge process serves **all** mailboxes at once: a mailbox is a folder inside
`.agentbus/`, and the folder name is the agent name. There is no separate bridge per
mailbox, and a new folder is picked up on the next tick without a restart.

```
<share_root>/<agent>/         # root: AGENTBUS_SHARE_ROOT or the parent of AGENTBUS_SHARE
    outbox/                   # VM drops <anything>.json here -> message queued
    sent/  failed/            # bridge moves files here (failed/ also gets .error)
        <stem>.receipt.json   # receipt: id, thread_id, kind, recipient
    inbox/<message_id>/       # bridge puts the reply here
        message.json
        patch.diff, checks.txt, run.log
    read/                     # VM moves handled messages here (optional)
    status.json               # queue state as seen by this mailbox (see below)
```

`AGENTBUS_SHARE` is the **mailbox folder** (for example `~/.agentbus/cage-claude`), and
`AGENTBUS_SHARE_ROOT` is the root holding every mailbox. If only `AGENTBUS_SHARE` is
set, its parent is used as the root, so older env files keep working. Mailboxes whose
names do not match `^[a-z0-9][a-z0-9-]{1,40}$` (including folders with a dot) are
skipped by the bridge. `AGENTBUS_BRIDGE_AGENT` is no longer used: the agent is always
the folder name.

`cb` finds its mailbox by the first match of:

1. `AGENTBUS_SHARE` — full path to the mailbox folder;
2. `AGENTBUS_MAILBOX=<name>` — `<root>/<name>`;
3. inside a git repository — `claude-<repository folder name>`: lower-case, every
   character outside `[a-z0-9-]` becomes `-`, repeats collapse, up to 33 characters
   (`My_Repo.v2` → `claude-my-repo-v2`);
4. otherwise `cage-claude`, so old setups keep working.

Two sessions in different products get separate mailboxes without configuration.
`cb env` prints the chosen `mailbox` and `source`, and `cb init [name]` creates a
mailbox (`outbox/ inbox/ sent/ read/`) and prints its path. `send`, `reply` and `init`
create the folder themselves; other commands fail on a missing mailbox.

Inside the sandbox you need only `bin/cb` and `python3` — no jq, no curl, no token:
the bridge has no network access by design.

```bash
export AGENTBUS_SHARE=/mnt/cage-bridge/cage-claude

cb init
cb send --to local-opencode --repo /path/to/repo \
        --meta '{"acceptance":["make check passes"],"checks":true,"ticket":"T2.1"}' \
        "Implement T2.1 per docs/specs/T2.1.md"   # prints id and thread_id

cb inbox                  # what has arrived
cb show m_... --text      # message, ticket and diff_stat
cb patch m_...            # the diff itself
cb wait m_... [m_...] [--all] [--timeout 600]   # replies by parent_id
cb status [--json] [--log] [m_...]              # queue, slots, my messages
cb reply m_... --kind verdict "Rework X"   # meta.decision = "rework"
cb accept m_... --note "accepted"         # decision.json + move to read/
cb read m_...             # remove from inbox
```

The bridge runs on the host: `make bridge` (or `deploy/agentbus-bridge.service`). The
root comes from `AGENTBUS_SHARE_ROOT`, otherwise from the parent of `AGENTBUS_SHARE`.
The startup line prints the root and the mailboxes it found. One cycle runs every
`AGENTBUS_BRIDGE_INTERVAL` seconds (2 by default).

After a successful send, a receipt `sent/<stem>.receipt.json` appears next to the file
with the `id` and `thread_id`: the planner learns the message id without touching the
API. `cb send` and `cb reply` wait for it (`--wait-id`, 15 seconds by default, `0` = do
not wait) and print `id` and `thread_id` as the same JSON. Without a receipt they print
everything they know plus a warning on stderr and exit 0; if the file went to `failed/`,
they print the `.error` text and exit 1. The sender is always forced to the mailbox name
— you cannot send as someone else's mailbox.

`cb wait <id> ...` waits in `inbox/` for replies whose `parent_id` is one of the given
ids: by default it returns after the first, with `--all` after all of them, and
`--timeout` (3600 seconds by default) exits with code 2. A reply line shows the ticket,
`checks=ok|fail|-` and `timeout=true|false`.

The `meta.ticket` of a sent message is visible in `cb inbox` and in `cb show --text` for
replies to it: the chain goes `parent_id` → receipt in `sent/` → the original file. A
verdict sent with `cb reply --kind verdict` gets `meta.decision = "rework"` by default
and inherits `ticket` from the message it answers. `cb accept <id>` writes
`read/<id>/decision.json` and moves the folder to `read/`; the bridge closes the message
with an `accept` or `accept: <note>` note instead of `read in the VM`.

Every `AGENTBUS_STATUS_INTERVAL` seconds (5 by default, `0` disables) the bridge asks the
API for state and atomically rewrites `<mailbox>/status.json`: pool slots, queues per
mailbox, this mailbox's messages (`queued`, `leased`, the last 20 finished in the past
day) and the last 30 events of its threads. A leased message has `log_tail` — the last 15
lines of `run.log`, with no raw payload — so you can see what the executor is doing right
now. The file holds no token and no message bodies; for someone else's slot only the
mailbox name (`sender`) is exposed. If the API is down, the bridge writes
`{"generated_at", "error"}` and keeps moving outbox/inbox.

`cb status` reads that file: the first line is its age (`status: 3s ago`), then slots
(`local-opencode#0 busy m_… [T2.1] 4m`), then your messages
(`m_… [T2.1] leased by local-opencode#1, attempt 1`; `queued, ~2nd in line` is an
estimate from the queue, and a lease can jump ahead of it; `done → result m_…`). `--json`
prints the file as is, `--log` prints `log_tail` of leased messages, and `id ...` filters
messages. A file older than `max(30, 6*interval)` seconds, or a missing file, means
`status is stale` and exit code 2. If the file exists, `cb wait` prints state transitions
of the messages it waits for to stderr (`m_… queued → leased by …`). `cb env` shows the
file's age.

Delivery is idempotent: the file name in `outbox` is the idempotency key, and the
"already delivered" mark is kept on the host, so restarting the bridge duplicates
nothing. Broken files and rejected messages go to `failed/` with the error text and are
not retried in a loop.

The share lives inside a directory the sandbox already exposes to the VM, so nothing in
the VM configuration needs to change. Cage, for example, mounts opened project
directories at their own host paths (reverse-sshfs, writable), so the path is the same on
both sides:

```
host:  /path/to/.agentbus/cage-claude
guest: /path/to/.agentbus/cage-claude
```

`cb` needs no variables at all: it derives the share from its own location
(`<...>/agentbus/bin/cb` → `<...>/.agentbus/cage-claude`), and `AGENTBUS_SHARE` is only
needed when the share lives elsewhere. Checked from inside the VM:

```bash
cage run ~/agentbus/bin/cb inbox
```

One caveat: Cage only exposes opened project directories. While another project is open,
`~` is not visible in the VM — open it too, or add a line to `~/.agent-vm/volumes` (the
native Cage mechanism: `source[:destination][:ro|rw[:project]]`), for example
`~/path:rw`.

Another consequence: the token and `AGENTBUS_URL` never reach the sandbox. The only way
to attack the bridge is to write files into the share, and the share is reachable only
from that VM.

## Dashboard

On the host, read-only: `http://127.0.0.1:8787/ui/login?token=<AGENTBUS_TOKEN>`.
Logging in sets an HttpOnly cookie and the page uses it afterwards. The `Host` check
stops a browser from reaching the API under a foreign name.

- **Now** — one card per slot: free or busy, mailbox, ticket, repository, elapsed time,
  last tool call, tokens; queues per mailbox below.
- **Mailboxes** — how many tickets were sent, in progress, waiting for review and
  accepted.
- **Thread** — timeline `task → result → verdict → fix`, diff by file, `checks.txt`, the
  `run.log` feed (live for a leased thread), duration and tokens.
- **History** — all threads with filters by mailbox, repository, state and text.
- **Stats** — by mailbox, repository and model: median queue and run time, tokens, share
  accepted at the first review, rework count, timeouts and failures.

Data updates over SSE (`GET /v1/stream`) with polling as a fallback; every `/v1/*` endpoint
is read-only. The Bearer token for `ab`, `cb` and the worker does not change.

## Skill for the sandboxed agent

`skills/agentbus/SKILL.md` is the instruction for the planner in isolation: how to write
a ticket, what to put in `meta`, how to read the patch and how to issue a verdict. It is
installed as a symlink, so updates from the repository are picked up automatically:

```bash
# inside the VM
mkdir -p ~/.claude/skills
ln -sfn ~/agentbus/skills/agentbus ~/.claude/skills/agentbus

# on the host, if OpenCode needs the same skill
mkdir -p ~/.config/opencode/skills
ln -sfn ~/agentbus/skills/agentbus ~/.config/opencode/skills/agentbus
```

Check that the skill is visible:

```bash
head -3 ~/.claude/skills/agentbus/SKILL.md
```

## What the executor reports

Reply `meta`: `worktree`, `branch`, `repo`, `files_changed`, `diff_stat`, `base_commit`,
`head_commit`, `attempt_files_changed`, `exit_code`, `duration_seconds`, `timeout`,
`usage`, `checks`, `session_id`, `model`, `harness`, `artifacts`.

Artifacts live in `~/.local/state/agentbus/artifacts/<message_id>/`: `run.log` (raw
OpenCode JSONL), `patch.diff` (the thread's cumulative diff from the base commit),
`attempt.diff` (only the latest attempt) and `checks.txt`. Read them with `ab artifact`.
After the reply, the directory moves under the result id.

## Operations

```bash
# user units
ln -sf ~/agentbus/deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agentbus-api agentbus-bridge agentbus-worker

# Postgres migrations (idempotent)
uv run python -m agentbus.migrate

# restart the worker: SIGTERM stops new tasks and waits for the running ones
systemctl --user restart --no-block agentbus-worker
```

`KillMode=mixed` in `agentbus-worker.service` sends SIGTERM to the worker only: running
`opencode run` processes finish within `TimeoutStopSec` (`AGENTBUS_STOP_GRACE` plus a
margin). A restart therefore does not cut running jobs.

Variables (the full list is in `.env.example`):

| Variable | What it does |
|---|---|
| `AGENTBUS_WORKER_SLOTS` | number of pool slots; 1 by default, 3–4 recommended |
| `AGENTBUS_PER_SENDER_LEASES` | cap of concurrent leases per sender (0 = no cap) |
| `AGENTBUS_ALLOWED_HOSTS` | allowed dashboard `Host` values, comma separated |
| `AGENTBUS_REPOS` | directories the worker may run in, comma separated |
| `AGENTBUS_COMMIT_AUTHOR` | author of worker commits, `Name <email>` |
| `AGENTBUS_STOP_GRACE` | how long SIGTERM waits for running tasks (defaults to `AGENTBUS_TASK_TIMEOUT`) |
| `AGENTBUS_SHARE_ROOT` | root of the bridge mailboxes; otherwise the parent of `AGENTBUS_SHARE` |
| `AGENTBUS_STATUS_INTERVAL` | period of `status.json` in seconds (5 by default, 0 disables) |

### Cleanup

Worktrees, artifacts and the event log keep growing. A closed thread is one with no
`queued`/`leased` messages. `make prune` keeps the last `KEEP` closed threads (by
`updated_at`, 50 by default) and removes for the rest
`~/.local/state/agentbus/worktrees/<thread>` via `git worktree remove --force`,
`artifacts/<message_id>` and `threads/<thread>.json`. Database rows are not touched.

A worktree with unsaved work is skipped together with its artifacts and base file: a
dirty `git status` (`uncommitted changes: N files`; ignored files like `.venv/` do not
count) or a detached HEAD with commits missing from `master`/`main`. The thread branch
keeps commits, so a normal HEAD needs no check.

```bash
make prune PRUNE_ARGS=--dry-run   # plan only, including skipped dirty worktrees
make prune KEEP=20                # remove the excess
make prune PRUNE_ARGS=--delete-merged-branches  # also branches merged into master/main
make prune PRUNE_ARGS=--force-dirty  # also remove worktrees with unsaved work
```

A branch is deleted only with `git branch -d`, that is, only when it is fully merged into
`master`/`main`; a repository outside `AGENTBUS_REPOS` is skipped with a warning. Do not
run `git worktree prune` from the VM: worktrees are created on the host and look
`prunable` inside the VM.

## Data model

`threads` — repository, base branch, OpenCode session id.
`messages` — sender, recipient, type, body, `meta`, state, `parent_id`, `attempt`,
`lease_owner`, `lease_until`.
`events` — append-only log: creation, lease, reply, state change, expired lease return.
Queue time, run time, retries and stats are computed from it.
`workers` — slot registry: `<agent>#<slot>`, host, pid, model, harness, state, current
message, heartbeat.

Types: `task`, `fix`, `question`, `note` (work); `result`, `verdict`, `ack` (replies).
States: `queued → leased → done | failed | cancelled`.

Leasing runs under a short advisory lock: one thread, one execution (a thread cannot have
two `leased` messages), the sender with fewer current leases first, then the oldest
messages. `AGENTBUS_PER_SENDER_LEASES` limits concurrent leases of one session. An
expired lease returns to the queue on the next lease and `attempt` grows.

## Security model

- The API listens on `127.0.0.1` by default. For the VM, set
  `AGENTBUS_BIND_HOST=<bridge-ip>`, for example the `virbr0` address. Never expose it to
  the outside.
- Every request requires `Authorization: Bearer $AGENTBUS_TOKEN`.
- The `Host` header is checked against `AGENTBUS_ALLOWED_HOSTS` (defaults to
  `127.0.0.1:<port>,localhost:<port>`) to block DNS rebinding.
- The worker refuses to run in a repository outside `AGENTBUS_REPOS`.
- `/file` reads only inside the message worktree; going above it returns 400.
- The worker never pushes and never merges. Review belongs to the planner, merging to a
  human.

**IMPORTANT:** anyone who can write into a mailbox folder can make the executor start an
LLM agent with shell access inside the allowed repositories. Treat the shared directory
and `AGENTBUS_REPOS` as a trust boundary, not as a convenience.

## Limits of v2

Deliberately not built: per-agent authorization, retries by error class, automerge,
metrics, cancelling a ticket from the dashboard, Kafka. The transport is Postgres; the
event log lives next to the state table and survives message deletion.

## Status

A personal tool. It runs its author's daily loop and is published as is: no stability
promises, no support guarantees. APIs, the database schema and file formats may change at
any time.

## How it was built

The specs in `docs/specs/` (Russian, internal development history) were written by a
planner agent and implemented by an executor agent through agentbus itself.
`docs/plan-*.md` is internal history too and also stays in Russian.

## License

MIT. See [LICENSE](LICENSE).
