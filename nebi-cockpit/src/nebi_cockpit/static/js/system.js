import { $, api, confirmDialog, esc, fmtBytes, on, post, toast } from "./core.js";
import { clearHms } from "./telemetry.js";

// System tab: mission, HMS fault injection, robot Wi-Fi (scan / provision / connect / AP), OTA upload, simulator tools.

// ---------------------------------------------------------------- system

on("btn-set-mission", "click", () => {
  const mission_id = Number($("select-mission").value);
  const sub_mode = Number($("input-submode").value || 0);
  post("/api/motion/mission", { mission_id, sub_mode }, `Mission → ${$("select-mission").selectedOptions[0].text}`);
});

on("btn-inject-fault", "click", () => {
  const code = $("hms-inject-code").value.trim() || "0xF0010012";
  post("/api/system/hms/inject_sim_fault", { code, instance: 0 }, `Simulated HMS fault ${code}`);
});
on("btn-hms-banner-clear", "click", () =>
  clearHms($("hms-banner").dataset.code || "0xF0010012", Number($("hms-banner").dataset.instance || 0))
);
on("btn-hms-refresh", "click", () =>
  post("/api/system/hms/refresh", {}, (r) => `Robot reports ${r.count} active fault${r.count === 1 ? "" : "s"}`)
);

const wifiFeedback = (msg, isErr = false) => {
  const fb = $("wifi-prov-feedback");
  fb.className = `notice${isErr ? " error" : ""}`;
  fb.textContent = msg;
};
const signalBars = (rssi) => {
  const lvl = rssi >= -55 ? 4 : rssi >= -65 ? 3 : rssi >= -75 ? 2 : 1;
  return `<span class="bars">${[4, 7, 10, 13].map((h, i) => `<i class="${i < lvl ? "on" : ""}" style="height:${h}px"></i>`).join("")}</span>`;
};

on("btn-wifi-scan", "click", async () => {
  const box = $("wifi-results");
  box.innerHTML = `<div class="empty">Scanning…</div>`;
  try {
    const res = await api("/api/system/wifi/scan");
    const nets = (res.networks || []).sort((a, b) => b.rssi - a.rssi);
    box.innerHTML = nets.length
      ? nets
          .map(
            (n) => `<div class="row">
              <div class="row-main">
                <div class="row-title">${signalBars(n.rssi)}${esc(n.ssid)} ${n.security ? "🔒" : ""}</div>
                <div class="row-sub">Channel ${esc(n.channel)} · ${esc(n.rssi)} dBm</div>
              </div>
              <button class="btn btn-light btn-sm" data-ssid="${esc(n.ssid)}">Select</button>
            </div>`
          )
          .join("")
      : `<div class="empty">No networks found.</div>`;
  } catch (e) {
    box.innerHTML = "";
    toast(e.message, "error");
  }
});
on("wifi-results", "click", (e) => {
  const b = e.target.closest("[data-ssid]");
  if (!b) return;
  $("wifi-ssid").value = b.dataset.ssid;
  $("wifi-pass").focus();
});

on("btn-wifi-provision", "click", async () => {
  const ssid = $("wifi-ssid").value.trim();
  if (!ssid) return toast("Enter or select a Wi-Fi network first", "info");
  wifiFeedback(`Running 3-phase transactional provisioning for “${ssid}”…`);
  try {
    const res = await api("/api/system/wifi/provision", "POST", {
      ssid,
      password: $("wifi-pass").value,
      security: $("wifi-pass").value ? 2 : 0,
      switch_connection: true,
    });
    wifiFeedback(`Joined “${res.ssid}” (${res.status}) · robot LAN IP: ${res.ipv4 || "same host"}`);
    toast(`Home Wi-Fi active: ${res.ssid}`);
  } catch (e) {
    const hint = e.nebiError && e.nebiError.hint ? ` — ${e.nebiError.hint}` : "";
    wifiFeedback(`${e.message}${hint}`, true);
    toast(e.message, "error");
  }
});
on("btn-wifi-connect", "click", async () => {
  const ssid = $("wifi-ssid").value.trim();
  if (!ssid) return toast("Enter or select a Wi-Fi network first", "info");
  try {
    const res = await api("/api/system/wifi/connect", "POST", {
      ssid,
      password: $("wifi-pass").value,
      security: $("wifi-pass").value ? 2 : 0,
    });
    wifiFeedback(`Credentials sent for “${ssid}”: ${JSON.stringify(res)}`);
  } catch (e) {
    wifiFeedback(e.message, true);
  }
});
on("btn-wifi-station", "click", async () => {
  try {
    const res = await api("/api/system/wifi/station", "POST", {});
    wifiFeedback(`Station mode requested: ${JSON.stringify(res)}`);
  } catch (e) {
    wifiFeedback(e.message, true);
  }
});
on("btn-wifi-ap", "click", async () => {
  try {
    await api("/api/system/wifi/switch_ap", "POST", {});
    wifiFeedback("Robot switched back to SoftAP (192.168.7.1) and BLE advertising re-enabled.");
    toast("Robot back in SoftAP mode");
  } catch (e) {
    toast(e.message, "error");
  }
});

// OTA
const otaFile = () => $("ota-file").files[0];
on("ota-file", "change", () => {
  const f = otaFile();
  $("ota-file-lbl").textContent = f ? `${f.name} · ${fmtBytes(f.size)}` : "Choose or drop a firmware archive";
});
const drop = $("ota-drop");
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("over");
  })
);
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  if (e.dataTransfer.files.length) {
    $("ota-file").files = e.dataTransfer.files;
    $("ota-file").dispatchEvent(new Event("change"));
  }
});
on("btn-ota-upload", "click", async () => {
  const f = otaFile();
  if (!f) return toast("Choose a firmware archive first", "info");
  const ok = await confirmDialog("Upload firmware?", `${f.name} will be uploaded and the robot will start upgrading.`, "Upload");
  if (!ok) return;
  const fd = new FormData();
  fd.append("file", f);
  const btn = $("btn-ota-upload");
  btn.disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/system/ota/upload");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      $("ota-meter").style.width = `${pct}%`;
      $("ota-status-lbl").textContent = `Uploading… ${pct}%`;
    }
  };
  xhr.onload = () => {
    btn.disabled = false;
    if (xhr.status >= 200 && xhr.status < 300) toast("Firmware uploaded, upgrade started");
    else toast(`OTA upload failed (HTTP ${xhr.status})`, "error");
  };
  xhr.onerror = () => {
    btn.disabled = false;
    toast("OTA upload failed", "error");
  };
  xhr.send(fd);
});

// Simulator tools
on("btn-sim-req-ble-btn", "click", () =>
  post("/api/sim/pairing_button", { reset: true, require_button: true }, "Next BLE bootstrap will require the 5 s button press")
);
const triggerSim5sButton = () => post("/api/sim/pairing_button", { reset: false }, "Simulated 5 s pairing button press");
on("btn-sim-press-ble-btn", "click", triggerSim5sButton);
on("btn-modal-sim-5s", "click", triggerSim5sButton);
on("btn-banner-sim-auth", "click", triggerSim5sButton);
on("btn-sim-glitch", "click", () => post("/api/sim/link_drop", { duration_sec: 1.6 }, "Injected 1.6 s UDP blackout (watch DEGRADED)"));
on("btn-sim-drop", "click", () => post("/api/sim/link_drop", { duration_sec: 4.2 }, "Injected 4.2 s link drop (watch RECONNECTING)"));
