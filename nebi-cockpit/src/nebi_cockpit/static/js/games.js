import { $, $$, on, post, state } from "./core.js";
import { bindPad } from "./pads.js";

// Games: start / pause / resume, tilt pad (streamed via /ws/control) and button inputs.

// ---------------------------------------------------------------- games

on("games-list", "click", (e) => {
  const b = e.target.closest("[data-game-id]");
  if (b) post("/api/games/start", { game_type: Number(b.dataset.gameId) }, `Started ${b.textContent.trim()}`);
});
$$("[data-game-act]").forEach((btn) =>
  btn.addEventListener("click", () => post("/api/games/action", { action: btn.dataset.gameAct }, `Game ${btn.dataset.gameAct}`))
);

bindPad(
  $("tilt-pad"),
  $("tilt-dot"),
  (x, y) => {
    state.tilt.active = true;
    state.tilt.roll = Math.round(x * 30);
    state.tilt.pitch = Math.round(-y * 30);
    $("tilt-readout").textContent = `Pitch ${state.tilt.pitch}° · Roll ${state.tilt.roll}°`;
  },
  () => {
    state.tilt.active = false;
    state.tilt.pitch = 0;
    state.tilt.roll = 0;
    $("tilt-readout").textContent = "Pitch 0° · Roll 0°";
    if (state.wsControl?.readyState === WebSocket.OPEN)
      state.wsControl.send(JSON.stringify({ type: "game_input", pitch_deg: 0, roll_deg: 0, yaw_deg: 0 }));
  },
  62
);
on("slider-game-yaw", "input", (e) => ($("val-game-yaw").textContent = `${e.target.value}°`));
const gameInput = (extra) =>
  post("/api/games/input", {
    pitch_deg: state.tilt.pitch,
    roll_deg: state.tilt.roll,
    yaw_deg: Number($("slider-game-yaw").value),
    ...extra,
  });
on("btn-game-jump", "click", () => gameInput({ jump: true }));
on("btn-game-action", "click", () => gameInput({ action_pressed: true }));
on("btn-game-pulse", "click", () =>
  post("/api/games/input", { pitch_deg: -18.0, roll_deg: 5.0, action_pressed: true }, "Boost sent!")
);
