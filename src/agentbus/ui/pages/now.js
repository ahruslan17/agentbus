import {
  errorNote,
  fmtDuration,
  fmtTime,
  h,
  isAbort,
  registerPage,
} from "../app.js";

const OVERVIEW_MS = 10_000;
const LIVE_THROTTLE_MS = 1000;
const TAIL_MS = 2000;
const TAIL_LINES = 3;

registerPage("now", { title: "Now", render });

function render(root, _params, ctx) {
  const slots = h("div", { class: "slots" });
  const slotsEmpty = h("p", { class: "muted" }, "no slots: worker is not running");
  const queues = h("div", { class: "panel-list" });
  const review = h("div", { class: "panel-list" });
  const events = h("div", { class: "panel-list" });
  const status = h("div", { class: "status muted" });

  root.replaceChildren(
    h("h1", {}, "Now"),
    status,
    block("Slots", slots, slotsEmpty),
    block(
      "Queues",
      h(
        "div",
        { class: "cols" },
        column("By recipient", queues),
        column("Awaiting review", review),
      ),
    ),
    block("Recent events", events),
  );

  const state = {
    cards: new Map(),
    visibility: new Map(),
    observer: null,
    loading: false,
    lastLoad: 0,
    refreshTimer: null,
  };

  if (typeof IntersectionObserver === "function") {
    state.observer = new IntersectionObserver((entries) => {
      for (const entry of entries) state.visibility.set(entry.target, entry.isIntersecting);
    });
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      const data = await ctx.api("/v1/overview");
      state.lastLoad = Date.now();
      applyOverview(data);
      status.textContent = "";
    } catch (error) {
      if (isAbort(error)) return;
      status.replaceChildren(errorNote(error));
    } finally {
      state.loading = false;
    }
  }

  function applyOverview(data) {
    const workers = Array.isArray(data.workers) ? data.workers : [];
    applyWorkers(workers);
    slotsEmpty.hidden = workers.length > 0;
    renderQueues(queues, data.queues, (row) => `${row.queued} queued · ${row.leased} in progress`);
    renderQueues(review, data.awaiting_review, (row) => `awaiting review: ${row.count}`);
    renderEvents(events, data.recent_events);
    tickAll();
  }

  function applyWorkers(workers) {
    const seen = new Set();
    for (const worker of workers) {
      seen.add(worker.id);
      let card = state.cards.get(worker.id);
      if (!card) {
        card = createCard(worker);
        state.cards.set(worker.id, card);
        slots.append(card.element);
        if (state.observer) state.observer.observe(card.element);
      }
      updateCard(card, worker);
    }
    for (const [id, card] of state.cards) {
      if (seen.has(id)) continue;
      destroyCard(card);
      state.cards.delete(id);
    }
  }

  function createCard(worker) {
    const stateBadge = h("span", { class: "badge" });
    const meta = h("div", { class: "slot-meta muted" });
    const task = h("div", { class: "slot-task" });
    const timing = h("div", { class: "slot-timing muted" });
    const tail = h("pre", { class: "log-tail mono" });
    const element = h(
      "article",
      { class: "card slot" },
      h("div", { class: "slot-head" }, h("span", { class: "mono slot-id" }, worker.id), stateBadge),
      meta,
      task,
      timing,
      tail,
    );
    return {
      element,
      stateBadge,
      meta,
      task,
      timing,
      tail,
      busy: false,
      messageId: null,
      cursor: 0,
      lines: [],
      timer: null,
      leasedAt: null,
    };
  }

  function updateCard(card, worker) {
    const name = worker.stale ? "stale" : worker.state || "?";
    card.element.dataset.state = name;
    card.stateBadge.textContent = name;
    card.stateBadge.className = `badge state-${name}`;
    card.meta.textContent =
      [worker.model, worker.harness, worker.host].filter(Boolean).join(" · ") || "—";
    card.busy = worker.state === "busy" && !worker.stale;
    const messageId = card.busy ? worker.message_id || null : null;
    if (messageId !== card.messageId) switchTask(card, messageId);
    tickCard(card);
  }

  function switchTask(card, messageId) {
    card.messageId = messageId;
    card.cursor = 0;
    card.lines = [];
    card.leasedAt = null;
    card.tail.textContent = "";
    card.task.replaceChildren();
    if (card.timer) clearInterval(card.timer);
    card.timer = null;
    if (!messageId) {
      card.task.textContent = card.busy ? "task…" : "idle";
      return;
    }
    card.task.append(h("span", { class: "mono" }, messageId));
    loadDetail(card, messageId);
    card.timer = setInterval(() => pullTail(card), TAIL_MS);
    pullTail(card);
  }

  async function loadDetail(card, messageId) {
    let message;
    try {
      message = await ctx.api(`/v1/messages/${encodeURIComponent(messageId)}`);
    } catch (error) {
      if (!isAbort(error)) card.task.append(h("span", { class: "muted" }, ` (details: ${error.message})`));
      return;
    }
    if (card.messageId !== messageId) return;
    const threadId = message.thread_id || null;
    const ticket = (message.meta || {}).ticket;
    card.task.replaceChildren(
      ticket ? h("span", { class: "badge" }, ticket) : null,
      threadId
        ? h("a", { class: "mono", href: `#/thread/${encodeURIComponent(threadId)}` }, threadId)
        : null,
      h("span", { class: "muted" }, message.kind),
    );
    if (threadId) loadLeaseStart(card, messageId, threadId);
  }

  async function loadLeaseStart(card, messageId, threadId) {
    try {
      const data = await ctx.api(`/v1/threads/${encodeURIComponent(threadId)}/events`);
      if (card.messageId !== messageId) return;
      const leased = (Array.isArray(data.events) ? data.events : [])
        .filter((event) => event.kind === "leased" && event.message_id === messageId)
        .pop();
      if (leased && leased.at) card.leasedAt = new Date(leased.at).getTime();
      tickCard(card);
    } catch (error) {
      if (isAbort(error)) return;
      // without a lease timestamp we just show "running"
    }
  }

  async function pullTail(card) {
    if (!card.messageId || !tailVisible(card)) return;
    const messageId = card.messageId;
    try {
      const data = await ctx.api(
        `/v1/messages/${encodeURIComponent(messageId)}/log?after=${card.cursor}`,
      );
      if (card.messageId !== messageId) return;
      for (const row of Array.isArray(data.lines) ? data.lines : []) card.lines.push(row);
      if (card.lines.length > TAIL_LINES) card.lines = card.lines.slice(-TAIL_LINES);
      const next = Number(data.next);
      if (Number.isFinite(next) && next > card.cursor) card.cursor = next;
      card.tail.textContent = card.lines.map(tailLine).join("\n");
      if (data.running === false && card.timer) {
        clearInterval(card.timer);
        card.timer = null;
      }
    } catch (error) {
      if (isAbort(error)) return;
      card.tail.textContent = `log unavailable: ${error.message}`;
    }
  }

  function tailVisible(card) {
    if (document.visibilityState === "hidden") return false;
    if (!card.element.isConnected) return false;
    return state.visibility.get(card.element) !== false;
  }

  function tickCard(card) {
    if (!card.busy) {
      card.timing.textContent = "";
      return;
    }
    card.timing.textContent = card.leasedAt
      ? `running for ${fmtDuration((Date.now() - card.leasedAt) / 1000)}`
      : "running";
  }

  function tickAll() {
    for (const card of state.cards.values()) tickCard(card);
  }

  function destroyCard(card) {
    if (card.timer) clearInterval(card.timer);
    card.timer = null;
    if (state.observer) state.observer.unobserve(card.element);
    state.visibility.delete(card.element);
    card.element.remove();
  }

  function scheduleRefresh() {
    if (state.refreshTimer) return;
    const wait = Math.max(0, LIVE_THROTTLE_MS - (Date.now() - state.lastLoad));
    state.refreshTimer = setTimeout(() => {
      state.refreshTimer = null;
      load();
    }, wait);
  }

  ctx.onLive(() => scheduleRefresh());
  load();
  const refresh = setInterval(load, OVERVIEW_MS);
  const ticks = setInterval(tickAll, 1000);
  ctx.onUnmount(() => {
    clearInterval(refresh);
    clearInterval(ticks);
    if (state.refreshTimer) clearTimeout(state.refreshTimer);
    if (state.observer) state.observer.disconnect();
    for (const card of state.cards.values()) {
      if (card.timer) clearInterval(card.timer);
    }
  });
}

function tailLine(row) {
  const time = row.ts ? fmtTime(new Date(Number(row.ts) * 1000)) : "";
  if (row.type === "text") return `${time}  💬 ${row.summary || ""}`.trim();
  return [time, row.tool || row.type, row.summary, row.status].filter(Boolean).join("  ");
}

function renderQueues(container, rows, label) {
  if (!Array.isArray(rows) || !rows.length) {
    container.replaceChildren(h("p", { class: "muted" }, "empty"));
    return;
  }
  container.replaceChildren(
    ...rows.map((row) =>
      h(
        "div",
        { class: "queue-row" },
        h("span", { class: "mono" }, row.recipient),
        h("span", { class: "muted" }, label(row)),
      ),
    ),
  );
}

function renderEvents(container, rows) {
  if (!Array.isArray(rows) || !rows.length) {
    container.replaceChildren(h("p", { class: "muted" }, "no events yet"));
    return;
  }
  container.replaceChildren(
    ...[...rows].reverse().map((event) =>
      h(
        "div",
        { class: "event-row" },
        h("span", { class: "mono muted" }, fmtTime(event.at)),
        h("span", { class: `badge kind-${event.kind}` }, event.kind),
        event.thread_id
          ? h(
              "a",
              { class: "mono thread-link", href: `#/thread/${encodeURIComponent(event.thread_id)}` },
              event.thread_id,
            )
          : h("span", { class: "muted" }, "—"),
        h("span", { class: "muted actor" }, event.actor || "system"),
      ),
    ),
  );
}

function block(title, ...content) {
  return h("section", { class: "block" }, h("h2", {}, title), ...content);
}

function column(title, ...content) {
  return h("div", { class: "column" }, h("h3", {}, title), ...content);
}
