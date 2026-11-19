// "Agent driving" indicator: shows when the nebi agent CLI (hosted in this cockpit) is moving the robot,
// or when its idle "pet life" loop is on. Silent when the agent API is not present (nebi-cli not installed, or `nebi-cockpit serve --disable-extension nebi-cli`).
(() => {
  const pill = document.getElementById("agent-pill");
  if (!pill) return;
  let misses = 0;
  async function tick() {
    try {
      const r = await fetch("/agent/activity", { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      const a = await r.json();
      misses = 0;
      if (a.active) {
        const secs = Math.max(0, Math.round(Date.now() / 1000 - (a.since || 0)));
        pill.textContent = `Agent: ${a.command} ${secs}s`;
        pill.className = "agent-pill active";
        pill.hidden = false;
      } else if (a.idle_running) {
        pill.textContent = "Agent: idle life";
        pill.className = "agent-pill idle";
        pill.hidden = false;
      } else {
        pill.hidden = true;
      }
    } catch (e) {
      if (++misses > 2) pill.hidden = true;
    }
    setTimeout(tick, misses > 5 ? 10000 : 1000);
  }
  tick();
})();
