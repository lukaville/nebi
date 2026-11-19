import { toggleFullscreen } from "./camera.js";
import { $, $$, api, closeModal, isTyping, on, openModal, post, run, setSegment, state, toast, touchedAt } from "./core.js";
import { stepMedia } from "./media.js";
import { bindPad } from "./pads.js";
import { selectTab } from "./tabs.js";

// Driving: virtual sticks, keyboard, gamepad -> /ws/control joystick messages (25 Hz), dead-man hygiene,
// global shortcuts, and the drive deck (posture, gear, height/roll).
//
// Safety contract with the server (ws_routes.JoystickDeadman): a held stick is re-sent every tick (and at
// least every JOY_KEEPALIVE_MS); a release sends neutral a few times (NEUTRAL_BURST). Server and motion
// layer both expire stick axes ~0.5 s after the last update.

const JOY_TICK_MS = 40; // 25 Hz
const JOY_KEEPALIVE_MS = 80;
const NEUTRAL_BURST = 3;
const GAMEPAD_DEADZONE = 0.08;
const ESC_DOUBLE_PRESS_MS = 600;
const SHORTCUT_JUMP_HEIGHT = 0.8;
const BUTTON_JUMP_HEIGHT = 0.85;
// Keyboard drive: [key, axis, value], applied in order (a later key wins when both are held).
const KEY_AXES = [
  ["KeyW", "ly", 0.85],
  ["KeyS", "ly", -0.85],
  ["KeyA", "lx", -0.75],
  ["KeyD", "lx", 0.75],
  ["ArrowLeft", "rx", -0.75],
  ["ArrowRight", "rx", 0.75],
  ["ArrowUp", "ry", 0.75],
  ["ArrowDown", "ry", -0.75],
];
const DRIVE_KEYS = KEY_AXES.map(([k]) => k);
// Keys are digital (0 -> full in one tick), which the balance controller feels as a jolt, unlike the phone app's
// analog stick. Ramp keyboard axes like a thumb would: full deflection in ~0.3 s, back to zero in ~0.2 s.
// (Blur / hidden tab / dead-man still zero everything immediately.)
const KEY_RAMP_UP_PER_S = 3.0;
const KEY_RAMP_DOWN_PER_S = 4.5;
const KEY_SNAP = 0.02;
const keySmoothed = { lx: 0, ly: 0, rx: 0, ry: 0 };

function rampToward(cur, target, dtS) {
  const rate = Math.abs(target) > Math.abs(cur) && Math.sign(target) !== -Math.sign(cur) ? KEY_RAMP_UP_PER_S : KEY_RAMP_DOWN_PER_S;
  const step = rate * dtS;
  const next = target > cur ? Math.min(target, cur + step) : Math.max(target, cur - step);
  return Math.abs(next) < KEY_SNAP && target === 0 ? 0 : next;
}

function bindVirtualStick(stickEl, knobEl, target) {
  bindPad(
    stickEl,
    knobEl,
    (x, y) => {
      target.active = true;
      target.x = x;
      target.y = y;
    },
    () => {
      target.active = false;
      target.x = 0;
      target.y = 0;
    }
  );
}
bindVirtualStick($("stick-left"), $("knob-left"), state.stickLeft);
bindVirtualStick($("stick-right"), $("knob-right"), state.stickRight);

// number keys follow the tab bar's order (core tabs + extension tabs, System last)
const tabOrder = () => Array.from(document.querySelectorAll("#domain-tabs .tab"), (b) => b.dataset.tab);

window.addEventListener("keydown", (e) => {
  // Esc: close the top modal, otherwise double-press = E-STOP
  if (e.key === "Escape") {
    const open = $$(".modal-backdrop:not(.hidden)");
    if (open.length) {
      closeModal(open[open.length - 1]);
      return;
    }
    const now = Date.now();
    if (now - state.lastEscAt < ESC_DOUBLE_PRESS_MS) {
      triggerEstop();
      state.lastEscAt = 0;
    } else {
      state.lastEscAt = now;
      toast("Press Esc again to emergency stop", "info");
    }
    return;
  }
  if (isTyping() || e.metaKey || e.ctrlKey || e.altKey) return;
  if (!$("media-modal").classList.contains("hidden")) {
    if (e.key === "ArrowLeft") return void stepMedia(-1);
    if (e.key === "ArrowRight") return void stepMedia(1);
  }
  if ($$(".modal-backdrop:not(.hidden)").length) return;

  if (e.code === "Space") {
    e.preventDefault();
    if (!e.repeat) post("/api/motion/jump", { height: SHORTCUT_JUMP_HEIGHT });
    return;
  }
  if (!e.repeat) {
    const k = e.key.toLowerCase();
    if (k === "p") return void $("btn-photo").click();
    if (k === "r") return void $("btn-record").click();
    if (k === "h") return void $("btn-highlight").click();
    if (k === "u") return void $("btn-unlock-target").click();
    if (k === "f") return void toggleFullscreen();
    if (e.key === "?") return void openModal("help-modal");
    if (/^[1-9]$/.test(e.key)) return void selectTab(tabOrder()[Number(e.key) - 1]);
  }
  if (e.code.startsWith("Arrow")) e.preventDefault();
  state.keys.add(e.code);
});
window.addEventListener("keyup", (e) => state.keys.delete(e.code));

// Dead-man hygiene: losing focus / hiding the tab must stop the robot, never leave a stick latched.
// Hidden tabs throttle timers to ~1 Hz, which would otherwise re-arm the server's 0.5 s dead-man
// once a second and make the robot lurch.
const zeroSticks = () => {
  for (const s of [state.stickLeft, state.stickRight]) {
    s.x = 0;
    s.y = 0;
    s.active = false;
  }
  state.keys.clear();
  keySmoothed.lx = keySmoothed.ly = keySmoothed.rx = keySmoothed.ry = 0;
  for (const k of ["knob-left", "knob-right"]) {
    const el = $(k);
    if (el) el.style.transform = "translate(-50%, -50%)";
  }
};
const forceNeutral = () => {
  zeroSticks();
  state.neutralBurst = NEUTRAL_BURST;
  sendNeutralNow();
};
window.addEventListener("blur", forceNeutral);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) forceNeutral();
});
window.addEventListener("pagehide", forceNeutral);

const joyPayload = (lx, ly, rx, ry, fromGamepad) =>
  JSON.stringify({
    type: "joystick",
    lx,
    ly,
    rx,
    ry,
    roll: state.bodyRoll,
    height: state.bodyHeight,
    roll_mode: state.rollMode,
    gamepad_source: fromGamepad,
  });

function sendNeutralNow() {
  const ws = state.wsControl;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const payload = joyPayload(0, 0, 0, 0, false);
  try {
    ws.send(payload);
    state.lastSentJoyJson = payload;
    state.lastJoySentAt = Date.now();
  } catch (_) {
    // socket closing: the server-side dead-man neutralizes anyway
  }
}

setInterval(() => {
  const ax = { lx: state.stickLeft.x, ly: state.stickLeft.y, rx: state.stickRight.x, ry: state.stickRight.y };
  let fromGamepad = false;

  // Keyboard overrides (slew-limited, see KEY_RAMP_*)
  const keyTarget = { lx: 0, ly: 0, rx: 0, ry: 0 };
  for (const [key, axis, value] of KEY_AXES) if (state.keys.has(key)) keyTarget[axis] = value;
  for (const axis of Object.keys(keySmoothed)) {
    keySmoothed[axis] = rampToward(keySmoothed[axis], keyTarget[axis], JOY_TICK_MS / 1000);
    if (keySmoothed[axis] !== 0) ax[axis] = Math.round(keySmoothed[axis] * 1000) / 1000;
  }
  $("pill-keyboard").classList.toggle("pill-active", DRIVE_KEYS.some((k) => state.keys.has(k)));

  // Gamepad
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  const pad = pads && Array.from(pads).find(Boolean);
  const gpPill = $("pill-gamepad");
  if (pad) {
    gpPill.classList.remove("pill-off");
    $("pill-gamepad-txt").textContent = "Gamepad";
    const dead = (v) => (Math.abs(v) < GAMEPAD_DEADZONE ? 0 : v);
    const gx = dead(pad.axes[0] || 0);
    const gy = dead(-(pad.axes[1] || 0));
    const grx = dead(pad.axes[2] || 0);
    const gry = dead(-(pad.axes[3] || 0));
    if (gx || gy || grx || gry) {
      Object.assign(ax, { lx: gx, ly: gy, rx: grx, ry: gry });
      fromGamepad = true;
    }
    gpPill.classList.toggle("pill-active", fromGamepad);
    // A / Cross = jump (edge-triggered)
    const aDown = !!pad.buttons[0]?.pressed;
    if (aDown && !state.padButtons[0]) post("/api/motion/jump", { height: SHORTCUT_JUMP_HEIGHT });
    state.padButtons[0] = aDown;
  } else {
    gpPill.classList.add("pill-off");
    gpPill.classList.remove("pill-active");
    $("pill-gamepad-txt").textContent = "No gamepad";
  }

  const { lx, ly, rx, ry } = ax;
  $("readout-left").textContent = `LX ${lx.toFixed(2)} · LY ${ly.toFixed(2)}`;
  $("readout-right").textContent = `RX ${rx.toFixed(2)} · RY ${ry.toFixed(2)}`;

  if (document.hidden) return; // never drive from a hidden tab (see forceNeutral)
  if (state.wsControl && state.wsControl.readyState === WebSocket.OPEN) {
    const payload = joyPayload(lx, ly, rx, ry, fromGamepad);
    const isNeutral = lx === 0 && ly === 0 && rx === 0 && ry === 0 && state.bodyRoll === 0;
    const now = Date.now();
    if (!isNeutral && state.lastJoyNeutral === true) state.neutralBurst = 0;
    if (isNeutral && state.lastJoyNeutral === false) state.neutralBurst = NEUTRAL_BURST; // release: send neutral a few times
    state.lastJoyNeutral = isNeutral;
    // Non-neutral: every tick plus a hard keepalive bound; the server and the motion
    // layer both expire stick axes when updates stop. Neutral: on change + a short repeat burst.
    const stale = now - (state.lastJoySentAt || 0) >= JOY_KEEPALIVE_MS;
    if (!isNeutral || payload !== state.lastSentJoyJson || state.neutralBurst > 0 || (stale && !isNeutral)) {
      state.wsControl.send(payload);
      state.lastSentJoyJson = payload;
      state.lastJoySentAt = now;
      if (isNeutral && state.neutralBurst > 0) state.neutralBurst -= 1;
    }

    // Game tilt pad streaming
    if (state.tilt.active) {
      state.wsControl.send(
        JSON.stringify({
          type: "game_input",
          pitch_deg: state.tilt.pitch,
          roll_deg: state.tilt.roll,
          yaw_deg: Number($("slider-game-yaw").value),
          jump: false,
          action_pressed: false,
        })
      );
    }
  }
}, JOY_TICK_MS);

// ---------------------------------------------------------------- top bar & drive deck

const triggerEstop = () => post("/api/motion/estop", {}, "EMERGENCY STOP TRIGGERED");
on("btn-estop", "click", triggerEstop);
on("btn-ping", "click", () => post("/api/system/ping", {}, (r) => `Pong: ${r.echo} (${r.rtt_ms} ms)`));

on("btn-stand", "click", () => post("/api/motion/stand", {}, "Standing & self-balancing"));
on("btn-sit", "click", () => post("/api/motion/sit", {}, "Folding legs"));
on("btn-dock", "click", () => post("/api/motion/dock", {}, "Autonomous docking engaged"));
on("btn-undock", "click", () => post("/api/motion/undock", {}, "Undocking"));
on("btn-jump", "click", () => post("/api/motion/jump", { height: BUTTON_JUMP_HEIGHT }, "Jump!"));
on("btn-turn180", "click", () => post("/api/motion/turn_around", {}, "180° quick turn"));
on("btn-ego-mode", "click", async () => {
  // The firmware only enters pet mode while standing and still: say what actually happened.
  const r = await run(() => api("/api/motion/ego", "POST", {}));
  if (!r) return;
  for (const w of r.warnings || []) toast(w, "info");
  if (r.entered === true) toast("Pet mode on: Nebi reacts to touch, gestures and voice");
  else if (r.entered === false) toast(r.hint || `Robot stayed in ${r.mission || "its current mode"}`, "error");
  else toast("Pet mode requested (no mode report from the robot to confirm)", "info");
});
$$("[data-flip]").forEach((btn) =>
  btn.addEventListener("click", () =>
    post("/api/motion/flip", { direction: Number(btn.dataset.flip) }, `${btn.textContent.replace(/^\S+\s/, "")}!`)
  )
);

on("gear-selector", "click", (e) => {
  const b = e.target.closest("[data-gear]");
  if (!b) return;
  touchedAt.set($("gear-selector"), Date.now());
  setSegment($("gear-selector"), (x) => x === b);
  post("/api/motion/speed_gear", { gear: Number(b.dataset.gear) }, `Speed gear: ${b.textContent}`);
});

on("slider-height", "input", (e) => {
  state.bodyHeight = Number(e.target.value);
  $("val-height").textContent = `${Math.round(state.bodyHeight * 100)}%`;
});
on("slider-roll", "input", (e) => {
  state.bodyRoll = Number(e.target.value);
  $("val-roll").textContent = `${Math.round(state.bodyRoll * 16)}°`;
});
on("slider-roll", "dblclick", () => {
  state.bodyRoll = 0.0;
  if ($("slider-roll")) $("slider-roll").value = "0.0";
  if ($("val-roll")) $("val-roll").textContent = "0°";
});
on("chk-roll-mode", "change", (e) => (state.rollMode = e.target.checked));
