import { $, on, post, setSegment, state, touchedAt } from "./core.js";

// AI follow modes, distance / orbit speed, detection target type and positioning mode.

// ---------------------------------------------------------------- follow

export function setFollowActive(sub) {
  state.followSubMode = sub;
  setSegment($("follow-modes"), (b) => Number(b.dataset.submode) === sub);
  $("orbit-speed-field").classList.toggle("hidden", sub !== 4);
}
const followBody = (extra = {}) => ({
  sub_mode: state.followSubMode,
  distance_m: Number($("slider-follow-dist").value),
  ...(state.followSubMode === 4 ? { orbit_speed: Number($("slider-orbit-speed").value) } : {}),
  ...extra,
});

on("follow-modes", "click", (e) => {
  const b = e.target.closest("[data-submode]");
  if (!b) return;
  touchedAt.set($("follow-modes"), Date.now());
  setFollowActive(Number(b.dataset.submode));
  post("/api/tracking/follow_mode", followBody(), `Follow mode: ${b.querySelector("strong").textContent}`);
});
on("slider-follow-dist", "input", (e) => ($("val-follow-dist").textContent = `${Number(e.target.value).toFixed(1)} m`));
on("slider-follow-dist", "change", () => {
  if (state.followSubMode > 0) post("/api/tracking/follow_mode", followBody(), `Follow distance ${$("slider-follow-dist").value} m`);
});
on("slider-orbit-speed", "input", (e) => ($("val-orbit-speed").textContent = Number(e.target.value).toFixed(2)));
on("slider-orbit-speed", "change", () => post("/api/tracking/follow_mode", followBody(), "Orbit speed updated"));

on("select-target-type", "change", (e) =>
  post("/api/tracking/target_type", { value: Number(e.target.value) }, `Detecting: ${e.target.selectedOptions[0].text}`)
);
on("select-pnl-mode", "change", (e) =>
  post("/api/tracking/pnl_mode", { value: Number(e.target.value) }, `Positioning: ${e.target.selectedOptions[0].text}`)
);
