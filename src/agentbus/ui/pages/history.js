import { errorNote, h, isAbort, registerPage } from "../app.js";

const PAGE_SIZE = 50;
const DEBOUNCE_MS = 300;
const STYLE_ID = "t7-history-style";
const FILTER_KEYS = ["mailbox", "repo", "state", "q"];
const STATES = [
  ["", "все"],
  ["active", "в работе"],
  ["review", "ждут ревью"],
  ["done", "готово"],
  ["failed", "сбои"],
];
const COLUMNS = [
  { label: "Тикет" },
  { label: "Заголовок" },
  { label: "Репозиторий" },
  { label: "Ящик" },
  { label: "Попыток", num: true },
  { label: "Последний" },
  { label: "Модель" },
  { label: "Обновлён" },
];

const STYLE = `
.t7-filters { display: flex; flex-wrap: wrap; gap: 0.6rem 0.8rem; margin-bottom: 0.8rem; }
.t7-field { display: grid; gap: 0.2rem; font-size: 0.8rem; }
.t7-field select,
.t7-field input {
  font: inherit;
  padding: 0.3rem 0.45rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  color: var(--text);
}
.t7-field input[type="search"] { min-width: 14rem; }
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
.t7-title { max-width: 32rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.t7-title a { color: var(--text); }
.t7-title a:hover { color: var(--accent); }
.t7-badges { white-space: nowrap; }
.t7-footer { margin-top: 0.7rem; }
.t7-more {
  font: inherit;
  font-size: 0.85rem;
  padding: 0.35rem 0.8rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  color: var(--accent);
  cursor: pointer;
}
.t7-table-wrap[hidden],
.t7-more[hidden] { display: none; }
`;

registerPage("history", { title: "История", render });

function readFilters(query) {
  const filters = {};
  for (const key of FILTER_KEYS) {
    const value = query ? query[key] : "";
    filters[key] = typeof value === "string" ? value : "";
  }
  if (!STATES.some(([value]) => value === filters.state)) filters.state = "";
  return filters;
}

function render(root, params, ctx) {
  ensureStyles();

  const filters = readFilters(params.query);
  const knownRepos = new Set(filters.repo ? [filters.repo] : []);
  const state = { rows: [], seq: 0 };

  const mailboxSelect = h("select", { name: "mailbox" });
  const repoSelect = h("select", { name: "repo" });
  const stateSelect = h(
    "select",
    { name: "state" },
    STATES.map(([value, label]) => h("option", { value }, label)),
  );
  stateSelect.value = filters.state;
  const search = h("input", {
    type: "search",
    name: "q",
    placeholder: "поиск по тексту",
    value: filters.q,
    autocomplete: "off",
  });

  const form = h(
    "form",
    { class: "t7-filters" },
    field("ящик", mailboxSelect),
    field("репозиторий", repoSelect),
    field("состояние", stateSelect),
    field("поиск", search),
  );
  form.addEventListener("submit", (event) => event.preventDefault());

  const status = h("div", { class: "status muted" });
  const empty = h("p", { class: "muted" }, "тредов не найдено");
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
            h("th", { class: column.num ? "t7-num" : null }, column.label),
          ),
        ),
      ),
      body,
    ),
  );
  const more = h("button", { type: "button", class: "t7-more" }, "показать ещё");
  const footer = h("div", { class: "t7-footer" }, more);

  empty.hidden = true;
  table.hidden = true;
  more.hidden = true;
  root.replaceChildren(h("h1", {}, "История"), status, form, empty, table, footer);

  let searchTimer = null;

  async function load(reset) {
    const seq = ++state.seq;
    if (reset) status.textContent = "загрузка…";
    try {
      const data = await ctx.api(`/v1/threads?${requestQuery(reset ? 0 : state.rows.length)}`);
      if (seq !== state.seq) return;
      const threads = Array.isArray(data.threads) ? data.threads : [];
      if (reset) {
        state.rows = [];
        body.replaceChildren();
      }
      state.rows.push(...threads);
      body.append(...threads.map((row) => rowNode(row)));
      collectRepos(threads);
      status.replaceChildren();
      empty.hidden = state.rows.length > 0;
      table.hidden = state.rows.length === 0;
      more.hidden = threads.length < PAGE_SIZE;
    } catch (error) {
      if (isAbort(error) || seq !== state.seq) return;
      status.replaceChildren(errorNote(error));
    }
  }

  function requestQuery(offset) {
    const params = new URLSearchParams();
    for (const key of FILTER_KEYS) {
      if (filters[key]) params.set(key, filters[key]);
    }
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));
    return params.toString();
  }

  function collectRepos(threads) {
    let changed = false;
    for (const row of threads) {
      if (!row.repo || knownRepos.has(row.repo)) continue;
      knownRepos.add(row.repo);
      changed = true;
    }
    if (changed) fillSelect(repoSelect, [...knownRepos], filters.repo, "все репозитории");
  }

  function rowNode(row) {
    const href = ctx.link(`/thread/${encodeURIComponent(row.id)}`);
    const node = h(
      "tr",
      { tabindex: "0" },
      h("td", { class: "mono" }, h("a", { href }, row.ticket || "—")),
      h(
        "td",
        { class: "t7-title" },
        h("a", { href, title: row.title || null }, row.title || "—"),
      ),
      h("td", { class: "mono", title: row.repo || null }, basename(row.repo) || "—"),
      h("td", { class: "mono" }, row.created_by || "—"),
      h("td", { class: "t7-num" }, formatCount(row.attempts)),
      h(
        "td",
        { class: "t7-badges" },
        badge("kind", row.last_kind),
        " ",
        badge("state", row.last_state),
      ),
      h("td", { class: "mono" }, row.model || "—"),
      h("td", { class: "muted" }, ctx.fmtTime(row.updated_at)),
    );
    bindOpen(node, href);
    return node;
  }

  function applyFilters() {
    filters.mailbox = mailboxSelect.value;
    filters.repo = repoSelect.value;
    filters.state = stateSelect.value;
    filters.q = search.value.trim();
    ctx.setQuery(filters);
  }

  // `ctx.setQuery` and Back/forward both land here. The page is not re-rendered,
  // so the search field keeps focus; only the data is reloaded.
  function applyQuery(query) {
    if (searchTimer) {
      clearTimeout(searchTimer);
      searchTimer = null;
    }
    Object.assign(filters, readFilters(query));
    syncControls();
    load(true);
  }

  function syncControls() {
    ensureOption(mailboxSelect, filters.mailbox);
    ensureOption(repoSelect, filters.repo);
    mailboxSelect.value = filters.mailbox;
    repoSelect.value = filters.repo;
    stateSelect.value = filters.state;
    if (search.value !== filters.q) search.value = filters.q;
  }

  function onSearch() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(applyFilters, DEBOUNCE_MS);
  }

  async function loadMailboxes() {
    try {
      const data = await ctx.api("/v1/mailboxes");
      const names = (Array.isArray(data.mailboxes) ? data.mailboxes : []).map(
        (row) => row.mailbox,
      );
      fillSelect(mailboxSelect, names, filters.mailbox, "все ящики");
    } catch (error) {
      if (isAbort(error)) return;
      fillSelect(mailboxSelect, [], filters.mailbox, "все ящики");
    }
  }

  mailboxSelect.addEventListener("change", applyFilters);
  repoSelect.addEventListener("change", applyFilters);
  stateSelect.addEventListener("change", applyFilters);
  search.addEventListener("input", onSearch);
  more.addEventListener("click", () => load(false));
  ctx.onQuery(applyQuery);

  fillSelect(mailboxSelect, [], filters.mailbox, "все ящики");
  fillSelect(repoSelect, [...knownRepos], filters.repo, "все репозитории");
  loadMailboxes();
  load(true);

  ctx.onUnmount(() => {
    if (searchTimer) clearTimeout(searchTimer);
  });
}

function field(label, control) {
  return h("label", { class: "t7-field" }, h("span", { class: "muted" }, label), control);
}

function fillSelect(select, values, selected, allLabel) {
  const options = [...new Set(values.filter(Boolean))].sort((left, right) =>
    left.localeCompare(right, "ru"),
  );
  if (selected && !options.includes(selected)) options.push(selected);
  select.replaceChildren(
    h("option", { value: "" }, allLabel),
    ...options.map((value) => h("option", { value }, value)),
  );
  select.value = selected || "";
}

// A value restored from the hash by Back/forward may not be in the list yet.
function ensureOption(select, value) {
  if (!value) return;
  if ([...select.options].some((option) => option.value === value)) return;
  select.append(h("option", { value }, value));
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

function badge(prefix, value) {
  if (!value) return h("span", { class: "muted" }, "—");
  return h("span", { class: `badge ${prefix}-${value}` }, value);
}

function basename(repo) {
  if (!repo) return "";
  const parts = String(repo).split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(repo);
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
