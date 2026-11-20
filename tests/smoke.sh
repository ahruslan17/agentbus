#!/usr/bin/env bash
# Proves the whole loop without spending model tokens: a fake executor leases the task
# and replies, then the sender waits for the reply the same way Claude would.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$PWD/bin:$PATH"
export AGENTBUS_SELF="${AGENTBUS_SELF:-cage-claude}"

ab health | jq -e '.ok' >/dev/null

task=$(ab send --to local-opencode --repo "$PWD" --kind task --meta '{"acceptance":["smoke"]}' \
  "smoke: ответь словом PONG")
task_id=$(jq -r '.id' <<<"$task")
thread_id=$(jq -r '.thread_id' <<<"$task")
echo "sent $task_id in $thread_id"

leased=$(ab lease local-opencode --wait 5)
[ "$(jq -r '.id' <<<"$leased")" = "$task_id" ] || { echo "wrong message leased" >&2; exit 1; }

AGENTBUS_SELF=local-opencode ab reply "$task_id" --kind result --meta '{"fake":true}' "PONG" >/dev/null

reply=$(ab wait "$task_id" --timeout 30)
jq -e '.body == "PONG"' >/dev/null <<<"$reply"
jq -e --arg id "$task_id" '.parent_id == $id' >/dev/null <<<"$reply"

state=$(ab show "$task_id" | jq -r '.state')
[ "$state" = "done" ] || { echo "parent state is $state" >&2; exit 1; }

echo "smoke ok: $task_id done, reply $(jq -r '.id' <<<"$reply")"
