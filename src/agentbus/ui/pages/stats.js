import { errorNote, fmtDuration, fmtTokens, h, isAbort, registerPage } from "../app.js";

const STYLE_ID = "t7-stats-style";
const GROUPS = [
  ["mailbox", "ящик"],
  ["repo", "репозиторий"],
  ["model", "модель"],
];
const ACCEPT_TITLE =
  "Треды, где первый результат принят (state_note accept*), и их доля от тикетов группы";
const COLUMNS = [
  { label: "Группа" },
  { label: "Тикетов", num: true, title: "Сколько тредов с задачей в группе" },
  { label: "Результатов", num: true, title: "Сообщений kind=result" },
  { label: "Принято с первого ревью", num: true, title: ACCEPT_TITLE },
  { label: "Rework", num: true, title: "Вердикты с решением rework" },
  { label: "Таймаутов", num: true, title: "Результаты с meta.timeout=true" },
  { label: "Сбоев", num: true, title: "Треды, где есть сообщение в состоянии failed" },
  {
    label: "Очередь, медиана",
    num: true,
    title: "Медиана от создания задачи до её lease, секунды",
  },
  {
    label: "Исполнение, медиана",
    num: true,
    title: "Медиана от lease родителя до создания результата, секунды",
  },
  { label: "Токены in / out", num: true, title: "Сумма usage.tokens.input / output по результатам" },
];

const STYLE = `
.t7-groups { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.8rem; }
.t7-group {
  font: inherit;
  font-size: 0.85rem;
  padding: 0.3rem 0.7rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--panel);
  color: var(--muted);
  cursor: pointer;
}
.t7-group[aria-pressed="true"] {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--panel-2);
}
.t7-bars { display: grid; gap: 0.9rem; margin-bottom: 1.1rem; }
.t7-bar-block {
  padding: 0.6rem 0.8rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
}
.t7-bar-row {
  display: grid;
  grid-template-columns: minmax(6rem, 12rem) minmax(0, 1fr) 4.5rem;
  gap: 0.5rem;
  align-items: center;
  padding: 0.15rem 0;
  font-size: 0.82rem;
}
.t7-bar-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.t7-bar-track {
  height: 0.5rem;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 999px;
  overflow: hidden;
}
.t7-bar-fill { display: block; height: 100%; background: var(--accent); }
.t7-bar-block + .t7-bar-block .t7-bar-fill { background: var(--warn); }
.t7-bar-value {
  text-align: right;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
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
.t7-table tbody tr:hover { background: var(--panel-2); }
.t7-num { text-align: right; font-variant-numeric: tabular-nums; }
.t7-bars[hidden],
.t7-table-wrap[hidden] { display: none; }
`;

registerPage("stats", { title: "Статистика", render });

function readGroup(query) {
  const group = query ? query.group : "";
  return GROUPS.some(([value]) => value === group) ? group : "mailbox";
}

function render(root, params, ctx) {
  ensureStyles();

  let group = readGroup(params.query);
  let requestSeq = 0;

  const status = h("div", { class: "status muted" });
  const switcher = h("div", { class: "t7-groups", role: "group", "aria-label": "Группировка" });
  const buttons = new Map();
  for (const [value, label] of GROUPS) {
    const button = h("button", { type: "button", class: "t7-group" }, label);
    button.addEventListener("click", () => selectGroup(value));
    buttons.set(value, button);
    switcher.append(button);
  }

  const empty = h("p", { class: "muted" }, "данных пока нет");
  const acceptBars = h("div");
  const runBars = h("div");
  const bars = h(
    "div",
    { class: "t7-bars" },
    h(
      "section",
      { class: "t7-bar-block" },
      h("h2", { title: ACCEPT_TITLE }, "принято с первого ревью, %"),
      acceptBars,
    ),
    h(
      "section",
      { class: "t7-bar-block" },
      h(
        "h2",
        { title: "Доля от максимальной медианы исполнения в таблице" },
        "медиана исполнения",
      ),
      runBars,
    ),
  );

  const body = h("tbody");
  const table = h(
    "div",
    { class: "t7-table-wrap" },
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

  empty.hidden = true;
  bars.hidden = true;
  table.hidden = true;
  root.replaceChildren(h("h1", {}, "Статистика"), status, switcher, bars, empty, table);

  syncButtons();

  async function load() {
    const seq = ++requestSeq;
    status.textContent = "загрузка…";
    try {
      const data = await ctx.api(`/v1/stats?group=${encodeURIComponent(group)}`);
      if (seq !== requestSeq) return;
      renderRows(Array.isArray(data.rows) ? data.rows : []);
      status.replaceChildren();
    } catch (error) {
      if (isAbort(error) || seq !== requestSeq) return;
      status.replaceChildren(errorNote(error));
    }
  }

  function selectGroup(value) {
    if (value === group) return;
    ctx.setQuery({ group: value });
  }

  // `ctx.setQuery` and Back/forward both land here, without a full re-render.
  function applyQuery(query) {
    const next = readGroup(query);
    if (next === group) return;
    group = next;
    syncButtons();
    load();
  }

  function syncButtons() {
    for (const [value, button] of buttons) {
      button.setAttribute("aria-pressed", String(value === group));
    }
  }

  function renderRows(rows) {
    body.replaceChildren(...rows.map(rowNode));
    renderBars(rows);
    empty.hidden = rows.length > 0;
    bars.hidden = rows.length === 0;
    table.hidden = rows.length === 0;
  }

  function rowNode(row) {
    return h(
      "tr",
      {},
      h("td", { class: "mono" }, row.key || "—"),
      countCell(row.tickets),
      countCell(row.results),
      h("td", { class: "t7-num" }, acceptedText(row)),
      countCell(row.rework_verdicts),
      countCell(row.timeouts),
      countCell(row.failed),
      h("td", { class: "t7-num" }, duration(row.median_queue_seconds)),
      h("td", { class: "t7-num" }, duration(row.median_run_seconds)),
      h(
        "td",
        { class: "t7-num mono" },
        `${tokens(row.tokens_input)} / ${tokens(row.tokens_output)}`,
      ),
    );
  }

  function renderBars(rows) {
    acceptBars.replaceChildren(
      ...rows.map((row) => {
        const share = percent(row.accepted_first_review, row.tickets);
        return barRow(row.key, share, share === null ? "—" : `${share} %`);
      }),
    );
    const medians = rows
      .map((row) => Number(row.median_run_seconds))
      .filter((value) => Number.isFinite(value));
    const max = medians.length ? Math.max(...medians) : 0;
    runBars.replaceChildren(
      ...rows.map((row) => {
        const value = row.median_run_seconds;
        const share =
          value === null || value === undefined || max <= 0
            ? null
            : Math.round((Number(value) / max) * 100);
        return barRow(row.key, share, duration(value));
      }),
    );
  }

  load();

  ctx.onQuery(applyQuery);

  ctx.onUnmount(() => {
    requestSeq += 1;
  });
}

function barRow(label, share, valueText) {
  const fill = h("span", { class: "t7-bar-fill" });
  if (share !== null) fill.style.width = `${Math.max(0, Math.min(100, share))}%`;
  return h(
    "div",
    { class: "t7-bar-row" },
    h("span", { class: "t7-bar-label mono", title: label || null }, label || "—"),
    h("span", { class: "t7-bar-track" }, share === null ? null : fill),
    h("span", { class: "t7-bar-value" }, valueText),
  );
}

function countCell(value) {
  return h("td", { class: "t7-num" }, formatCount(value));
}

function acceptedText(row) {
  const count = formatCount(row.accepted_first_review);
  const share = percent(row.accepted_first_review, row.tickets);
  return share === null ? count : `${count} · ${share} %`;
}

function percent(part, total) {
  const accepted = Number(part);
  const tickets = Number(total);
  if (!Number.isFinite(accepted) || !Number.isFinite(tickets) || tickets <= 0) return null;
  return Math.round((accepted / tickets) * 100);
}

function duration(value) {
  if (value === null || value === undefined) return "—";
  return fmtDuration(value);
}

function tokens(value) {
  if (value === null || value === undefined) return "—";
  return fmtTokens(value);
}

function formatCount(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("ru-RU");
}

function ensureStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = STYLE;
  document.head.append(style);
}
