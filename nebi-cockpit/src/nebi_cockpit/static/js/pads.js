import { $ } from "./core.js";

// Pointer-driven 2-D pad (virtual sticks, game tilt pad).

export function bindPad(el, knob, onMove, onRelease, maxR) {
  function update(e) {
    const rect = el.getBoundingClientRect();
    const r = maxR ?? rect.width / 2 - 28;
    let dx = e.clientX - (rect.left + rect.width / 2);
    let dy = e.clientY - (rect.top + rect.height / 2);
    const dist = Math.hypot(dx, dy);
    if (dist > r) {
      dx = (dx / dist) * r;
      dy = (dy / dist) * r;
    }
    knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
    onMove(+(dx / r).toFixed(3), +(-dy / r).toFixed(3));
  }
  let active = false;
  el.addEventListener("pointerdown", (e) => {
    active = true;
    el.classList.add("dragging");
    el.setPointerCapture(e.pointerId);
    update(e);
  });
  el.addEventListener("pointermove", (e) => active && update(e));
  const release = () => {
    active = false;
    el.classList.remove("dragging");
    knob.style.transform = "translate(-50%, -50%)";
    onRelease();
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
  el.addEventListener("lostpointercapture", () => active && release());
}
