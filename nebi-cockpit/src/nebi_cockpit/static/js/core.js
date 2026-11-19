// Shared cockpit state and helpers: DOM lookup, escaping, toasts, the REST client, control syncing,
// formatting and modals. Imports nothing from the feature modules (they import from here).

export const state = {
  catalog: null,
  telemetry: null,
  connection: null,
  dismissedErrorKey: "",
  wsControl: null,
  wsVideo: null,
  useMjpegFallback: false,
  stickLeft: { x: 0, y: 0, active: false },
  stickRight: { x: 0, y: 0, active: false },
  tilt: { pitch: 0, roll: 0, active: false },
  keys: new Set(),
  bodyHeight: 0.52,  // raw 16/31, what the official app sends at rest
  bodyRoll: 0.0,
  rollMode: false,
  recording: false,
  lastSentJoyJson: "",
  lastJoySentAt: 0,
  lastJoyNeutral: null,
  neutralBurst: 0,
  lastFrameAt: 0,
  followSubMode: 0,
  mediaFilter: "",
  mediaFiles: [],
  mediaIndex: -1,
  paramSpecs: [],
  paramValues: {},
  activeTab: "camera",
  lastEscAt: 0,
  padButtons: [],
  lastListRender: 0,
};

export const $ = (id) => document.getElementById(id);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const icon = (name, cls = "i i-sm") => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;

/** ws(s)://<this host><path> for the cockpit's WebSockets. */
export const wsUrl = (path) => `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`;

/** Robot album file URL (same-origin proxy). */
export const mediaContentUrl = (fileId) => `/api/media/files/${encodeURIComponent(fileId)}/content`;

/** Cross-module callbacks set by feature modules (keeps this module free of feature imports). */
export const hooks = {
  /** Called with the `connection` summary carried by REST error bodies. */
  onConnection: null,
};

// ---------------------------------------------------------------- toasts

const TOAST_MAX = 4;
const TOAST_FADE_MS = 2700;
const ERROR_TOAST_EXTRA_MS = 4000;
const TOAST_REMOVE_MS = 2950;

export function toast(msg, kind = "ok") {
  const box = $("toast-container");
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  while (box.children.length > TOAST_MAX) box.firstChild.remove();
  const extra = kind === "error" ? ERROR_TOAST_EXTRA_MS : 0; // errors carry hints: give time to read them
  setTimeout(() => el.classList.add("out"), TOAST_FADE_MS + extra);
  setTimeout(() => el.remove(), TOAST_REMOVE_MS + extra);
}

// ---------------------------------------------------------------- API

export async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let parsed = null;
    const txt = await resp.text();
    try {
      parsed = JSON.parse(txt);
    } catch (_) {
      // not JSON (proxy / plain-text error): fall back to the raw text below
    }
    const bErr = parsed && parsed.error ? parsed.error : null;
    const msg = bErr
      ? `[${bErr.code}] ${bErr.message}`
      : parsed && parsed.detail
      ? String(parsed.detail)
      : txt || `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.nebiError = bErr;
    if (parsed && parsed.connection) hooks.onConnection?.(parsed.connection);
    throw err;
  }
  return resp.json();
}

/** Run an action, toast success / failure. Returns the result or undefined on error. */
export async function run(fn, okMsg) {
  try {
    const res = await fn();
    if (okMsg) toast(typeof okMsg === "function" ? okMsg(res) : okMsg);
    return res;
  } catch (e) {
    const hint = e.nebiError?.hint ? ` — ${e.nebiError.hint}` : "";
    toast(`${e.message || String(e)}${hint}`, "error");
    return undefined;
  }
}

export const post = (path, body = {}, okMsg) => run(() => api(path, "POST", body), okMsg);
export const on = (id, evt, fn) => $(id).addEventListener(evt, fn);

// ---------------------------------------------------------------- helpers

/** Paint the filled part of a range track. */
function fillRange(el) {
  const min = Number(el.min || 0);
  const max = Number(el.max || 100);
  const pct = ((Number(el.value) - min) / (max - min)) * 100;
  el.style.setProperty("--pct", `${pct}%`);
}
$$('input[type="range"]').forEach((el) => {
  fillRange(el);
  el.addEventListener("input", () => fillRange(el));
});

// Controls the user recently touched aren't overwritten by telemetry sync.
const TOUCH_GRACE_MS = 2500;
export const touchedAt = new WeakMap();
document.addEventListener("input", (e) => touchedAt.set(e.target, Date.now()), true);
document.addEventListener("change", (e) => touchedAt.set(e.target, Date.now()), true);
export const canSync = (el) => el && document.activeElement !== el && Date.now() - (touchedAt.get(el) || 0) > TOUCH_GRACE_MS;

export function syncValue(el, value) {
  if (!canSync(el) || value === undefined || value === null) return;
  if (String(el.value) !== String(value)) {
    el.value = value;
    if (el.type === "range") fillRange(el);
    el.dispatchEvent(new CustomEvent("synced"));
  }
}
export function syncChecked(el, value) {
  if (!canSync(el) || value === undefined || value === null) return;
  el.checked = !!value;
}
export function setSegment(container, predicate) {
  $$("button", container).forEach((b) => b.classList.toggle("active", predicate(b)));
}

export function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
export const fmtDur = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
export const titleCase = (s) => String(s).replace(/[_.]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export function confirmDialog(title, msg, okLabel = "Confirm") {
  return new Promise((resolve) => {
    $("confirm-title").textContent = title;
    $("confirm-msg").textContent = msg;
    $("confirm-ok").textContent = okLabel;
    const modal = $("confirm-modal");
    modal.classList.remove("hidden");
    const done = (v) => {
      modal.classList.add("hidden");
      $("confirm-ok").onclick = null;
      $("confirm-cancel").onclick = null;
      resolve(v);
    };
    $("confirm-ok").onclick = () => done(true);
    $("confirm-cancel").onclick = () => done(false);
    modal._cancel = () => done(false);
  });
}

export const isTyping = () => ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName) &&
  !["range", "checkbox", "color"].includes(document.activeElement?.type);

export function openModal(id) {
  $(id).classList.remove("hidden");
}
export function closeModal(el) {
  if (!el) return;
  if (el.id === "confirm-modal" && el._cancel) return el._cancel();
  el.classList.add("hidden");
  if (el.id === "media-modal") $("media-modal-body").innerHTML = "";
}
$$(".modal-backdrop").forEach((bd) =>
  bd.addEventListener("mousedown", (e) => {
    if (e.target === bd) closeModal(bd);
  })
);
$$("[data-close]").forEach((b) => b.addEventListener("click", () => closeModal($(b.dataset.close))));
on("btn-help", "click", () => openModal("help-modal"));
