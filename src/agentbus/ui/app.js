/**
 * agentbus dashboard: hash router, live event hub, shared helpers.
 *
 * Plain ES modules with no build step. Pages live in `pages/`, call
 * `registerPage(name, {title, render})` when imported and are all linked from
 * `index.html`. `render(root, params, ctx)` fills the container it gets.
 */

const POLL_MS = 3000;
const SEED_LIMIT = 500;
const SEED_MAX_PAGES = 50;

const pages = new Map();

const routes = [
  ["now", /^\/now\/?$/],
  ["thread", /^\/thread\/([^/]+)\/?$/],
  ["thread", /^\/thread\/?$/],
  ["mailboxes", /^\/mailboxes\/?$/],
  ["history", /^\/history\/?$/],
  ["stats", /^\/stats\/?$/],
];

let rootEl = null;
let abortController = null;
let unmountFns = [];
let unsubscribeFns = [];
let authScreenShown = false;

class AuthError extends Error {}

// --- public API --------------------------------------------------------------

export function registerPage(name, page) {
  pages.set(name, page);
}

/**
 * Tiny DOM builder: `h("div", {class: "x"}, "text", child)`. Text is always
 * inserted as a text node, never parsed as HTML.
 */
export function h(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2), value);
    } else if (value === true) element.setAttribute(key, "");
    else element.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

export function errorNote(error) {
  const message = error && error.message ? error.message : String(error);
  return h("p", { class: "error-note" }, `ошибка: ${message}`);
}

export function isAbort(error) {
  return Boolean(error) && error.name === "AbortError";
}

export function fmtTime(value) {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const time = date.toLocaleTimeString("ru-RU", { hour12: false });
  if (date.toDateString() === new Date().toDateString()) return time;
  return `${date.toLocaleDateString("ru-RU")} ${time}`;
}

export function fmtDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return "—";
  const rounded = Math.round(total);
  if (rounded < 60) return `${rounded} с`;
  const minutes = Math.floor(rounded / 60);
  if (minutes < 60) return `${minutes} мин ${rounded % 60} с`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч ${minutes % 60} мин`;
  return `${Math.floor(hours / 24)} д ${hours % 24} ч`;
}

export function fmtTokens(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return Math.round(number).toLocaleString("ru-RU");
}

// --- routing ------------------------------------------------------------------

function start() {
  rootEl = document.getElementById("app");
  if (!rootEl) return;
  window.addEventListener("hashchange", show);
  show();
}

// Module scripts run before DOMContentLoaded while `readyState` is already
// "interactive", so the hint must be "wait for DOMContentLoaded" in every case
// that is still parsing. Otherwise `app.js` boots before `pages/*.js` register.
if (document.readyState === "complete") {
  start();
} else {
  document.addEventListener("DOMContentLoaded", start);
}

function show() {
  if (!rootEl) return;
  teardown();
  authScreenShown = false;
  const match = matchRoute(location.hash);
  markNav(match);
  if (!match) {
    rootEl.replaceChildren(
      h(
        "section",
        { class: "page" },
        h("h1", {}, "Не найдено"),
        h("p", { class: "muted" }, `Неизвестный маршрут: ${location.hash || "(пусто)"}`),
        h("p", {}, h("a", { href: "#/now" }, "На «Сейчас»")),
      ),
    );
    return;
  }
  const page = pages.get(match.name);
  const view = h("section", { class: `page page-${match.name}` });
  rootEl.replaceChildren(view);
  document.title = page && page.title ? `${page.title} — agentbus` : "agentbus";
  if (!page) {
    view.append(h("p", { class: "muted" }, `Страница «${match.name}» не зарегистрирована`));
    return;
  }
  const controller = new AbortController();
  abortController = controller;
  try {
    const result = page.render(view, match.params, context(controller));
    if (result && typeof result.then === "function") {
      result.catch((error) => {
        if (!isAbort(error)) view.append(errorNote(error));
      });
    }
  } catch (error) {
    view.append(errorNote(error));
  }
}

function matchRoute(hash) {
  const path = (hash || "").replace(/^#/, "") || "/now";
  for (const [name, pattern] of routes) {
    const found = path.match(pattern);
    if (found) return { name, params: { id: decodePart(found[1] || "") } };
  }
  return null;
}

function decodePart(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function markNav(match) {
  for (const link of document.querySelectorAll("nav a[data-page]")) {
    if (match && link.dataset.page === match.name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function teardown() {
  for (const fn of unmountFns.splice(0)) {
    try {
      fn();
    } catch (error) {
      console.error(error);
    }
  }
  for (const off of unsubscribeFns.splice(0)) {
    try {
      off();
    } catch (error) {
      console.error(error);
    }
  }
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
}

function context(controller) {
  return {
    api: (path, options = {}) => request(path, { ...options, signal: controller.signal }),
    link: (path) => `#${path}`,
    onLive: (listener) => {
      const off = live.subscribe(listener);
      unsubscribeFns.push(off);
      return off;
    },
    onUnmount: (fn) => {
      unmountFns.push(fn);
    },
    fmtDuration,
    fmtTime,
    fmtTokens,
  };
}

// --- http ---------------------------------------------------------------------

async function request(path, { signal, ...options } = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    ...options,
    signal: signal || null,
  });
  if (response.status === 401) {
    showLogin();
    throw new AuthError("нужен вход по токену");
  }
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText} — ${path}`);
  }
  return response.json();
}

function showLogin() {
  if (!rootEl || authScreenShown) return;
  authScreenShown = true;
  teardown();
  const input = h("input", {
    type: "password",
    name: "token",
    placeholder: "токен agentbus",
    autocomplete: "off",
    required: true,
  });
  const form = h(
    "form",
    { class: "card login" },
    h("h1", {}, "Вход"),
    h(
      "p",
      { class: "muted" },
      "Введите токен agentbus. Он уходит только в /ui/login и не сохраняется в JS.",
    ),
    input,
    h("button", { type: "submit" }, "Войти"),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const token = input.value.trim();
    if (token) location.href = `/ui/login?token=${encodeURIComponent(token)}`;
  });
  rootEl.replaceChildren(form);
  input.focus();
}

// --- live events --------------------------------------------------------------

/**
 * Live hub. With `/v1/stream` (SSE) available it forwards every message; when the
 * stream cannot be opened it polls `/v1/events?after=<last id>` every 3 s. Before
 * the first poll it silently walks the event log once so a fresh page is not
 * flooded with history.
 */
const live = {
  listeners: new Set(),
  mode: "",
  stream: null,
  timer: null,
  cursor: null,

  subscribe(listener) {
    this.listeners.add(listener);
    if (!this.stream && !this.timer) this.open();
    return () => {
      this.listeners.delete(listener);
      if (!this.listeners.size) this.shutdown();
    };
  },

  open() {
    if (typeof EventSource === "undefined") {
      this.startPolling();
      return;
    }
    let stream = null;
    try {
      stream = new EventSource("/v1/stream");
    } catch {
      this.startPolling();
      return;
    }
    this.stream = stream;
    stream.onopen = () => this.setMode("live");
    // `GET /v1/stream` sends named events (`hello`, `agentbus`); unnamed events are
    // handled too so the hub also works with a plain SSE endpoint.
    stream.onmessage = (message) => this.receive(message.data);
    stream.addEventListener("agentbus", (message) => this.receive(message.data));
    stream.addEventListener("hello", () => this.setMode("live"));
    stream.onerror = () => {
      this.closeStream();
      this.startPolling();
    };
  },

  receive(raw) {
    this.setMode("live");
    const payload = parsePayload(raw);
    if (!payload) return;
    // The stream names the cursor `event_id`; polling uses the row shape with `id`.
    if (payload.id === undefined && payload.event_id !== undefined) payload.id = payload.event_id;
    this.dispatch(payload);
  },

  startPolling() {
    if (this.timer) return;
    this.setMode("poll");
    this.timer = setInterval(() => this.tick(), POLL_MS);
    this.tick();
  },

  async tick() {
    if (!this.listeners.size) return;
    try {
      if (this.cursor === null) this.cursor = await this.seedCursor();
      const data = await request(`/v1/events?after=${this.cursor}&limit=100`);
      for (const event of Array.isArray(data.events) ? data.events : []) {
        this.cursor = Math.max(this.cursor, Number(event.id) || 0);
        this.dispatch(event);
      }
      this.setMode("poll");
    } catch (error) {
      if (error instanceof AuthError) {
        this.shutdown();
        return;
      }
      this.setMode("offline");
    }
  },

  async seedCursor() {
    let cursor = 0;
    for (let page = 0; page < SEED_MAX_PAGES; page += 1) {
      const data = await request(`/v1/events?after=${cursor}&limit=${SEED_LIMIT}`);
      const events = Array.isArray(data.events) ? data.events : [];
      if (!events.length) break;
      cursor = Number(events[events.length - 1].id) || cursor;
      if (events.length < SEED_LIMIT) break;
    }
    return cursor;
  },

  dispatch(payload) {
    for (const listener of [...this.listeners]) {
      try {
        listener(payload);
      } catch (error) {
        console.error(error);
      }
    }
  },

  setMode(mode) {
    if (this.mode === mode) return;
    this.mode = mode;
    const indicator = document.getElementById("conn");
    if (!indicator) return;
    indicator.dataset.mode = mode;
    indicator.textContent = { live: "live", poll: "опрос", offline: "нет связи" }[mode] || mode;
  },

  closeStream() {
    if (!this.stream) return;
    this.stream.close();
    this.stream = null;
  },

  shutdown() {
    this.closeStream();
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.cursor = null;
  },
};

function parsePayload(raw) {
  if (typeof raw !== "string" || !raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : { kind: "raw", data: parsed };
  } catch {
    return { kind: "raw", data: raw };
  }
}
