---
name: agentbus
description: "Delegating tasks to an OpenCode executor on the host through the local agentbus queue: several mailboxes, parallel tickets, waiting for replies and accepting the result. Use when you need to delegate implementation, send a task to an executor, fan out independent tickets in parallel, wait for replies, review the diff and issue a verdict. Triggers: agentbus, cb, executor, opencode, delegate task, parallel tickets, wait for reply, accept result, verdict."
---

# agentbus

You are the planner and reviewer. The executor — OpenCode on the host — does the work
in a separate git worktree and returns a report with a diff. There are several
executors: a pool of slots runs on the host, so independent tickets can be dispatched
right away. You have no network: everything goes through a directory that Cage has
already shared into the VM.

## Tools

- Client: `<agentbus checkout>/bin/cb` (python3, nothing to install). Either put it on
  PATH or set `CB=~/agentbus/bin/cb`.
- `cb env` — which mailbox is selected, why, the paths and the unread count.
- `AGENTBUS_SHARE` is not needed on every command when the mailbox picks itself.

## Mailbox

`cb` selects a mailbox in this order:

1. `AGENTBUS_SHARE` — full path to the mailbox folder;
2. `AGENTBUS_MAILBOX=<name>` — `<root>/<name>`;
3. inside a git repository — `claude-<repository folder name>`;
4. otherwise `cage-claude`.

Set `AGENTBUS_MAILBOX` explicitly — in the session env or on every command: otherwise
changing cwd (opening another project) can route replies to a different mailbox.
`cb env` prints the selected mailbox and the reason for the choice.

## Sending a task

```bash
CB=~/agentbus/bin/cb

$CB send --to local-opencode \
  --repo /path/to/repo \
  --meta '{"ticket":"T3","acceptance":["make check is green","tests for the new case"],"scope_files":["src/agentbus/"],"checks":true}' \
  --body "Implement T3 per docs/specs/T3.md. Only the files listed in the spec."
```

`cb send` prints `id` and `thread_id` from the bridge receipt. `--wait-id N` sets how
many seconds to wait for it (`0` — do not wait). Without a receipt it prints everything
it knows plus a warning on stderr.

What `meta` must contain:

- `ticket` — short ticket name, shown in `cb inbox` and on the dashboard;
- `acceptance` — acceptance criteria as a list. Without them the executor invents its
  own;
- `scope_files` — where to work, so it does not spread across the repository.

Optional: `checks: true` — run `make check` in the worktree (expensive: starts Docker);
`checks_parallel: true` — when the project's tests are isolated and do not interfere,
so the check does not take the repository lock.

`--repo` is an absolute path. The executor only accepts whatever the host sets in
`AGENTBUS_REPOS`; anything else is rejected. `--base` — the branch or commit to start
from (defaults to the repository's current branch). `--thread <id>` — continue an
existing thread instead of opening a new one. `--kind task` (default) or `question` for
questions that need no code changes.

## Parallelism

The executor pool is configured by `AGENTBUS_WORKER_SLOTS` on the host; each slot is a
separate lease owner `<agent>#<slot>` with its own worktree per thread.

- Send independent tickets (non-overlapping `scope_files`, different threads) right
  away: free slots pick them up simultaneously.
- Send a dependent ticket only after the previous one is accepted and merged into the
  main branch, otherwise you get a conflict for no reason.
- One thread, one execution: the queue will not hand out a second message of a thread
  while one is `leased`. Fairness between sessions (the sender with fewer current
  leases first) is the queue's job, not yours.
- Do not send two tasks into one thread "for speed" — they will just queue up.

## Waiting

```bash
$CB wait <id> [<id> ...] [--all] [--timeout 600]
```

`cb wait` waits in its own mailbox for replies with a `parent_id` among the given ids:
by default it returns after the first one, with `--all` — after all of them, and
`--timeout` (3600 seconds by default) exits with code 2. A reply line contains the
ticket, `checks=ok|fail|-` and `timeout=true|false`. Polling `cb inbox` in a loop is not
needed. When the bridge manages to write `status.json`, `cb wait` also prints state
transitions of the awaited messages on stderr:
`m_… queued → leased by local-opencode#1`.

## State

```bash
$CB status                 # status.json freshness, slots, my messages
$CB status m_... --log     # only these messages, with the run.log tail of busy ones
$CB status --json          # the raw file
```

Every `AGENTBUS_STATUS_INTERVAL` seconds the bridge rewrites `<mailbox>/status.json`:
pool slots, queues per mailbox, this mailbox's messages and the latest events of its
threads. `cb status` reads the file rather than the API, so it shows state delayed by up
to one interval.

```
status: 3s ago
local-opencode#0 busy m_… [OPS-prune] 4m
local-opencode#1 idle
m_… [T11] leased by local-opencode#1, attempt 1, 6m
m_… [T12] queued, ~2nd in line
m_… [T13] done → result m_…
```

- `queued, ~2nd in line` — an estimate based on the number of earlier messages for the
  same recipient; the queue is more accurate than this number, so a lease can overtake
  it.
- `leased by <slot>, attempt N` — who took it and which attempt.
- `done → result m_…` — the message is closed by a reply; `failed`/`cancelled` have no
  arrow.

`status is stale (Ns) — bridge or API down?` with exit code 2 means the file is older
than `max(30, 6*interval)` seconds or missing. The bridge or the API is not running; the
task is not lost — it stays in the queue. `cb env` shows the file's age. For someone
else's busy slot you only see the mailbox name (`sender`); the ticket and text of
another agent's task never make it into the file.

## Collecting the result

```bash
$CB inbox                      # what arrived: id, kind, sender, ticket, first line
$CB show <id> --text           # report and diff_stat
$CB show <id> | jq .meta       # branch, files_changed, checks, usage, model, harness
$CB patch <id>                 # the thread's accumulated diff
cat ~/.agentbus/<mailbox>/inbox/<id>/attempt.diff   # diff of the last attempt only
```

The executor works on the `agentbus/<thread_id>` branch. It does not push and does not
merge.

## Review

- Look at the diff, not just the report text: `patch` is the thread's accumulated diff,
  `attempt.diff` only the last attempt. Reports can be rosier than reality.
- `meta.checks` — the `make check` result in the worktree (`ok`, `exit_code`, tail of
  the output).
- `meta.usage` — steps, tool calls, tokens and the cost of the run.
- `exit_code 143` — the executor was killed by a signal (timeout or worker shutdown).
  Answer with a verdict to "continue from the current state": the context and the
  worktree are preserved.

## Verdict and acceptance

Your feedback goes to the same worktree and the same OpenCode session, so the context is
preserved:

```bash
$CB reply <result_id> --kind verdict \
  --body "There is no test for empty input. Also drop the debug print in api.py:112."
```

`cb reply --kind verdict` sets `meta.decision = "rework"` by default and inherits
`ticket`. For a question without code changes: `--kind question`. Once you accept the
result, run `$CB accept <id> [--note "accepted"]`: it writes `decision.json` and closes
the message in the queue. `cb read <id>` only removes the message from the inbox without
marking it accepted.

## Merge

The worker commits every attempt to the `agentbus/<thread_id>` branch. Merge by branch
(`git merge --ff-only agentbus/<thread_id>` or a regular merge), never by patch: a patch
loses history and authorship. The executor does not push and does not merge; deployment
is not its job either.

## What not to do from the VM

- `git worktree prune` in executor repositories. The worktrees live on the host under
  `~/.local/state/agentbus/worktrees`; inside the VM they look `prunable`, but cleaning
  them breaks the running task. Cleanup is `make prune` on the host (start with
  `PRUNE_ARGS=--dry-run`).
- Push, merge into `main`, or switch branches in the executor's worktree.

## Dashboard

On the host: `http://127.0.0.1:8787/ui/login?token=<AGENTBUS_TOKEN>`. It shows slots,
queues per mailbox, a thread with its timeline, diff, `checks.txt` and live `run.log`,
plus history and statistics per mailbox, repository and model.

## When something is wrong

- `failed` — `cb show <id>` prints `meta.state_note`: usually the repository is outside
  the allowlist or the task was missing a required field.
- The task was killed by a timeout — the report arrives with `timeout: true`, the log is
  in `run.log`. Split the task into smaller pieces.
- No reply for more than five minutes on a small task — tell the human: the worker or
  the service may be down.
- Diffs larger than 256 KiB are truncated: `meta.patch_bytes` holds the real size
  while `diff_stat` and `files_changed` are complete.

## What not to do

- Do not ask to push, merge into `main` or deploy — none of that will happen.
- Do not send secrets or tokens: the executor does not need them.
- Do not send a task without acceptance criteria and without naming the files.
- Do not send the same task twice — that is a duplicate. Clarify with a verdict
  instead.
- Do not consider a task done without seeing the diff.
