import { loadTrajectories, updateProfileHint } from "./camera.js";
import { renderFlowCards } from "./connection.js";
import { $, api, esc, state, toast } from "./core.js";
import { refreshMediaGallery } from "./media.js";
import { loadParamsForm } from "./params.js";

// Catalog load at boot: flows, camera profiles, games, sound effects (eye expressions: eyes.js).

// ---------------------------------------------------------------- catalog & panels

const GAME_EMOJI = { motion_sense: "🏎️", tug_of_war: "🪢", camera_battle: "📸", balance: "⚖️", treasure_hunt: "🗺️" };
const SFX_EMOJI = ["🔊", "👋", "🐱", "🎉", "📷", "🎺", "🐦", "😻", "📢"];
const FALLBACK_FLOWS = [
  { id: "simulator", name: "Simulator", desc: "Built-in MockNebiRobot" },
  { id: "auto", name: "Smart auto", desc: "USB-C → LAN → SoftAP → BLE" },
  { id: "lan_sta", name: "Home Wi-Fi", desc: "Robot's LAN IP" },
  { id: "usb_ncm", name: "USB-C cable", desc: "192.168.123.1" },
  { id: "softap", name: "SoftAP", desc: "192.168.7.1" },
  { id: "ble_bootstrap", name: "BLE bootstrap", desc: "Pair, fetch SoftAP creds" },
];

export async function loadCatalogAndInit() {
  let cat;
  try {
    cat = await api("/api/catalog");
  } catch (e) {
    toast(`Failed to load catalog: ${e.message}`, "error");
    setTimeout(loadCatalogAndInit, 3000);
    return;
  }
  state.catalog = cat;

  renderFlowCards(cat.flows || FALLBACK_FLOWS);

  // Camera profiles grouped by resolution
  const groups = {};
  for (const p of cat.camera_profiles) (groups[p.resolution || "Other"] ||= []).push(p);
  $("select-cam-profile").innerHTML = Object.entries(groups)
    .map(
      ([res, ps]) =>
        `<optgroup label="${esc(res)}">${ps
          .map((p) => `<option value="${p.id}">${esc(p.label)}</option>`)
          .join("")}</optgroup>`
    )
    .join("");
  updateProfileHint();

  // Games
  $("games-list").innerHTML = cat.games
    .map(
      (g) =>
        `<button class="tile" data-game-id="${g.id}"><span class="tile-emoji">${GAME_EMOJI[g.key] || "🎮"}</span><span>${esc(g.name)}</span></button>`
    )
    .join("");

  // Sound effects
  $("sfx-list").innerHTML = cat.sound_effects
    .map(
      (s) =>
        `<button class="tile" data-sfx="${s.id}" data-name="${esc(s.name)}"><span class="tile-emoji">${SFX_EMOJI[s.id] || "🔊"}</span><span>${esc(s.name)}</span></button>`
    )
    .join("");

  loadTrajectories();
  state.paramSpecs = cat.params || [];
  await loadParamsForm();
  await refreshMediaGallery();
}
