import { $, $$, on, state } from "./core.js";
import { refreshMediaGallery } from "./media.js";
import { renderStatusGrid } from "./telemetry.js";

// Domain tabs.

// ---------------------------------------------------------------- tabs & modals

const showHooks = new Map();   // tab -> [callbacks] (extension tabs: NebiUI.registerTab({onShow}))

export function onTabShow(name, fn) {
  if (!showHooks.has(name)) showHooks.set(name, []);
  showHooks.get(name).push(fn);
}

export function selectTab(name) {
  if (!name || !document.querySelector(`#domain-tabs .tab[data-tab="${name}"]`)) return;
  state.activeTab = name;
  $$("#domain-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".pane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "media") refreshMediaGallery();
  if (name === "system" && state.telemetry) renderStatusGrid(state.telemetry);
  for (const fn of showHooks.get(name) || []) {
    try { fn(); } catch (e) { console.warn(e); }
  }
}
on("domain-tabs", "click", (e) => {
  const b = e.target.closest(".tab");
  if (b) selectTab(b.dataset.tab);
});
