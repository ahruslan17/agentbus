import { errorNote, fmtTime, h, isAbort, registerPage } from "../app.js";

const LIVE_THROTTLE_MS = 2000;
const TICK_MS = 15_000;
const STYLE_ID = "t7-mailboxes-style";

const COLUMNS = [
  { label: "Ящик" },
  { label: "Задач", num: true, title: "Сколько задач отправил ящик" },
  { label: "В работе", num: true, title: "Треды ящика с сообщением в lease" },
  { label: "Ждут ревью", num: true, title: "Результаты в очереди" },
  { label: "Принято", num: true, title: "Результаты с state_note accept*" },
  { label: "Сбои", num: true, title: "Сообщения ящика в состоянии failed" },
  { label: "Последняя активность" },
];

const STYLE = `
.t7-table-wrap { overflow-x: auto; }
.t7-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
.t7-table th {
  padding: 0.35rem 0.5rem;
  border-bottom: 1px solid var(--border);
  color: var(--muted);
  font-weight: 600;
  text-align: left;
  white-space: nowrap;
}
.t7-table td { padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border); vertical-align: baseline; }
.t7-table tbody tr:last-child td { border-bottom: 0; }
.t7-table tbody tr { cursor: pointer; }
.t7-table tbody tr:hover { background: var(--panel-2); }
.t7-table tbody tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.t7-num { text-align: right; font-variant-numeric: tabular-nums; }
.t7-mb-cards { display: none; gap: 0.6rem; }
.t7-mb-card {
  display: grid;
  gap: 0.4rem;
  padding: 0.7rem 0.85rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--text);
}
.t7-mb-card:hover { text-decoration: none; border-color: var(--accent); }
.t7-mb-name { font-family: var(--code); font-size: 0.95rem; overflow-wrap: anywhere; }
.t7-mb-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.15rem 0.8rem;
  font-size: 0.82rem;
}
.t7-mb-metric { display: flex; justify-content: space-between; gap: 0.5rem; }
.t7-mb-table[hidden],
.t7-mb-cards[hidden] { display: none; }
@media (max-width: 640px) {
  .t7-mb-table { display: none; }
  .t7-mb-cards { display: grid; }
}
`;

registerPage("mailboxes", { title: "Ящики", render });

function render(root, _params, ctx) {
  ensureStyles();

  const status = h("div", { class: "status muted" });
  const empty = h("p", { class: "muted" }, "ящиков пока нет");
  const body = h("tbody");
  const table = h(
    "div",
    { class: "t7-table-wrap t7-mb-table" },
    h(
      "table",
      { class: "t7-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          COLUMNS.map((column) =>
            h(
              "th",
              { class: column.num ? "t7-num" : null, title: column.title || null },
              column.label,
            ),
          ),
        ),
      ),
      body,
    ),
  );
  const cards = h("div", { class: "t7-mb-cards" });

  root.replaceChildren(h("h1", {}, "Ящики"), status, empty, table, cards);
  empty.hidden = true;
  table.hidden = true;
  cards.hidden = true;

  const times = [];
  let loading = false;
  let lastLoad = 0;
  let refreshTimer = null;

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const data = await ctx.api("/v1/mailboxes");
      lastLoad = Date.now();
      renderRows(Array.isArray(data.mailboxes) ? data.mailboxes : []);
      status.replaceChildren();
    } catch (error) {
      if (isAbort(error)) return;
      status.replaceChildren(errorNote(error));
    } finally {
      loading = false;
    }
  }

  function renderRows(rows) {
    times.length = 0;
    body.replaceChildren(...rows.map(rowNode));
    cards.replaceChildren(...rows.map(cardNode));
    empty.hidden = rows.length > 0;
    table.hidden = rows.length === 0;
    cards.hidden = rows.length === 0;
    tickTimes();
  }

  function rowNode(row) {
    const href = ctx.link(`/history?mailbox=${encodeURIComponent(row.mailbox)}`);
    const node = h(
      "tr",
      { tabindex: "0" },
      h("td", {}, h("a", { class: "mono", href }, row.mailbox)),
      metricCell(row.tasks),
      metricCell(row.in_work),
      metricCell(row.awaiting_review),
      metricCell(row.accepted),
      metricCell(row.failed),
      activityCell(row.last_activity),
    );
    bindOpen(node, href);
    return node;
  }

  function cardNode(row) {
    const href = ctx.link(`/history?mailbox=${encodeURIComponent(row.mailbox)}`);
    return h(
      "a",
      { class: "t7-mb-card", href },
      h("span", { class: "t7-mb-name" }, row.mailbox),
      h(
        "span",
        { class: "t7-mb-grid" },
        cardMetric("задач", formatCount(row.tasks)),
        cardMetric("в работе", formatCount(row.in_work)),
        cardMetric("ждут ревью", formatCount(row.awaiting_review)),
        cardMetric("принято", formatCount(row.accepted)),
        cardMetric("сбои", formatCount(row.failed)),
        cardMetric("активность", fmtAgo(row.last_activity), fmtTime(row.last_activity)),
      ),
    );
  }

  function metricCell(value) {
    return h("td", { class: "t7-num" }, formatCount(value));
  }

  function activityCell(value) {
    const node = h("span", { class: "muted", title: fmtTime(value) }, fmtAgo(value));
    times.push({ node, value });
    return h("td", {}, node);
  }

  function cardMetric(label, shown, title) {
    return h(
      "span",
      { class: "t7-mb-metric" },
      h("span", { class: "muted" }, label),
      h("span", { class: "t7-num", title: title || null }, shown),
    );
  }

  function bindOpen(node, href) {
    const open = () => {
      location.hash = href;
    };
    node.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      open();
    });
    node.addEventListener("keydown", (event) => {
      if (event.target.closest("a")) return;
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      open();
    });
  }

  function tickTimes() {
    for (const entry of times) {
      entry.node.textContent = fmtAgo(entry.value);
    }
  }

  function scheduleRefresh() {
    if (refreshTimer) return;
    const wait = Math.max(0, LIVE_THROTTLE_MS - (Date.now() - lastLoad));
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      load();
    }, wait);
  }

  ctx.onLive(() => scheduleRefresh());
  load();
  const ticks = setInterval(tickTimes, TICK_MS);
  ctx.onUnmount(() => {
    clearInterval(ticks);
    if (refreshTimer) clearTimeout(refreshTimer);
  });
}

function formatCount(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("ru-RU");
}

function fmtAgo(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "только что";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} дн. назад`;
  return date.toLocaleDateString("ru-RU");
}

function ensureStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = STYLE;
  document.head.append(style);
}
