// Cockpit UI extension points (exposed as window.NebiUI by main.js) and the loader for extension UI modules.
//
// Extensions declare ES modules on the server (ServerExtensionContext.add_ui_module); the page fetches
// /api/extensions and imports each listed module after the core UI is up. A module uses:
//
//   NebiUI.registerTab({ id, title, icon, html, onShow })
//       Adds a side-panel tab. `icon` is a sprite id from index.html (e.g. "map", "target", "gear") or SVG markup;
//       `html` (string) or `element` fills the pane (its element id is `tab-<id>`); `onShow()` runs on every open.
//   NebiUI.registerView({ id, label, element, fullStage, onShow, onHide })
//       Adds a video-stage view to the Camera switch (key M cycles). `element` is appended to #video-stage; the
//       stage gets the class `view-<id>` (plus `view-alt` when `fullStage`) while the view is selected, so the
//       extension's CSS decides how its layer shows. `onShow()` / `onHide()` run on switches.
//   NebiUI.selectView(id), NebiUI.currentView(), NebiUI.selectTab(id), NebiUI.toast(msg, kind), NebiUI.isTyping()
//
// A module that fails to load is reported with console.warn and a toast; the cockpit keeps working.

import { $, $$, toast } from "./core.js";
import { onTabShow } from "./tabs.js";

const LS_VIEW = "nebi.view";
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// ---------------------------------------------------------------- tabs

export function registerTab({ id, title, icon = "gear", html = "", element = null, onShow = null }) {
  if (!id || $(`tab-${id}`)) throw new Error(`tab ${id} already exists`);
  const tabs = $("domain-tabs");
  const btn = document.createElement("button");
  btn.className = "tab";
  btn.dataset.tab = id;
  const svg = String(icon).trim().startsWith("<") ? icon : `<svg class="i"><use href="#i-${icon}"/></svg>`;
  btn.innerHTML = `${svg}<span></span>`;
  btn.querySelector("span").textContent = title || id;
  const system = tabs.querySelector('.tab[data-tab="system"]');   // System stays last
  tabs.insertBefore(btn, system);
  const pane = element || document.createElement("div");
  pane.classList.add("pane");
  pane.id = `tab-${id}`;
  if (!element) pane.innerHTML = html;
  const panes = document.querySelector(".side-panel .panes");
  panes.insertBefore(pane, $("tab-system"));
  if (onShow) onTabShow(id, onShow);
  return pane;
}

// ---------------------------------------------------------------- video-stage views

const views = new Map([["camera", { id: "camera", label: "Camera" }]]);
let current = "camera";

export function currentView() { return current; }

export function selectView(id) {
  if (!views.has(id)) id = "camera";
  const stage = $("video-stage");
  const prev = views.get(current);
  if (prev && prev.id !== id) { try { prev.onHide?.(); } catch (e) { console.warn(e); } }
  for (const v of views.keys()) stage.classList.toggle(`view-${v}`, v === id && v !== "camera");
  stage.classList.toggle("view-alt", !!views.get(id).fullStage);
  current = id;
  store.set(LS_VIEW, id);
  $$("#view-switch button").forEach((b) => b.classList.toggle("active", b.dataset.view === id));
  try { views.get(id).onShow?.(); } catch (e) { console.warn(e); }
  window.dispatchEvent(new CustomEvent("nebi:view", { detail: { view: id } }));
}

export function registerView({ id, label, element = null, fullStage = false, onShow = null, onHide = null }) {
  if (!id || views.has(id)) throw new Error(`view ${id} already exists`);
  views.set(id, { id, label: label || id, fullStage, onShow, onHide });
  if (element) $("video-stage").appendChild(element);
  const sw = $("view-switch");
  const b = document.createElement("button");
  b.dataset.view = id;
  b.textContent = label || id;
  sw.appendChild(b);
  sw.classList.remove("hidden");
  if (store.get(LS_VIEW) === id) selectView(id);   // restore the last view once its extension is back
}

$("view-switch")?.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (b) selectView(b.dataset.view);
});

window.addEventListener("keydown", (e) => {
  if (e.repeat || e.metaKey || e.ctrlKey || e.altKey || views.size < 2 || e.key.toLowerCase() !== "m") return;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  if (document.querySelector(".modal-backdrop:not(.hidden)")) return;
  const ids = [...views.keys()];
  selectView(ids[(ids.indexOf(current) + 1) % ids.length]);
});

// ---------------------------------------------------------------- loader

function addStyle(href) {
  if (document.querySelector(`link[data-ext-style="${CSS.escape(href)}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  link.dataset.extStyle = href;
  document.head.appendChild(link);
}

export async function loadExtensions() {
  let list = [];
  try {
    const r = await fetch("/api/extensions", { cache: "no-store" });
    if (!r.ok) return [];
    list = (await r.json()).extensions || [];
  } catch {
    return [];   // older server: no extension support
  }
  for (const ext of list) {
    if (!ext.ok) { console.warn(`extension ${ext.name} failed:`, ext.errors); continue; }
    (ext.styles || []).forEach(addStyle);
    for (const url of ext.ui_modules || []) {
      try {
        await import(url);
      } catch (e) {
        console.warn(`extension ${ext.name}: UI module ${url} failed`, e);
        toast(`Extension ${ext.name}: UI failed to load`, "error");
      }
    }
  }
  return list;
}
