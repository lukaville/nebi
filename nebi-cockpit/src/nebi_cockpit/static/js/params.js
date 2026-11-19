import { $, $$, api, esc, on, post, state, titleCase, toast } from "./core.js";

// Robot parameter editor (System tab).

// Params
export async function loadParamsForm() {
  try {
    const res = await api("/api/system/params");
    state.paramValues = res.values || {};
  } catch (_) {
    state.paramValues = {};
  }
  renderParams(state.paramValues);
}

function renderParams(values) {
  const groups = {};
  for (const p of state.paramSpecs) (groups[p.group] ||= []).push(p);
  $("params-form").innerHTML =
    Object.entries(groups)
      .map(([g, ps]) => {
        const rows = ps
          .map((p) => {
            const val = values[p.key] ?? p.default;
            const unit = p.unit ? ` (${esc(p.unit)})` : "";
            return `<div class="param-row">
              <div class="param-name">${esc(p.label)}${unit}<span class="param-key">${esc(p.key)} · default ${esc(p.default)}</span></div>
              <input type="number" step="${p.integer ? 1 : 0.1}" class="input" data-pkey="${esc(p.key)}" data-orig="${esc(val)}" value="${esc(val)}" />
            </div>`;
          })
          .join("");
        return `<div class="param-group-title">${esc(titleCase(g.replace(/^md_/, "")))}</div>${rows}`;
      })
      .join("") || `<div class="empty">No tunable parameters.</div>`;
}

on("params-form", "input", (e) => {
  const inp = e.target.closest("[data-pkey]");
  if (inp) inp.classList.toggle("changed", Number(inp.value) !== Number(inp.dataset.orig));
});
on("btn-params-reload", "click", async () => {
  await loadParamsForm();
  toast("Parameters reloaded", "info");
});
on("btn-params-defaults", "click", () => {
  $$("#params-form [data-pkey]").forEach((inp) => {
    const spec = state.paramSpecs.find((p) => p.key === inp.dataset.pkey);
    if (spec) {
      inp.value = spec.default;
      inp.classList.toggle("changed", Number(inp.value) !== Number(inp.dataset.orig));
    }
  });
  toast("Defaults filled in — save to apply", "info");
});
on("btn-save-params", "click", async () => {
  const values = {};
  $$("#params-form [data-pkey]").forEach((inp) => {
    if (Number(inp.value) !== Number(inp.dataset.orig)) values[inp.dataset.pkey] = Number(inp.value);
  });
  if (!Object.keys(values).length) return toast("No parameter changes to save", "info");
  const res = await post("/api/system/params", { values }, `Saved ${Object.keys(values).length} parameter(s)`);
  if (res) await loadParamsForm();
});
