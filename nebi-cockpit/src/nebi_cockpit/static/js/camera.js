import { $, api, esc, icon, on, post, state, touchedAt } from "./core.js";
import { refreshMediaGallery } from "./media.js";
import { connectVideoWs, fpvCanvas } from "./video.js";

// Camera: photo / record / highlight, zoom, video transport toggle, profiles, EIS, pro exposure, stream, trajectories.

export async function loadTrajectories() {
  try {
    const tr = await api("/api/camera/trajectories");
    const list = tr.trajectories || [];
    $("trajectory-list").innerHTML = list.length
      ? list
          .map(
            (t) =>
              `<button class="tile" data-traj="${esc(t.id)}">${icon("play")}<span>${esc(t.name)}${
                t.sample_count ? `<span class="tile-sub">${t.sample_count} samples</span>` : ""
              }</span></button>`
          )
          .join("")
      : `<div class="empty">No trajectories recorded.</div>`;
  } catch (_) {
    $("trajectory-list").innerHTML = `<div class="empty">Trajectories unavailable.</div>`;
  }
}

// ---------------------------------------------------------------- camera

// Still photos: the robot firmware has no photo capture (button is disabled in index.html); the API answers 501
// CAMERA_PHOTO_UNSUPPORTED with a hint, which `post` toasts if the button is ever re-enabled.
on("btn-photo", "click", () => post("/api/camera/photo", {}, "Photo saved to album"));
on("btn-record", "click", async () => {
  if (!state.recording) {
    await post("/api/camera/record/start", {}, "Recording started");
  } else {
    // The camera finalizes the MP4 asynchronously; the server confirms the stop from camera status.
    const res = await post("/api/camera/record/stop", {}, "Recording stopped — video is being saved");
    if (res) setTimeout(refreshMediaGallery, 2500);
  }
});
on("btn-highlight", "click", () => post("/api/camera/highlight", {}, "Highlight marked ★"));
const unlock = () => post("/api/tracking/unlock", {}, "Target unlocked");
on("btn-unlock-target", "click", unlock);
on("btn-unlock-target-2", "click", unlock);

let zoomTimer = null;
on("slider-zoom", "input", (e) => {
  const z = Number(e.target.value);
  $("val-zoom").textContent = `${z.toFixed(1)}×`;
  clearTimeout(zoomTimer);
  zoomTimer = setTimeout(() => post("/api/camera/zoom", { zoom_x: z }), 60); // firmware range 1.0–2.0×
});

on("btn-idr", "click", () => post("/api/camera/stream", { request_idr: true }, "Keyframe requested"));

on("btn-toggle-transport", "click", () => {
  state.useMjpegFallback = !state.useMjpegFallback;
  const mjpeg = $("mjpeg-fallback");
  if (state.useMjpegFallback) {
    const ws = state.wsVideo;
    state.wsVideo = null;
    if (ws) ws.close();
    fpvCanvas.classList.add("hidden");
    mjpeg.src = `/api/video/mjpeg?t=${Date.now()}`;
    mjpeg.classList.remove("hidden");
    $("btn-toggle-transport").textContent = "MJPEG";
  } else {
    mjpeg.src = "";
    mjpeg.classList.add("hidden");
    fpvCanvas.classList.remove("hidden");
    $("btn-toggle-transport").textContent = "WS Canvas";
    connectVideoWs();
  }
});

export function toggleFullscreen() {
  const stage = $("video-stage");
  if (document.fullscreenElement) document.exitFullscreen();
  else stage.requestFullscreen?.();
}
on("btn-fullscreen", "click", toggleFullscreen);

export function updateProfileHint() {
  const p = state.catalog?.camera_profiles.find((x) => String(x.id) === $("select-cam-profile").value);
  $("cam-profile-hint").textContent = p ? [p.resolution, p.aspect, `${p.fps} fps`, p.note].filter(Boolean).join(" · ") : "";
}
on("select-cam-profile", "change", () => {
  updateProfileHint();
  post("/api/camera/profile", { profile_id: Number($("select-cam-profile").value) }, `Switched to profile #${$("select-cam-profile").value}`);
});
$("select-cam-profile").addEventListener("synced", updateProfileHint);

on("chk-eis", "change", (e) => post("/api/camera/eis", { enabled: e.target.checked, level: 1 }, `EIS ${e.target.checked ? "on" : "off"}`));
on("chk-horizon", "change", (e) => post("/api/camera/horizon_lock", { enabled: e.target.checked }, `Horizon lock ${e.target.checked ? "on" : "off"}`));
on("chk-prerecord", "change", (e) => post("/api/camera/pre_record", { enabled: e.target.checked }, `Pre-record ${e.target.checked ? "on" : "off"}`));

on("slider-ev", "input", (e) => ($("val-ev").textContent = `${Number(e.target.value) > 0 ? "+" : ""}${Number(e.target.value).toFixed(1)}`));
on("slider-wb", "input", (e) => ($("val-wb").textContent = `${e.target.value} K`));
const applyPro = () =>
  post(
    "/api/camera/pro_exposure",
    {
      pro_mode: $("chk-pro-mode").checked,
      ev_compensation: Number($("slider-ev").value),
      wb_kelvin: Number($("slider-wb").value),
      iso: Number($("select-iso").value),
      shutter_ns: Number($("select-shutter").value),
    },
    "Exposure updated"
  );
on("btn-apply-pro", "click", applyPro);
on("chk-pro-mode", "change", applyPro);

on("slider-bitrate", "input", (e) => ($("val-bitrate").textContent = `${e.target.value} kbps`));
on("slider-bitrate", "change", (e) => post("/api/camera/stream", { bitrate_kbps: Number(e.target.value) }, `Bitrate ${e.target.value} kbps`));
on("select-fps", "change", (e) => post("/api/camera/stream", { fps: Number(e.target.value) }, `Stream ${e.target.value} fps`));
const setStreaming = (on_) => {
  touchedAt.set($("stream-toggle"), Date.now());
  $("btn-stream-start").classList.toggle("active", on_);
  $("btn-stream-stop").classList.toggle("active", !on_);
  post("/api/camera/stream", { streaming: on_ }, on_ ? "Stream resumed" : "Stream paused");
};
on("btn-stream-start", "click", () => setStreaming(true));
on("btn-stream-stop", "click", () => setStreaming(false));

on("trajectory-list", "click", (e) => {
  const b = e.target.closest("[data-traj]");
  if (b) post("/api/camera/trajectories/play", { trajectory_id: b.dataset.traj }, `Replaying ${b.textContent.trim()}`);
});
on("btn-traj-stop", "click", () => post("/api/camera/trajectories/stop", {}, "Trajectory stopped"));
