import { $, $$, canSync, esc, on, post, setSegment, state, syncChecked, syncValue, titleCase } from "./core.js";
import { setFollowActive } from "./follow.js";

// Telemetry -> DOM: top stats, overlay chips, control sync from robot state, targets / UWB / HMS lists, status grid.

// ---------------------------------------------------------------- telemetry → DOM

function setStat(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.classList.remove("good", "warn", "bad");
  if (cls) el.classList.add(cls);
}

export function updateTelemetryDom(t) {
  const lnk = t.link || {};
  const ifaceTag = lnk.bind_interface ? ` [${lnk.bind_interface}]` : "";
  $("robot-meta").textContent = `${t.serial_number} · ${t.firmware_version} · ${lnk.host}:${lnk.ucp_port}${ifaceTag}`;

  // Top stats
  const soc = Number(t.battery_soc || 0);
  setStat("hud-battery", `${soc}%${t.charging ? " ⚡" : ""}`, soc < 20 ? "bad" : soc < 40 ? "warn" : "good");
  setStat("hud-speed", `${Number(t.velocity_mps).toFixed(2)} m/s`);
  const obs = Number(t.obstacle_cm);
  setStat("hud-obstacle", `${obs} cm`, obs < 30 ? "bad" : obs < 80 ? "warn" : null);
  const rtt = Number(lnk.rtt_ms || 0);
  setStat("hud-rtt", `${rtt.toFixed(1)} ms`, rtt > 150 ? "bad" : rtt > 60 ? "warn" : null);
  setStat("hud-video", `${Number(lnk.video_fps || 0).toFixed(0)} fps · ${Math.round(lnk.video_kbps || 0)}k`);

  // Video overlay chips
  $("hud-posture").textContent = t.charging && t.is_locked ? `${t.posture_name} · LOCKED (CHARGING)` : t.posture_name;
  $("hud-mission").textContent = `${titleCase(String(t.mission_name).toLowerCase())} · ${titleCase(String(t.sub_mode_name).toLowerCase())}`;
  const tracking = t.tracking_active || t.locked_target_id;
  $("track-badge").classList.toggle("hidden", !tracking);
  if (tracking) $("track-badge-txt").textContent = `Tracking #${t.locked_target_id}`;
  $("video-empty").classList.toggle("hidden", state.useMjpegFallback || Date.now() - state.lastFrameAt < 3000);

  // Recording
  state.recording = !!t.recording;
  $("rec-badge").classList.toggle("hidden", !state.recording);
  $("btn-record").classList.toggle("recording", state.recording);
  if (state.recording) {
    const s = Math.floor(t.recording_sec || 0);
    $("rec-timer").textContent = `REC ${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    $("btn-record-txt").textContent = "Stop";
  } else {
    $("btn-record-txt").textContent = "Record";
  }

  // Control sync from robot state
  syncValue($("slider-zoom"), t.zoom_x);
  $("val-zoom").textContent = `${Number($("slider-zoom").value).toFixed(1)}×`;
  syncChecked($("chk-eis"), t.eis_enabled);
  syncChecked($("chk-horizon"), t.horizon_lock);
  syncChecked($("chk-prerecord"), t.pre_record_enabled);
  syncChecked($("chk-pro-mode"), t.pro_mode);
  $("pro-fields").classList.toggle("off", !$("chk-pro-mode").checked);
  syncValue($("slider-bitrate"), t.vt_bitrate_kbps);
  $("val-bitrate").textContent = `${$("slider-bitrate").value} kbps`;
  syncValue($("select-fps"), t.vt_fps);
  syncValue($("select-cam-profile"), t.record_profile_id);
  syncValue($("select-pnl-mode"), t.pnl_mode);
  syncChecked($("chk-voice-wakeup"), t.voice_wakeup);
  if (canSync($("stream-toggle"))) {
    $("btn-stream-start").classList.toggle("active", !!t.vt_streaming);
    $("btn-stream-stop").classList.toggle("active", !t.vt_streaming);
  }
  if (canSync($("gear-selector")) && t.sport_mode) setSegment($("gear-selector"), (b) => Number(b.dataset.gear) === t.sport_mode);
  if (canSync($("follow-modes")) && t.mission_id === 1 && t.sub_mode <= 5) setFollowActive(t.sub_mode);

  // Games
  $("game-state-lbl").textContent = t.game_state || "idle";
  $("game-score-lbl").textContent = String(t.game_score || 0);
  $$("#games-list .tile").forEach((el) =>
    el.classList.toggle("active", Number(el.dataset.gameId) === t.active_game && t.game_state !== "idle")
  );

  // Storage
  if (t.sd_total_gb) {
    const used = t.sd_total_gb - t.sd_free_gb;
    $("sd-lbl").textContent = `${t.sd_free_gb.toFixed(1)} of ${t.sd_total_gb.toFixed(0)} GB free`;
    $("sd-meter").style.width = `${Math.max(1, (used / t.sd_total_gb) * 100)}%`;
  }

  // Wi-Fi
  $("wifi-mode-lbl").textContent = t.wifi_mode || "AP";
  $("wifi-cur-ssid-lbl").textContent = t.wifi_ssid || "--";
  $("wifi-ip-lbl").textContent = t.wifi_ipv4 || lnk.host || "--";
  $("wifi-rssi-lbl").textContent = t.wifi_rssi ? `${t.wifi_rssi} dBm` : "--";

  // OTA
  $("ota-status-lbl").textContent = `OTA state: ${t.ota_state} (${t.ota_progress_pct}%)`;
  if (t.ota_state && t.ota_state !== "idle") $("ota-meter").style.width = `${t.ota_progress_pct}%`;

  // Mission
  $("mission-hint").textContent = `Current: ${t.mission_name} (${t.mission_id}) · sub-mode ${t.sub_mode_name} (${t.sub_mode})`;

  // HMS alerts
  const alerts = t.hms_alerts || [];
  $("hms-count").textContent = String(alerts.length);
  $("hms-count").classList.toggle("bad", alerts.length > 0);
  setTabDot("system", alerts.length > 0);
  if (alerts.length > 0) {
    const first = alerts[alerts.length - 1]; // newest
    const more = alerts.length > 1 ? ` (+${alerts.length - 1} more in System → Health)` : "";
    $("hms-banner").classList.remove("hidden");
    $("hms-banner-text").textContent = `[${first.code}] ${first.message_en} (${first.severity})${more}`;
    $("hms-banner").dataset.code = first.code;
    $("hms-banner").dataset.instance = String(first.instance || 0);
  } else {
    $("hms-banner").classList.add("hidden");
  }

  // Lists that change often: throttle DOM rebuilds
  const now = Date.now();
  if (now - state.lastListRender > 450) {
    state.lastListRender = now;
    renderTargets(t);
    renderUwb(t.uwb_remotes || []);
    renderHms(alerts, t.hms_pending_actions || {});
    renderHmsEvents(t.hms_events || []);
    if (state.activeTab === "system") renderStatusGrid(t);
  }
}

function setTabDot(tab, show) {
  const btn = document.querySelector(`.tab[data-tab="${tab}"]`);
  let dot = btn.querySelector(".tab-dot");
  if (show && !dot) {
    dot = document.createElement("span");
    dot.className = "tab-dot";
    btn.appendChild(dot);
  } else if (!show && dot) dot.remove();
}

function renderTargets(t) {
  const list = t.targets || [];
  $("targets-list").innerHTML = list.length
    ? list
        .map(
          (g) => `<div class="row ${g.locked ? "locked" : ""}">
            <div class="row-main">
              <div class="row-title">${g.label === "pet" ? "🐾" : "🧍"} ${esc(titleCase(g.label))} #${g.target_id}</div>
              <div class="row-sub">${g.distance_m.toFixed(1)} m · ${(g.confidence * 100).toFixed(0)}% confidence</div>
            </div>
            ${
              g.locked
                ? `<span class="pill pill-ok">Locked</span>`
                : `<button class="btn btn-light btn-sm" data-lock-target="${g.target_id}">Lock</button>`
            }
          </div>`
        )
        .join("")
    : `<div class="empty">No targets detected.</div>`;
}

function renderUwb(remotes) {
  $("uwb-list").innerHTML = remotes.length
    ? remotes
        .map(
          (r) => `<div class="row">
            <div class="row-main">
              <div class="row-title">#${r.slot} · ${esc(r.mac)} ${r.online === false ? '<span class="badge">offline</span>' : ""}</div>
              <div class="row-sub">${r.battery_pct}% battery · ${r.distance_m} m away</div>
            </div>
            <button class="btn btn-light btn-sm" data-vibrate="${r.slot}">Vibrate</button>
          </div>`
        )
        .join("")
    : `<div class="empty">No paired remotes.</div>`;
}

const hmsTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "");

function renderHms(alerts, pending = {}) {
  $("hms-list").innerHTML = alerts.length
    ? alerts
        .slice()
        .reverse()
        .map(
          (a) => `<div class="row sev-${esc(String(a.severity || "").toLowerCase())}">
            <div class="row-main">
              <div class="row-title">${esc(a.code)}${a.instance ? ` #${esc(a.instance)}` : ""} · ${esc(a.severity || "")}<span class="muted"> · since ${esc(hmsTime(a.timestamp))}</span></div>
              <div class="row-sub">${esc(a.message_en)}</div>
              ${a.recovery_hint ? `<div class="row-sub muted">${esc(a.recovery_hint)}</div>` : ""}
            </div>
            ${
              pending[`${a.code}:${a.instance || 0}`] || Object.values(pending).some((p) => p.code === a.code)
                ? `<button class="btn btn-light btn-sm" data-clear-hms="${esc(a.code)}" data-instance="${esc(a.instance || 0)}" title="The robot asked for confirmation: acknowledge this fault">Acknowledge</button>`
                : `<span class="muted" title="The robot did not ask the app to confirm this fault">auto-clears</span>`
            }
          </div>`
        )
        .join("")
    : `<div class="empty">No active faults.</div>`;
}

function renderHmsEvents(events) {
  const box = $("hms-events");
  if (!box) return;
  $("hms-events-count").textContent = events.length ? `(${events.length})` : "";
  box.innerHTML = events.length
    ? events
        .slice()
        .reverse()
        .map(
          (e) => `<li><span class="tl-stage">${esc(e.action)} ${esc(e.code)}${e.instance ? ` #${esc(e.instance)}` : ""}</span><span class="tl-time">${esc(hmsTime(e.ts))}</span><div class="tl-detail">${esc(e.message || "")} (kind ${esc(e.kind)})</div></li>`
        )
        .join("")
    : `<li><span class="tl-detail">No fault events since the cockpit connected.</span></li>`;
}

export function renderStatusGrid(t) {
  const lnk = t.link || {};
  const items = [
    ["Posture", t.posture_name],
    ["Mission", `${t.mission_name} · ${t.sub_mode_name}`],
    ["Battery", `${t.battery_soc}% · ${(t.voltage_mv / 1000).toFixed(1)} V`],
    ["Power", t.docked ? "Docked" : t.charging && t.is_locked ? "Charging (Motion Locked)" : t.charging ? "Charging" : "On battery"],
    ["Body height", `${t.height_cm} cm`],
    ["Gimbal", `${Number(t.gimbal_pitch_deg).toFixed(1)}°`],
    ["Attitude", `P ${Number(t.pitch_deg).toFixed(1)}° R ${Number(t.roll_deg).toFixed(1)}°`],
    ["Heading", `${Number(t.yaw_deg).toFixed(1)}°`],
    ["Position", `${Number(t.pos_x_m).toFixed(2)}, ${Number(t.pos_y_m).toFixed(2)} m`],
    ["Trip odometer", `${Number(t.trip_odom_m).toFixed(1)} m`],
    ["Session", `${Number(t.session_odom_m).toFixed(1)} m`],
    ["Storage", `${Number(t.sd_free_gb).toFixed(1)} / ${Number(t.sd_total_gb).toFixed(0)} GB`],
    ["Video", `${lnk.video_frames_rx || 0} frames`],
    ["Zenoh", `${lnk.zenoh_ready ? "ready" : "down"} · ${lnk.zenoh_rx_msgs || 0}↓ ${lnk.zenoh_tx_msgs || 0}↑`],
    ["Tracker", t.tracking_model || "--"],
    ["Voice", `${t.voice_lang || "--"} · vol ${t.speaker_volume ?? "--"}`],
    ["Serial", t.serial_number],
    ["Firmware", t.firmware_version],
  ];
  $("status-grid").innerHTML = items
    .map(([k, v]) => `<div class="kv" title="${esc(v)}"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`)
    .join("");
}

// Delegated list actions
on("targets-list", "click", (e) => {
  const b = e.target.closest("[data-lock-target]");
  if (b) post("/api/tracking/lock_target", { target_id: Number(b.dataset.lockTarget) }, `Locked target #${b.dataset.lockTarget}`);
});
on("uwb-list", "click", (e) => {
  const b = e.target.closest("[data-vibrate]");
  if (b) vibrateUwb(Number(b.dataset.vibrate));
});
on("hms-list", "click", (e) => {
  const b = e.target.closest("[data-clear-hms]");
  if (b) clearHms(b.dataset.clearHms, Number(b.dataset.instance || 0));
});

const vibrateUwb = (slot) => post("/api/system/uwb/vibrate", { slot, level: 2 }, `Vibrated UWB remote #${slot}`);
export const clearHms = (code, instance = 0) =>
  post("/api/system/hms/clear", { code, instance }, `Acknowledged fault ${code}`);
