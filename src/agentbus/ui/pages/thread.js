import {
  errorNote,
  fmtDuration,
  fmtTime,
  fmtTokens,
  h,
  isAbort,
  registerPage,
} from "../app.js";

const LOG_POLL_MS = 2000;
const LIVE_THROTTLE_MS = 2000;
const COLLAPSE_LINES = 20;
const BIG_DIFF_LINES = 3000;
const REFRESH_KINDS = new Set(["created", "leased", "done", "failed", "cancelled", "requeued"]);

registerPage("thread", { title: "Thread", render });

function render(root, params, ctx) {
  const id = params.id;
  if (!id) {
    root.replaceChildren(
      h("h1", {}, "Thread"),
      h("p", { class: "muted" }, "Pick a thread in Now or History."),
    );
    return;
  }

  const header = h("div", { class: "thread-header" });
  const status = h("div", { class: "status muted" });
  const timeline = h("div", { class: "timeline" });
  const eventsBox = h("div", { class: "panel-list" });

  root.replaceChildren(
    status,
    header,
    block("Timeline", timeline),
    block("Thread events", eventsBox),
  );

  const parts = [];
  let refreshTimer = null;
  let loading = false;
  let lastLoad = 0;

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const data = await ctx.api(`/v1/threads/${encodeURIComponent(id)}/timeline`);
      lastLoad = Date.now();
      renderAll(data);
      status.replaceChildren();
    } catch (error) {
      if (isAbort(error)) return;
      status.replaceChildren(errorNote(error));
    } finally {
      loading = false;
    }
  }

  function renderAll(data) {
    clearParts();
    const thread = data.thread || {};
    const messages = Array.isArray(data.messages) ? data.messages : [];
    header.replaceChildren(...headerNodes(thread, messages));
    const cards = messages.map((message) => messageCard(message));
    timeline.replaceChildren(...cards.map((card) => card.element));
    eventsBox.replaceChildren(...eventRows(data.events));
    messages.forEach((message, index) => {
      if (message.kind === "task" || message.kind === "fix") {
        attachLog(cards[index], message);
      }
      if (Array.isArray(message.artifacts) && message.artifacts.includes("patch.diff")) {
        attachDiff(cards[index], message);
      }
    });
  }

  function headerNodes(thread, messages) {
    const ticket = firstTicket(messages);
    const branch = lastBranch(messages) || (thread.id ? `agentbus/${thread.id}` : "");
    return [
      h(
        "div",
        { class: "thread-title" },
        h("span", { class: "badge" }, threadState(messages)),
        h("span", { class: "mono" }, thread.id || id),
        ticket ? h("span", { class: "badge kind-task" }, ticket) : null,
      ),
      h(
        "div",
        { class: "thread-meta muted" },
        metaItem("repository", thread.repo || "—"),
        metaItem("branch", branch || "—"),
        metaItem("base", thread.base_ref || "—"),
        metaItem("created by", thread.created_by || "—"),
        metaItem("created", fmtTime(thread.created_at)),
      ),
    ];
  }

  function messageCard(message) {
    const meta = message.meta || {};
    const head = h(
      "div",
      { class: "msg-head" },
      h("span", { class: `badge kind-${message.kind}` }, message.kind),
      h("span", { class: "mono" }, `${message.sender} → ${message.recipient}`),
      h("span", { class: "muted" }, fmtTime(message.created_at)),
      message.state && message.state !== "done" ? stateBadge(message.state) : null,
      message.attempt ? h("span", { class: "badge" }, `attempt ${message.attempt}`) : null,
      meta.decision ? h("span", { class: `badge decision-${meta.decision}` }, meta.decision) : null,
      meta.state_note ? h("span", { class: "badge note" }, meta.state_note) : null,
    );
    const extras = h("div", { class: "msg-extra" });
    extras.append(...resultFacts(message), ...artifactLinks(message));
    const element = h(
      "article",
      { class: `card msg msg-${message.kind}` },
      head,
      bodyBlock(message.body),
      extras,
    );
    return { element, extras };
  }

  function attachDiff(card, message) {
    const body = h("div", { class: "diff-body" }, h("p", { class: "muted" }, "loading diff…"));
    const box = h("details", { class: "diff-box" }, h("summary", {}, "diff"), body);
    box.open = true;
    card.extras.append(box);
    ctx
      .api(`/v1/messages/${encodeURIComponent(message.id)}/artifact?name=patch.diff`)
      .then((data) => {
        body.replaceChildren(...renderDiff(data.content || "", Boolean(data.truncated)));
      })
      .catch((error) => {
        if (isAbort(error)) return;
        body.replaceChildren(h("p", { class: "muted" }, `failed to load diff: ${error.message}`));
      });
  }

  function attachLog(card, message) {
    const feed = h("div", { class: "feed" });
    const empty = h("p", { class: "muted" }, "log is empty");
    const box = h(
      "details",
      { class: "feed-box" },
      h("summary", {}, "executor feed"),
      empty,
      feed,
    );
    box.open = true;
    card.extras.append(box);

    const tail = { cursor: 0, timer: null, seen: 0, running: true };

    async function poll() {
      try {
        const data = await ctx.api(
          `/v1/messages/${encodeURIComponent(message.id)}/log?after=${tail.cursor}`,
        );
        const rows = Array.isArray(data.lines) ? data.lines : [];
        const atBottom = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 40;
        for (const row of rows) appendLogRow(feed, row);
        const next = Number(data.next);
        if (Number.isFinite(next) && next > tail.cursor) tail.cursor = next;
        tail.seen += rows.length;
        empty.hidden = tail.seen > 0;
        tail.running = Boolean(data.running);
        if (!tail.running && tail.timer) clearInterval(tail.timer);
        if (rows.length && atBottom) feed.scrollTop = feed.scrollHeight;
      } catch (error) {
        if (tail.timer) clearInterval(tail.timer);
        if (isAbort(error)) return;
        if (!tail.seen) empty.textContent = `log unavailable: ${error.message}`;
      }
    }

    poll();
    tail.timer = setInterval(poll, LOG_POLL_MS);
    parts.push(() => {
      if (tail.timer) clearInterval(tail.timer);
    });
  }

  function scheduleRefresh() {
    if (refreshTimer) return;
    const wait = Math.max(0, LIVE_THROTTLE_MS - (Date.now() - lastLoad));
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      load();
    }, wait);
  }

  function clearParts() {
    for (const fn of parts.splice(0)) {
      try {
        fn();
      } catch (error) {
        console.error(error);
      }
    }
  }

  ctx.onLive((event) => {
    if (!event || event.thread_id !== id) return;
    if (event.kind && !REFRESH_KINDS.has(event.kind)) return;
    scheduleRefresh();
  });
  load();
  ctx.onUnmount(() => {
    clearParts();
    if (refreshTimer) clearTimeout(refreshTimer);
  });
}

// --- messages -----------------------------------------------------------------

function stateBadge(state) {
  return h("span", { class: `badge state-${state}` }, state);
}

function bodyBlock(text) {
  const body = h("div", { class: "body" }, text || "");
  if (String(text || "").split("\n").length <= COLLAPSE_LINES) return body;
  body.classList.add("collapsed");
  const toggle = h("button", { class: "link-button", type: "button" }, "show all");
  toggle.addEventListener("click", () => {
    const collapsed = body.classList.toggle("collapsed");
    toggle.textContent = collapsed ? "show all" : "collapse";
  });
  return h("div", { class: "body-wrap" }, body, toggle);
}

function resultFacts(message) {
  if (message.kind !== "result") return [];
  const meta = message.meta || {};
  const facts = [];
  if (meta.duration_seconds !== undefined && meta.duration_seconds !== null) {
    facts.push(`duration ${fmtDuration(meta.duration_seconds)}`);
  }
  const tokens = meta.usage && meta.usage.tokens;
  if (tokens) facts.push(`tokens ${fmtTokens(tokens.input)} / ${fmtTokens(tokens.output)}`);
  if (meta.model) facts.push(`model ${meta.model}`);
  if (meta.harness) facts.push(`harness ${meta.harness}`);
  if (meta.timeout) facts.push("timeout");
  const nodes = [];
  if (facts.length) {
    nodes.push(h("div", { class: "facts" }, facts.map((fact) => h("span", { class: "badge" }, fact))));
  }
  if (meta.checks) nodes.push(checksBlock(meta.checks));
  return nodes;
}

function checksBlock(checks) {
  const skipped = checks.skipped;
  const ok = checks.ok === true;
  const label = skipped
    ? `checks skipped (${skipped})`
    : ok
      ? "checks ok"
      : `checks fail${checks.exit_code !== undefined ? ` (exit ${checks.exit_code})` : ""}`;
  const badge = h(
    "span",
    { class: `badge${skipped ? "" : ok ? " checks-ok" : " checks-fail"}` },
    label,
  );
  if (!checks.tail) return h("div", { class: "checks" }, h("div", { class: "facts" }, badge));
  const tail = h("pre", { class: "checks-tail mono" }, checks.tail);
  tail.hidden = true;
  const toggle = h("button", { class: "link-button", type: "button" }, "tail");
  toggle.addEventListener("click", () => {
    tail.hidden = !tail.hidden;
    toggle.textContent = tail.hidden ? "tail" : "hide tail";
  });
  return h("div", { class: "checks" }, h("div", { class: "facts" }, badge, toggle), tail);
}

function artifactLinks(message) {
  const names = Array.isArray(message.artifacts) ? message.artifacts : [];
  if (!names.length) return [];
  return [
    h(
      "div",
      { class: "artifacts" },
      h("span", { class: "muted" }, "artifacts:"),
      ...names.map((name) =>
        h(
          "a",
          {
            class: "mono",
            href: `/v1/messages/${encodeURIComponent(message.id)}/artifact?name=${encodeURIComponent(name)}`,
            target: "_blank",
            rel: "noreferrer",
          },
          name,
        ),
      ),
    ),
  ];
}

function appendLogRow(feed, row) {
  const raw = h("pre", { class: "raw mono" }, JSON.stringify(row.detail ?? row, null, 2));
  raw.hidden = true;
  const line = h(
    "div",
    { class: `log-row log-${row.type}`, tabindex: "0", role: "button" },
    h("span", { class: "log-time" }, timeOf(row.ts)),
    h("span", { class: "log-tool mono" }, row.tool || (row.type === "text" ? "agent" : row.type)),
    h("span", { class: "log-summary" }, row.summary || ""),
    h("span", { class: "log-status" }, row.status || ""),
  );
  const toggle = () => {
    raw.hidden = !raw.hidden;
  };
  line.addEventListener("click", toggle);
  line.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    toggle();
  });
  feed.append(line, raw);
}

function eventRows(events) {
  if (!Array.isArray(events) || !events.length) {
    return [h("p", { class: "muted" }, "no events")];
  }
  return events.map((event) =>
    h(
      "div",
      { class: "event-row" },
      h("span", { class: "mono muted" }, fmtTime(event.at)),
      h("span", { class: `badge kind-${event.kind}` }, event.kind),
      h("span", { class: "mono muted" }, event.message_id || "—"),
      h("span", { class: "muted actor" }, event.actor || "system"),
    ),
  );
}

function firstTicket(messages) {
  for (const message of messages) {
    const ticket = (message.meta || {}).ticket;
    if (ticket) return ticket;
  }
  return "";
}

function lastBranch(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const meta = messages[index].meta || {};
    if (messages[index].kind === "result" && meta.branch) return meta.branch;
  }
  return "";
}

function threadState(messages) {
  if (!messages.length) return "empty";
  if (messages.some((message) => message.state === "leased")) return "in progress";
  const last = messages[messages.length - 1];
  if (last.kind === "result" && last.state === "queued") return "awaiting review";
  if (messages.some((message) => message.state === "queued")) return "queued";
  if (messages.some((message) => message.state === "failed")) return "failed";
  return "done";
}

function metaItem(name, value) {
  return h("span", { class: "meta-item" }, h("span", { class: "muted" }, `${name}: `), value);
}

function timeOf(timestamp) {
  if (!timestamp) return "";
  const date = new Date(Number(timestamp) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("en-US", { hour12: false });
}

// --- unified diff -------------------------------------------------------------

function parseDiff(text) {
  const files = [];
  let file = null;
  let oldNo = 0;
  let newNo = 0;
  const lines = String(text || "").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();

  function start(path) {
    file = { path: path || "", rows: [], added: 0, removed: 0 };
    files.push(file);
  }

  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      start(pathFromHeader(line));
      continue;
    }
    if (!file) start("");
    if (line.startsWith("@@")) {
      const match = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldNo = Number(match[1]);
        newNo = Number(match[2]);
      }
      file.rows.push({ kind: "hunk", text: line });
      continue;
    }
    if (isMetaLine(line)) {
      file.rows.push({ kind: "meta", text: line });
      continue;
    }
    if (line.startsWith("+")) {
      file.rows.push({ kind: "add", old: null, new: newNo, text: line });
      newNo += 1;
      file.added += 1;
      continue;
    }
    if (line.startsWith("-")) {
      file.rows.push({ kind: "del", old: oldNo, new: null, text: line });
      oldNo += 1;
      file.removed += 1;
      continue;
    }
    file.rows.push({ kind: "context", old: oldNo, new: newNo, text: line });
    oldNo += 1;
    newNo += 1;
  }
  return files;
}

function isMetaLine(line) {
  return (
    line.startsWith("+++") ||
    line.startsWith("---") ||
    line.startsWith("index ") ||
    line.startsWith("new file mode") ||
    line.startsWith("deleted file mode") ||
    line.startsWith("old mode") ||
    line.startsWith("similarity index") ||
    line.startsWith("rename ")
  );
}

function pathFromHeader(line) {
  const match = line.match(/^diff --git a\/(.*?) b\/(.*)$/);
  if (match) return match[2];
  return line.replace(/^diff --git /, "");
}

function renderDiff(text, truncated) {
  const files = parseDiff(text);
  const big = String(text || "").split("\n").length > BIG_DIFF_LINES;
  const nodes = files.map((file) => {
    const details = h("details", { class: "diff-file" });
    details.open = !big;
    details.append(
      h(
        "summary",
        {},
        h("span", { class: "mono diff-path" }, file.path || "diff"),
        h("span", { class: "diff-stat muted" }, `+${file.added} −${file.removed}`),
      ),
      h("div", { class: "diff-body" }, file.rows.map(diffRow)),
    );
    return details;
  });
  if (!files.length) nodes.push(h("p", { class: "muted" }, "diff is empty"));
  if (truncated) nodes.push(h("p", { class: "muted" }, "diff was truncated by the server"));
  return nodes;
}

function diffRow(row) {
  if (row.kind === "hunk" || row.kind === "meta") {
    return h(
      "div",
      { class: `diff-row diff-${row.kind}` },
      h("span", { class: "diff-text" }, row.text),
    );
  }
  return h(
    "div",
    { class: `diff-row diff-${row.kind}` },
    h("span", { class: "diff-no" }, row.old === null ? "" : String(row.old)),
    h("span", { class: "diff-no" }, row.new === null ? "" : String(row.new)),
    h("span", { class: "diff-text" }, row.text),
  );
}

function block(title, ...content) {
  return h("section", { class: "block" }, h("h2", {}, title), ...content);
}
