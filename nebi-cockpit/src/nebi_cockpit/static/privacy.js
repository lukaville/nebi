// Privacy mode for screen recordings: hides identifying details (serial, firmware, IP addresses, Wi-Fi SSIDs and
// credentials, interface names, BLE addresses) and blurs the live camera and the robot's photo / video gallery.
// Toggle: the eye button (top right) or V. Remembered per browser.
(() => {
  "use strict";
  const btn = document.getElementById("btn-privacy");
  if (!btn) return;
  const KEY = "nebi.privacy";
  const get = () => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } };
  const set = (v) => { try { localStorage.setItem(KEY, v ? "1" : "0"); } catch { /* private window */ } };
  function apply(on) {
    document.body.classList.toggle("privacy", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.classList.toggle("privacy-on", on);
    btn.querySelector("use")?.setAttribute("href", on ? "#i-eye-off" : "#i-eye");
    btn.title = on ? "Privacy mode ON — sensitive details hidden (V to show)" : "Privacy mode for screen recording: hides serial, addresses and Wi-Fi details, blurs the camera (V)";
  }
  btn.addEventListener("click", () => { const on = !document.body.classList.contains("privacy"); set(on); apply(on); });
  window.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.repeat || e.key.toLowerCase() !== "v") return;
    if (window.NebiUI?.isTyping?.() || document.querySelector(".modal-backdrop:not(.hidden) input:focus")) return;
    btn.click();
  });
  // status grid rows that identify the robot (rendered by js/telemetry.js): mark them as they appear
  const grid = document.getElementById("status-grid");
  const SENS = new Set(["Serial", "Firmware", "Position"]);
  const mark = () => grid?.querySelectorAll(".kv").forEach((kv) => kv.classList.toggle("sensitive", SENS.has(kv.querySelector("span")?.textContent)));
  if (grid) new MutationObserver(mark).observe(grid, { childList: true });
  apply(get());
})();
