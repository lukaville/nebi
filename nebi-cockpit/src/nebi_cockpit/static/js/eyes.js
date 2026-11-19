import { $, api, esc, on, post, setSegment, titleCase, toast } from "./core.js";
import { onTabShow } from "./tabs.js";

// Eyes card (Sound tab): solid colour / expression on the robot's LED eyes for a time or until released.
// Releasing stops the frames; the robot's own (firmware) eyes come back by themselves. API: /api/eyes (routes_eyes.py).

const EMOJI = { happy: "😊", wink: "😉", heart: "❤️", cool: "😎", sleepy: "😴", dizzy: "😵", angry: "😠", music_pulse: "🎵" };
const POLL_MS = 1000;
const els = {
  card: $("eyes-card"), pill: $("eyes-pill"), preview: $("eyes-preview"), color: $("eyes-color"),
  swatches: $("eyes-swatches"), bright: $("eyes-brightness"), brightVal: $("eyes-brightness-val"),
  hold: $("eyes-hold"), exprs: $("eyes-expressions"), status: $("eyes-status"),
};
let loopSec = 1.44;
let pollTimer = null;
let last = { mode: "firmware" };

const holdSec = () => {
  const b = els.hold.querySelector("button.active");
  return b && b.dataset.hold ? Number(b.dataset.hold) : null; // null: until release
};
const brightness = () => Number(els.bright.value) / 100;
const hexToRgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

function render(st) {
  last = st || { mode: "firmware" };
  const mode = last.mode;
  const active = mode !== "firmware";
  els.pill.className = `ic-pill ${active ? "live" : "off"}`;
  els.pill.textContent = mode === "color" ? "Colour" : mode === "expression" ? "Expression" : "Robot's own";
  els.card.dataset.mode = mode;
  const k = last.brightness ?? 1;
  if (mode === "color") {
    const [r, g, b] = last.rgbw || hexToRgb(last.color || "#000000");
    els.preview.style.setProperty("--eye", `rgb(${r * k}, ${g * k}, ${b * k})`);
  } else {
    els.preview.style.removeProperty("--eye");
  }
  els.preview.dataset.emoji = mode === "expression" ? EMOJI[last.expression] || "✨" : "";
  els.exprs.querySelectorAll("[data-expr]").forEach((t) =>
    t.classList.toggle("active", mode === "expression" && t.dataset.expr === last.expression));
  const left = last.until_release ? "until released" : last.seconds_left != null ? `${Math.ceil(last.seconds_left)} s left` : "";
  const frames = last.frames_sent != null ? ` · ${last.frames_sent} frames sent` : "";
  if (mode === "color") {
    const what = last.color === "#000000" ? "Eyes dark" : `Showing ${last.color}`;
    els.status.textContent = `${what}${k < 1 ? ` at ${Math.round(k * 100)}%` : ""} · ${left}${frames}`;
  } else if (mode === "expression") {
    els.status.textContent = `Playing ${titleCase(last.expression)} · ${left}${frames}`;
  } else {
    els.status.textContent = `The robot is showing its own eyes.${last.last_error ? ` (stopped: ${last.last_error})` : ""}`;
  }
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = null;
  if (last.mode !== "firmware" && els.card.offsetParent !== null) pollTimer = setTimeout(refresh, POLL_MS);
}

async function refresh() {
  try {
    const res = await api("/api/eyes");
    render(res.status);
    return res;
  } catch (_) {
    render({ mode: "firmware" });
    return null;
  }
}

async function showColor(hex = els.color.value) {
  const res = await post("/api/eyes/color", { hex, hold_sec: holdSec(), brightness: brightness() });
  if (res) render(res);
}

async function playExpression(name) {
  const hold = holdSec();
  const loops = hold == null ? 0 : Math.max(1, Math.round(hold / loopSec));
  const res = await post("/api/eyes/expression", { name, loops, brightness: brightness() });
  if (res) render(res);
}

async function init() {
  const res = await refresh();
  if (!res) return setTimeout(init, 3000); // server restarting: try again
  loopSec = res.expressions[0]?.loop_s || loopSec;
  els.swatches.innerHTML = res.presets
    .map((p) => `<button style="--c:${esc(p.hex)}" data-hex="${esc(p.hex)}" title="${esc(p.name)}" aria-label="${esc(p.name)}"></button>`)
    .join("");
  els.exprs.innerHTML = res.expressions
    .map((e) => `<button class="tile" data-expr="${esc(e.name)}" title="${esc(e.description)}"><span class="tile-emoji">${EMOJI[e.name] || "✨"}</span>${esc(titleCase(e.name))}</button>`)
    .join("");
  render(res.status);
}

on("eyes-swatches", "click", (e) => {
  const b = e.target.closest("[data-hex]");
  if (!b) return;
  els.color.value = b.dataset.hex;
  showColor(b.dataset.hex);
});
on("eyes-color", "change", () => showColor());
on("btn-eyes-show", "click", () => showColor());
on("eyes-expressions", "click", (e) => {
  const b = e.target.closest("[data-expr]");
  if (b) playExpression(b.dataset.expr);
});
on("eyes-hold", "click", (e) => {
  const b = e.target.closest("button");
  if (b) setSegment(els.hold, (x) => x === b);
});
on("eyes-brightness", "input", () => { els.brightVal.textContent = `${els.bright.value}%`; });
on("eyes-brightness", "change", () => {
  if (last.mode === "color") showColor(last.color);  // re-apply at the new brightness
});
on("btn-eyes-release", "click", async () => {
  const res = await post("/api/eyes/release", {}, "Eyes released: the robot shows its own eyes again");
  if (res) render(res);
});
onTabShow("audio", refresh);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

init().catch((e) => toast(`Eyes: ${e.message || e}`, "error"));
