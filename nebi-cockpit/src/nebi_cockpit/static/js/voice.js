import { $, api, esc, toast } from "./core.js";
import { onTabShow } from "./tabs.js";

// Speak: text-to-speech through Nebi's speaker (`/api/voice/*`). The robot speaker is shared with `nebi say` and the
// intercom: new speech interrupts the current one, Stop silences either, and speech is refused (409 SPEAKER_BUSY)
// while someone holds the intercom talk button.

const HISTORY_KEY = "nebi.voice.history";
const PREFS_KEY = "nebi.voice.prefs";
const HISTORY_MAX = 8;
const POLL_FAST_MS = 250;   // while speaking
const POLL_SLOW_MS = 2000;  // while the Sound tab is visible

const els = {
  card: $("voice-card"),
  text: $("voice-text"),
  say: $("btn-voice-say"),
  engine: $("voice-engine"),
  preset: $("voice-preset"),
  emotion: $("voice-emotion"),
  babble: $("btn-voice-babble"),
  stop: $("btn-voice-stop"),
  pill: $("voice-pill"),
  history: $("voice-history"),
  hint: $("voice-hint"),
};

let engines = [];
let speaking = false;
let pollTimer = null;

const load = (key, fallback) => {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (_) { return fallback; }
};
const save = (key, value) => {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode: keep in memory only */ }
};
let history = load(HISTORY_KEY, []);
const prefs = load(PREFS_KEY, {});

const option = (value, label, selected) =>
  `<option value="${esc(value)}"${selected ? " selected" : ""}>${esc(label)}</option>`;

function fillEngineDependent() {
  const eng = engines.find((e) => e.name === els.engine.value) || engines[0];
  if (!eng) return;
  const wantPreset = prefs.preset && eng.presets.includes(prefs.preset) ? prefs.preset : eng.default_preset;
  els.preset.innerHTML = eng.presets.map((p) => option(p, p, p === wantPreset)).join("");
  els.preset.classList.toggle("hidden", eng.presets.length < 2);
  const wantEmotion = prefs.emotion || "neutral";
  els.emotion.innerHTML = eng.emotions.map((e) => option(e, e, e === wantEmotion)).join("");
}

async function loadEngines() {
  try {
    const res = await api("/api/voice/engines");
    engines = res.engines || [];
    const want = engines.some((e) => e.name === prefs.engine) ? prefs.engine : res.default;
    els.engine.innerHTML = engines.map((e) => option(e.name, e.name, e.name === want)).join("");
    fillEngineDependent();
  } catch (e) {
    els.hint.textContent = `Voice unavailable: ${e.message}`;
  }
}

function savePrefs() {
  Object.assign(prefs, { engine: els.engine.value, preset: els.preset.value, emotion: els.emotion.value });
  save(PREFS_KEY, prefs);
}

// ---------------------------------------------------------------- history

function renderHistory() {
  els.history.innerHTML = history
    .map((t, i) => `<button class="chip voice-chip" data-i="${i}" title="Say again">${esc(t)}</button>`)
    .join("");
  els.history.classList.toggle("hidden", history.length === 0);
}

function remember(text) {
  history = [text, ...history.filter((t) => t !== text)].slice(0, HISTORY_MAX);
  save(HISTORY_KEY, history);
  renderHistory();
}

// ---------------------------------------------------------------- speaking state

function setSpeaking(on, label = "") {
  speaking = on;
  els.card.classList.toggle("speaking", on);
  els.pill.className = `ic-pill ${on ? "live" : "off"}`;
  els.pill.textContent = on ? "Speaking" : "Quiet";
  els.pill.title = on ? label : "";
  els.stop.disabled = !on;
}

function showStatus(st) {
  if (!st) return;
  const turn = st.turn;
  setSpeaking(Boolean(st.speaking), turn ? `${turn.owner}: ${turn.label}` : "");
  if (st.mic_held) {
    els.pill.className = "ic-pill talk";
    els.pill.textContent = "Mic in use";
    els.hint.textContent = "Someone is talking on the intercom; Nebi will speak once the talk button is released.";
  } else if (st.speaking && turn && turn.owner !== "cockpit") {
    els.hint.textContent = `Speaking for ${turn.owner}: “${turn.label}”`;
  } else if (!st.connected) {
    els.hint.textContent = "Connect to a robot to speak.";
  } else {
    els.hint.textContent = "";
  }
}

async function poll() {
  clearTimeout(pollTimer);
  pollTimer = null;
  const visible = !document.hidden && els.card.offsetParent !== null;
  if (!visible && !speaking) return;  // resumed by the next action or tab show
  try {
    showStatus(await api("/api/voice/status"));
  } catch (_) {
    /* transient: keep polling */
  }
  pollTimer = setTimeout(poll, speaking ? POLL_FAST_MS : POLL_SLOW_MS);
}

// ---------------------------------------------------------------- actions

async function speak({ text = "", babble = false } = {}) {
  if (!babble && !text.trim()) {
    els.text.focus();
    return toast("Type something for Nebi to say (or Babble)", "info");
  }
  savePrefs();
  const body = { text, babble, engine: els.engine.value || null, emotion: els.emotion.value || null,
                 preset: els.preset.value || null };
  try {
    const res = await api("/api/voice/say", "POST", body);
    if (!babble) remember(text.trim());
    setSpeaking(true, res.spoken || "babble");
    for (const w of res.warnings || []) toast(w, "info");
  } catch (e) {
    const err = e.nebiError;
    toast(err ? `${err.message}${err.hint ? " " + err.hint : ""}` : e.message, "error");
  }
  poll();
}

async function stop() {
  try {
    await api("/api/voice/stop", "POST", {});
  } catch (e) {
    toast(e.message, "error");
  }
  poll();
}

els.text.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) {
    e.preventDefault();
    const text = els.text.value;
    speak({ text }).then(() => { if (!e.shiftKey) els.text.select(); });
  }
});
els.say.addEventListener("click", () => speak({ text: els.text.value }));
els.babble.addEventListener("click", () => speak({ babble: true }));
els.stop.addEventListener("click", stop);
els.engine.addEventListener("change", () => { fillEngineDependent(); savePrefs(); });
els.preset.addEventListener("change", savePrefs);
els.emotion.addEventListener("change", savePrefs);
els.history.addEventListener("click", (e) => {
  const b = e.target.closest("[data-i]");
  if (!b) return;
  const text = history[Number(b.dataset.i)];
  els.text.value = text;
  speak({ text });
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
onTabShow("audio", () => setTimeout(poll, 0));

renderHistory();
setSpeaking(false);
loadEngines();
poll();
