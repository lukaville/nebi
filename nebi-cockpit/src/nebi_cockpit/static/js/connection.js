import { loadTrajectories } from "./camera.js";
import { $, $$, api, closeModal, esc, hooks, on, openModal, post, state, toast, wsUrl } from "./core.js";
import { refreshMediaGallery } from "./media.js";
import { loadParamsForm } from "./params.js";
import { updateTelemetryDom } from "./telemetry.js";
import { drawHudOverlay } from "./video.js";

// Connection: the /ws/control status stream, connection stage / banner rendering and the connection modal.
// (The joystick side of /ws/control lives in drive.js; it shares the socket via state.wsControl.)

const CONTROL_RECONNECT_MS = 1000;
const DEFAULT_COUNTRY = "US";

/** Adopt a connection summary from the status stream or a REST error body. */
function applyConnection(c) {
  state.connection = c;
  updateConnectionDom(c);
}
hooks.onConnection = applyConnection;

// ---------------------------------------------------------------- control + telemetry WS

export function connectControlWs() {
  const ws = new WebSocket(wsUrl("/ws/control"));
  state.wsControl = ws;

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "status") {
        if (msg.connection) applyConnection(msg.connection);
        if (msg.telemetry) {
          state.telemetry = msg.telemetry;
          updateTelemetryDom(msg.telemetry);
          if (state.useMjpegFallback) drawHudOverlay();
        }
      }
    } catch (_) {
      // malformed frame: skip it, the next status push follows within ~66 ms
    }
  };

  ws.onclose = () => {
    $("conn-dot").classList.remove("online", "degraded");
    $("conn-dot").classList.add("error");
    $("conn-stage-badge").textContent = "SERVER OFFLINE";
    setTimeout(connectControlWs, CONTROL_RECONNECT_MS);
  };
}

const STEP_ORDER = ["ble", "auth", "http", "ucp", "zenoh", "live"];

let autoOpenChecked = false;

export function updateConnectionDom(c) {
  if (!c) return;
  const stage = c.stage || "disconnected";
  // A fresh cockpit starts with no robot (`serve --sim` for the simulator): offer the connection menu once.
  if (!autoOpenChecked) {
    autoOpenChecked = true;
    if (!c.connected && stage === "disconnected" && !c.link_op) {
      setTimeout(() => $("btn-conn-modal")?.click(), 0);
    }
  }
  const flow = (FLOW_LABEL[c.flow] || c.flow || "disconnected").toUpperCase().replace(/_/g, " ");
  const dot = $("conn-dot");
  dot.classList.remove("online", "degraded", "error");
  if (c.connected && !c.degraded) {
    dot.classList.add("online");
  } else if (
    c.degraded ||
    stage === "reconnecting" ||
    stage.startsWith("ble_") ||
    stage === "security_handshaking" ||
    stage === "discovering" ||
    stage === "wifi_provisioning"
  ) {
    dot.classList.add("degraded");
  } else {
    dot.classList.add("error");
  }

  $("conn-mode-badge").textContent = flow;
  $("conn-stage-badge").textContent = stage.toUpperCase().replace(/_/g, " ");
  renderConnStatus(c, dot.className);

  const detailTxt = c.stage_detail ? ` — ${c.stage_detail}` : "";
  $("conn-modal-stage-lbl").textContent = `Stage: ${stage}${c.bind_interface ? ` [iface=${c.bind_interface}]` : ""}${detailTxt}`;

  // Simulator-only tools
  $$(".sim-only").forEach((el) => el.classList.toggle("hidden", !c.simulator_ble));

  // Flow cards: mark the current one
  $$(".flow-card").forEach((el) => el.classList.toggle("current", c.connected && el.dataset.flow === c.flow));

  // 6-step stepper
  let activeIdx = -1;
  let doneUpTo = -1;
  if (stage === "connected" || stage === "degraded") {
    doneUpTo = 5;
  } else if (["discovering", "ble_connecting", "ble_fetching_wifi_info", "joining_softap"].includes(stage)) {
    activeIdx = 0;
  } else if (stage === "ble_awaiting_button_auth") {
    doneUpTo = 0;
    activeIdx = 1;
  } else if (stage === "probing_http") {
    doneUpTo = 1;
    activeIdx = 2;
  } else if (stage === "ucp_handshaking" || stage === "reconnecting") {
    doneUpTo = 2;
    activeIdx = 3;
  } else if (stage === "zenoh_tunneling" || stage === "security_handshaking") {
    doneUpTo = 3;
    activeIdx = 4;
  } else if (stage === "stream_starting") {
    doneUpTo = 4;
    activeIdx = 5;
  }

  $$("#conn-stepper .step").forEach((el) => {
    const idx = STEP_ORDER.indexOf(el.dataset.step);
    el.classList.remove("active", "done", "error");
    if (stage === "error" && c.last_error) {
      const errSt = c.last_error.stage || "";
      if (
        (idx === 0 && ["discovering", "ble_connecting", "ble_fetching_wifi_info"].includes(errSt)) ||
        (idx === 1 && errSt === "ble_awaiting_button_auth") ||
        (idx === 2 && errSt === "probing_http") ||
        (idx === 3 && ["ucp_handshaking", "reconnecting", "error"].includes(errSt)) ||
        (idx === 4 && errSt === "zenoh_tunneling")
      ) {
        el.classList.add("error");
      }
    } else if (idx <= doneUpTo) {
      el.classList.add("done");
    } else if (idx === activeIdx) {
      el.classList.add("active");
    }
  });

  if (c.ble_credentials) showBleCreds(c.ble_credentials);

  // Stage history (only while modal is open)
  if (!$("conn-modal").classList.contains("hidden")) renderStageHistory(c.stage_history || []);

  // Banner
  const banner = $("conn-banner");
  $("btn-banner-sim-auth").classList.toggle("hidden", stage !== "ble_awaiting_button_auth");

  if (stage === "ble_awaiting_button_auth") {
    banner.className = "banner warn";
    $("conn-banner-title").textContent = "Pairing authorization needed";
    $("conn-banner-msg").textContent = "Hold Nebi's physical button for 5 seconds until the chime plays.";
    $("conn-banner-hint").textContent = c.simulator_ble ? "Simulator: use “Simulate 5s button hold”." : "";
  } else if (stage === "reconnecting") {
    banner.className = "banner warn";
    $("conn-banner-title").textContent = `Reconnecting (attempt ${c.reconnect_attempt || 1})`;
    $("conn-banner-msg").textContent = c.stage_detail || "UCP link dropped — attempting automatic recovery…";
    $("conn-banner-hint").textContent = "Joystick zeroed for safety while the link recovers.";
  } else if (stage === "degraded") {
    banner.className = "banner warn";
    $("conn-banner-title").textContent = "Link degraded";
    $("conn-banner-msg").textContent = c.stage_detail || `UDP silence ${c.rx_silence_ms} ms`;
    $("conn-banner-hint").textContent = "Watching the 3.5 s heartbeat watchdog…";
  } else if (stage === "wifi_provisioning") {
    banner.className = "banner";
    $("conn-banner-title").textContent = "Wi-Fi provisioning";
    $("conn-banner-msg").textContent = c.stage_detail || "Switching Nebi from SoftAP to home Wi-Fi…";
    $("conn-banner-hint").textContent = "";
  } else if (c.last_error) {
    const eKey = `${c.last_error.code}:${c.last_error.message}`;
    if (state.dismissedErrorKey !== eKey) {
      banner.className = "banner error";
      $("conn-banner-title").textContent = c.last_error.code || "Connection error";
      $("conn-banner-msg").textContent = c.last_error.message || "";
      $("conn-banner-hint").textContent = c.last_error.hint ? `Hint: ${c.last_error.hint}` : "";
    } else {
      banner.classList.add("hidden");
    }
  } else {
    banner.classList.add("hidden");
  }
}

function renderStageHistory(items) {
  $("stage-history").innerHTML = items
    .slice(-12)
    .map((h) => {
      const t = h.ts ? new Date(h.ts * 1000).toLocaleTimeString() : "";
      return `<li><span class="tl-stage">${esc(h.stage)}</span><span class="tl-time">${t}</span><div class="tl-detail">${esc(h.detail || "")}</div></li>`;
    })
    .join("") || `<li><span class="tl-detail">No events yet.</span></li>`;
}

// Status pushes carry the hotspot credentials without the password (`password_set` only); the full password
// comes only in the response of the request that fetched it. Keep showing it until other credentials arrive.
let shownCreds = null;

function showBleCreds(bc) {
  if (!bc.password && shownCreds?.password && shownCreds.ssid === bc.ssid) return;
  shownCreds = bc;
  const box = $("ble-creds-box");
  box.classList.remove("hidden");
  const enc = bc.md_ts_established ? " · MD-TS X25519 paired" : "";
  const pass = bc.password || (bc.password_set ? "•••••••• (fetch again to show)" : "—");
  box.textContent = `SoftAP credentials — SSID: ${bc.ssid} · Password: ${pass} · IP: ${bc.ap_ip}${enc}`;
}

// ---------------------------------------------------------------- banner actions

on("btn-banner-retry", "click", () => {
  state.dismissedErrorKey = "";
  post("/api/connection/retry", {}, "Reconnected!");
});
on("btn-banner-sim", "click", () => {
  state.dismissedErrorKey = "";
  post("/api/connection", { mode: "simulator" }, "Switched to simulator");
});
on("btn-banner-dismiss", "click", () => {
  if (state.connection && state.connection.last_error) {
    state.dismissedErrorKey = `${state.connection.last_error.code}:${state.connection.last_error.message}`;
  }
  $("conn-banner").classList.add("hidden");
});

// ---------------------------------------------------------------- connection modal

// Plain-language names for the flows a live link can report (`ble_home_wifi` ends up as a `lan_sta` link).
const FLOW_LABEL = {
  simulator: "Simulator",
  lan_sta: "Home Wi-Fi",
  softap: "Nebi hotspot",
  ble_bootstrap: "Nebi hotspot",
  usb_ncm: "USB-C",
  auto: "Auto",
  disconnected: "Not connected",
};
const DEFAULT_FLOW = "lan_sta";
const LAST_FLOW_KEY = "nebi.conn.lastFlow";
const LAST_HOST_KEY = "nebi.conn.lastHost";
const ROBOT_FLOWS = ["lan_sta", "ble_home_wifi", "softap", "ble_bootstrap", "usb_ncm"];

// Step-by-step instructions shown under the flow cards.
const FLOW_HELP = {
  lan_sta: [
    "Nebi must already be on your Wi-Fi. After a power-on it is not: use “Wake up over Bluetooth and connect to Home Wi-Fi” first.",
    "Enter its IP address, or leave it blank and Connect to search your network.",
  ],
  ble_home_wifi: [
    "Use this after every power-on: Nebi then only listens on Bluetooth.",
    "Keep Nebi near this computer and enter your Wi-Fi name and password.",
    "If asked, hold Nebi's button for 3–5 seconds. This computer briefly joins Nebi's hotspot, then returns to your Wi-Fi and connects.",
  ],
  softap: [
    "Join this computer's Wi-Fi to Nebi's hotspot first (its name starts with Beni-; get the password with “Hotspot via Bluetooth”).",
    "This computer has no internet while it is on the hotspot.",
  ],
  ble_bootstrap: [
    "Pairs over Bluetooth and reads the hotspot name and password (hold Nebi's button if asked).",
    "Turn on the switch below to move this computer onto the hotspot automatically, or just fetch the password.",
  ],
  usb_ncm: [
    "Plug Nebi into this computer with a USB-C data cable (a charge-only cable won't work) and wait a few seconds.",
    "Nebi should appear as a new network adapter (192.168.123.x). Some robots or firmware versions only charge over USB-C; then use Wi-Fi.",
  ],
  simulator: ["A built-in virtual robot: useful for trying the cockpit without hardware."],
  auto: ["Tries your home Wi-Fi, then Nebi's hotspot, then Bluetooth."],
};

const APPLY_LABEL = {
  ble_home_wifi: "Wake up & connect",
  ble_bootstrap: "Pair & connect",
  simulator: "Start simulator",
  auto: "Try everything",
};

// Plain-language status for the modal header.
const STAGE_TEXT = {
  disconnected: "Not connected",
  discovering: "Looking for Nebi…",
  ble_connecting: "Connecting over Bluetooth…",
  ble_awaiting_button_auth: "Hold Nebi's button for 3–5 seconds to approve pairing",
  ble_fetching_wifi_info: "Reading Wi-Fi details over Bluetooth…",
  joining_softap: "Joining Nebi's hotspot…",
  probing_http: "Reaching the robot…",
  ucp_handshaking: "Opening the control link…",
  zenoh_tunneling: "Opening the data link…",
  security_handshaking: "Securing the link…",
  stream_starting: "Starting video…",
  wifi_provisioning: "Sending your Wi-Fi to Nebi…",
  reconnecting: "Link lost, reconnecting…",
  degraded: "Connected, but the link is weak",
};

const storeGet = (k) => {
  try {
    return localStorage.getItem(k) || "";
  } catch (_) {
    return "";
  }
};
const storeSet = (k, v) => {
  try {
    localStorage.setItem(k, v);
  } catch (_) {
    // private window / blocked storage: the default flow is used next time
  }
};

function renderConnStatus(c, dotClass) {
  const stage = c.stage || "disconnected";
  let title = STAGE_TEXT[stage] || stage.replace(/_/g, " ");
  let detail = c.stage_detail || "";
  if (c.connected && stage !== "degraded") {
    title = `Connected · ${FLOW_LABEL[c.flow] || c.flow}${c.flow !== "simulator" && c.host ? ` · ${c.host}` : ""}`;
    detail = c.md_ts_active ? "Encrypted link (MD-TS)" : "";
  } else if (stage === "error" && c.last_error) {
    title = `Connection failed: ${c.last_error.message || c.last_error.code}`;
    detail = c.last_error.hint ? `What to try: ${c.last_error.hint}` : "";
  }
  $("conn-status-title").textContent = title;
  $("conn-status-detail").textContent = detail;
  $("conn-status-dot").className = dotClass;
}

const countryCode = () => ($("conn-country").value.trim() || DEFAULT_COUNTRY).toUpperCase();
const currentFlow = () => $("conn-mode-select").value;

export function renderFlowCards(flows) {
  $("flow-cards").innerHTML = flows
    .map(
      (f) =>
        `<button class="flow-card" type="button" data-flow="${esc(f.id)}">${f.badge ? `<em>${esc(f.badge)}</em>` : ""}<strong>${esc(f.name)}</strong><span>${esc(f.desc || "")}</span></button>`
    )
    .join("");
  const unsupported = state.catalog?.unsupported_flows || [];
  $("flow-unsupported").textContent = unsupported.map((u) => `${u.name}: not available. ${u.reason}`).join(" ");
  selectFlow(defaultFlow());
}

/** Current link's flow when connected, else the last flow used from this browser, else Home Wi-Fi. */
function defaultFlow() {
  const known = (f) => f && $$(`.flow-card[data-flow="${f}"]`).length;
  const c = state.connection;
  if (c?.connected && known(c.flow)) return c.flow;
  if (c?.connected && c.flow === "ble_bootstrap" && known("softap")) return "softap";
  const last = storeGet(LAST_FLOW_KEY);
  if (known(last)) return last;
  return known(state.catalog?.default_flow) ? state.catalog.default_flow : DEFAULT_FLOW;
}

function selectFlow(flow) {
  $("conn-mode-select").value = flow;
  $$(".flow-card").forEach((el) => el.classList.toggle("active", el.dataset.flow === flow));
  syncConnModalFields();
}
on("flow-cards", "click", (e) => {
  const b = e.target.closest("[data-flow]");
  if (b) selectFlow(b.dataset.flow);
});

async function refreshInterfacesDropdown() {
  try {
    const res = await api("/api/network/interfaces");
    const sel = $("conn-iface-select");
    const prev = sel.value;
    sel.innerHTML =
      `<option value="">Automatic (default route)</option>` +
      (res.interfaces || [])
        .map(
          (i) =>
            `<option value="${esc(i.name)}">${esc(i.name)} — ${esc(i.ipv4)} (${esc(i.kind)}${i.is_default ? ", primary" : ""}${
              i.subnet_role !== "lan" ? `, ${esc(i.subnet_role)}` : ""
            })</option>`
        )
        .join("");
    if (prev) sel.value = prev;
  } catch (_) {
    // keep the previous list; the modal still works with "Automatic"
  }
}

function syncConnModalFields() {
  const flow = currentFlow();
  const isBle = flow === "ble_bootstrap" || flow === "ble_home_wifi";
  $("flow-help").innerHTML = (FLOW_HELP[flow] || []).map((t) => `<li>${esc(t)}</li>`).join("");
  $("ble-bootstrap-fields").classList.toggle("hidden", !isBle);
  $("ble-box-title").textContent = flow === "ble_home_wifi" ? "Your Wi-Fi (sent to Nebi over Bluetooth)" : "Nebi's hotspot via Bluetooth";
  $("ble-softap-row").classList.toggle("hidden", flow !== "ble_bootstrap");
  $("ble-home-wifi-box").classList.toggle("hidden", flow !== "ble_home_wifi");
  $("conn-ip-fields").classList.toggle("hidden", !["lan_sta", "softap", "usb_ncm"].includes(flow));
  const HOST_UI = {
    softap: ["Hotspot IP address (optional)", "192.168.7.1"],
    usb_ncm: ["Robot USB IP address (optional)", "192.168.123.1"],
  };
  const [hostLabel, hostPlaceholder] = HOST_UI[flow] || ["Robot IP address (optional)", "Leave blank to search, e.g. 192.0.2.10"];
  $("conn-host-label").textContent = hostLabel;
  $("conn-host").placeholder = hostPlaceholder;
  if (flow === "lan_sta" && !$("conn-host").value) $("conn-host").value = storeGet(LAST_HOST_KEY);
  const applyBtn = $("btn-conn-apply");
  if (applyBtn && !applyBtn.disabled) applyBtn.textContent = APPLY_LABEL[flow] || "Connect";
}

function showModalError(e) {
  const box = $("conn-modal-error");
  box.classList.remove("hidden");
  const hint = e.nebiError && e.nebiError.hint ? `\nWhat to try: ${e.nebiError.hint}` : "";
  box.textContent = `${e.message}${hint}`;
}

function rememberFlow(flow, host) {
  if (!ROBOT_FLOWS.includes(flow) && flow !== "simulator") return;
  storeSet(LAST_FLOW_KEY, flow);
  if (flow === "lan_sta" && host) storeSet(LAST_HOST_KEY, host);
}

on("btn-fill-sim-ble", "click", () => {
  $("conn-ble-addr").value = "SIM-BLE-LZ110-01";
  $("conn-host").value = "127.0.0.1";
});

on("btn-conn-modal", "click", async () => {
  $("conn-modal-error").classList.add("hidden");
  selectFlow(defaultFlow());
  if (state.connection?.current_wifi_ssid && !$("ble-home-ssid").value) {
    $("ble-home-ssid").value = state.connection.current_wifi_ssid;
  }
  if (state.connection) renderConnStatus(state.connection, $("conn-dot").className);
  renderStageHistory(state.connection?.stage_history || []);
  openModal("conn-modal");
  await refreshInterfacesDropdown();
  syncConnModalFields();
});
on("btn-conn-cancel", "click", () => closeModal($("conn-modal")));
on("btn-conn-x", "click", () => closeModal($("conn-modal")));

on("btn-scan-robots", "click", async () => {
  const box = $("discovered-robots-box");
  const btn = $("btn-scan-robots");
  box.classList.remove("hidden");
  box.innerHTML = `<div class="empty">Searching your network, Nebi's hotspot and Bluetooth…</div>`;
  btn.disabled = true;
  try {
    const res = await api("/api/discovery/scan", "POST", { include_ble: true, include_lan: true, timeout: 1.8 });
    const list = res.robots || [];
    if (!list.length) {
      box.innerHTML = `<div class="empty">No robot found. If Nebi was just switched on, use “Wake up over Bluetooth and connect to Home Wi-Fi”.</div>`;
      return;
    }
    const via = (r) => (r.flow === "ble_bootstrap" ? "Bluetooth only — use “Wake up over Bluetooth and connect to Home Wi-Fi”" : FLOW_LABEL[r.flow] || r.flow);
    box.innerHTML = list
      .map(
        (r, idx) => `<div class="row">
          <div class="row-main">
            <div class="row-title">${esc(r.name)}</div>
            <div class="row-sub">${esc(via(r))} · ${esc(r.ble_address || r.host)}${r.interface ? ` · ${esc(r.interface)}` : ""}</div>
          </div>
          <button class="btn btn-primary btn-sm" type="button" data-pick-idx="${idx}">Use</button>
        </div>`
      )
      .join("");
    box.onclick = (e) => {
      const b = e.target.closest("[data-pick-idx]");
      if (!b) return;
      const r = list[Number(b.dataset.pickIdx)];
      selectFlow(r.flow === "ble_bootstrap" ? "ble_home_wifi" : r.flow);
      $("conn-host").value = r.flow === "simulator" ? "" : r.host || "";
      if (r.ble_address) $("conn-ble-addr").value = r.ble_address;
      if (r.interface) $("conn-iface-select").value = r.interface;
      toast(`Selected ${r.name}`, "info");
    };
  } catch (e) {
    box.innerHTML = `<div class="empty">Search failed: ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
});

on("btn-ble-fetch", "click", async () => {
  const addr = $("conn-ble-addr").value.trim();
  const btn = $("btn-ble-fetch");
  btn.disabled = true;
  $("conn-modal-error").classList.add("hidden");
  try {
    const res = await api("/api/ble/bootstrap", "POST", {
      ble_address: addr,
      country_code: countryCode(),
      wait_for_button_sec: 25.0,
      use_simulator_ble: addr.startsWith("SIM-BLE"),
    });
    showBleCreds(res.credentials);
    toast("Got Nebi's hotspot name and password");
  } catch (e) {
    showModalError(e);
  } finally {
    btn.disabled = false;
  }
});

async function wakeUpOverBluetooth() {
  const ssid = $("ble-home-ssid").value.trim();
  if (!ssid) {
    toast("Enter your Wi-Fi name first", "info");
    $("ble-home-ssid").focus();
    return;
  }
  const addr = $("conn-ble-addr").value.trim();
  const btn = $("btn-conn-apply");
  btn.disabled = true;
  btn.textContent = "Waking up Nebi…";
  $("conn-modal-error").classList.add("hidden");
  state.dismissedErrorKey = "";
  try {
    const res = await api("/api/ble/provision_home_wifi", "POST", {
      ssid,
      password: $("ble-home-pass").value,
      ble_address: addr,
      country_code: countryCode(),
      interface: $("conn-iface-select").value,
      switch_os_wifi: true,
      wait_for_button_sec: 35.0,
      timeout_sec: 30,
      use_simulator_ble: addr.startsWith("SIM-BLE"),
    });
    if (res.softap_credentials) showBleCreds(res.softap_credentials);
    if (res.connection) updateConnectionDom(res.connection);
    rememberFlow("ble_home_wifi");
    if (res.lan_ip) storeSet(LAST_HOST_KEY, res.lan_ip);
    toast(`Nebi is back on “${ssid}”${res.lan_ip ? ` at ${res.lan_ip}` : ""}`);
    closeModal($("conn-modal"));
    afterConnect();
  } catch (e) {
    showModalError(e);
  } finally {
    btn.disabled = false;
    syncConnModalFields();
  }
}
// Kept for older scripts/tests that click the former dedicated button.
on("btn-ble-provision-home", "click", wakeUpOverBluetooth);

function afterConnect() {
  loadParamsForm();
  loadTrajectories();
  refreshMediaGallery();
}

on("btn-conn-disconnect", "click", () => post("/api/connection/disconnect", {}, "Disconnected from robot"));

async function connectWith(flow) {
  $("conn-modal-error").classList.add("hidden");
  state.dismissedErrorKey = "";
  const btn = $("btn-conn-apply");
  btn.disabled = true;
  btn.textContent = flow === "simulator" ? "Starting…" : "Connecting…";
  const host = ["lan_sta", "softap"].includes(flow) ? $("conn-host").value.trim() : "";
  try {
    const res = await api("/api/connection", "POST", {
      mode: flow,
      flow,
      host,
      bind_interface: $("conn-iface-select").value,
      ble_address: $("conn-ble-addr").value.trim(),
      country_code: countryCode(),
      join_wifi: $("chk-join-os-wifi").checked,
      wait_for_button_sec: 30.0,
      auto_reconnect: $("chk-auto-reconnect").checked,
      ucp_port: Number($("conn-ucp-port").value) || 9007,
      config_port: Number($("conn-config-port").value) || 8010,
      media_port: Number($("conn-media-port").value) || 8084,
      ota_port: Number($("conn-ota-port").value) || 8088,
    });
    if (res?.ble_credentials) showBleCreds(res.ble_credentials);
    rememberFlow(flow, host || state.connection?.host || "");
    closeModal($("conn-modal"));
    toast(`Connected · ${FLOW_LABEL[state.connection?.flow] || FLOW_LABEL[flow] || flow}`);
    afterConnect();
  } catch (e) {
    showModalError(e);
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    syncConnModalFields();
  }
}

on("btn-conn-apply", "click", () => {
  const flow = currentFlow();
  if (flow === "ble_home_wifi") return wakeUpOverBluetooth();
  return connectWith(flow);
});
on("btn-conn-auto", "click", () => connectWith("auto"));
