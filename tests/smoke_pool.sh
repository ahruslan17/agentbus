#!/usr/bin/env bash
# Two mailboxes, two executor slots, no model tokens: proves that the pool hands the
# two independent threads to different leases, that a third lease stays empty, and that
# every sender sees only the answer to its own task.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$PWD/bin:$PATH"

ENV_FILE="${AGENTBUS_ENV:-$HOME/.config/agentbus/env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
: "${AGENTBUS_URL:=http://127.0.0.1:8787}"
AGENTBUS_URL="${AGENTBUS_URL%/}"
: "${AGENTBUS_TOKEN:?AGENTBUS_TOKEN is not set}"

api() {
  curl -sS -H "Authorization: Bearer $AGENTBUS_TOKEN" "$AGENTBUS_URL$1"
}

ab health | jq -e '.ok' >/dev/null

send_task() {
  local mailbox=$1 ticket=$2
  AGENTBUS_SELF="$mailbox" ab send --to local-opencode --kind task \
    --meta "{\"ticket\":\"$ticket\",\"acceptance\":[\"smoke pool\"]}" \
    "smoke pool: $ticket"
}

a=$(send_task smoke-a SMOKE-A)
b=$(send_task smoke-b SMOKE-B)
a_id=$(jq -r '.id' <<<"$a")
a_thread=$(jq -r '.thread_id' <<<"$a")
b_id=$(jq -r '.id' <<<"$b")
b_thread=$(jq -r '.thread_id' <<<"$b")
for value in "$a_id" "$a_thread" "$b_id" "$b_thread"; do
  [ "$value" != "null" ] && [ -n "$value" ] || { echo "no receipt from the bridge" >&2; exit 1; }
done
[ "$a_thread" != "$b_thread" ] || { echo "both tasks landed in one thread" >&2; exit 1; }
echo "sent $a_id ($a_thread) and $b_id ($b_thread)"

first=$(ab lease local-opencode --wait 5 --owner 'local-opencode#0')
second=$(ab lease local-opencode --wait 5 --owner 'local-opencode#1')
jq -e '.lease_owner == "local-opencode#0"' >/dev/null <<<"$first"
jq -e '.lease_owner == "local-opencode#1"' >/dev/null <<<"$second"

leased=$(printf '%s\n%s\n' "$(jq -r '.id' <<<"$first")" "$(jq -r '.id' <<<"$second")" | sort)
expected=$(printf '%s\n%s\n' "$a_id" "$b_id" | sort)
[ "$leased" = "$expected" ] || {
  echo "leases got $leased, expected $expected" >&2
  exit 1
}
[ "$(jq -r '.thread_id' <<<"$first")" != "$(jq -r '.thread_id' <<<"$second")" ] || {
  echo "two slots leased one thread" >&2
  exit 1
}
echo "two slots leased $a_id and $b_id in different threads"

if third=$(ab lease local-opencode --wait 1 --owner 'local-opencode#2' 2>/dev/null); then
  echo "third lease must be empty, got $third" >&2
  exit 1
fi
echo "third lease is empty"

AGENTBUS_SELF=local-opencode ab reply "$a_id" --kind result --meta '{"fake":true}' \
  "PONG-A" >/dev/null
AGENTBUS_SELF=local-opencode ab reply "$b_id" --kind result --meta '{"fake":true}' \
  "PONG-B" >/dev/null

inbox_a=$(api /v1/inbox/smoke-a)
inbox_b=$(api /v1/inbox/smoke-b)
jq -e --arg a "$a_id" --arg b "$b_id" '
  ([.messages[] | select(.parent_id == $a and .body == "PONG-A")] | length) == 1
  and ([.messages[] | select(.parent_id == $b)] | length) == 0
' <<<"$inbox_a" >/dev/null
jq -e --arg a "$a_id" --arg b "$b_id" '
  ([.messages[] | select(.parent_id == $b and .body == "PONG-B")] | length) == 1
  and ([.messages[] | select(.parent_id == $a)] | length) == 0
' <<<"$inbox_b" >/dev/null
echo "each mailbox sees only its own answer"

overview=$(api /v1/overview)
jq -e '
  ([.queues[] | select(.recipient == "smoke-a" and .queued >= 1)] | length) == 1
  and ([.queues[] | select(.recipient == "smoke-b" and .queued >= 1)] | length) == 1
' <<<"$overview" >/dev/null
echo "overview shows queues of both mailboxes"

threads_a=$(api "/v1/threads?mailbox=smoke-a")
jq -e --arg a "$a_thread" --arg b "$b_thread" '
  ([.threads[].id] | index($a)) != null
  and ([.threads[].id] | index($b)) == null
' <<<"$threads_a" >/dev/null
echo "/v1/threads?mailbox=smoke-a lists only $a_thread"

reply=$(ab wait "$a_id" --timeout 30)
jq -e --arg a "$a_id" '.parent_id == $a and .body == "PONG-A"' >/dev/null <<<"$reply"

echo "smoke pool ok: $a_id / $b_id, threads $a_thread / $b_thread"
