import { $, api, on, state, titleCase, toast, wsUrl } from "./core.js";

// Live video: /ws/video JPEG frames painted on a canvas, the HUD overlay (horizon, reticle, AI target
// boxes) and click-to-track on the video stage.

const VIDEO_RECONNECT_MS = 1200;
const HUD_PITCH_PX_PER_DEG = 2.2;

// ---------------------------------------------------------------- video + HUD

export const fpvCanvas = $("fpv-canvas");
const fpvCtx = fpvCanvas.getContext("2d");
const hudCanvas = $("hud-canvas");
const hudCtx = hudCanvas.getContext("2d");
let renderingFrame = false;

export function connectVideoWs() {
  if (state.wsVideo) {
    try {
      state.wsVideo.close();
    } catch (_) {
      // already closing
    }
    state.wsVideo = null;
  }
  if (state.useMjpegFallback) return;

  const ws = new WebSocket(wsUrl("/ws/video"));
  ws.binaryType = "blob";
  state.wsVideo = ws;

  ws.onmessage = async (ev) => {
    if (renderingFrame || !(ev.data instanceof Blob)) return;
    renderingFrame = true;
    try {
      const bmp = await createImageBitmap(ev.data);
      if (fpvCanvas.width !== bmp.width || fpvCanvas.height !== bmp.height) {
        fpvCanvas.width = bmp.width;
        fpvCanvas.height = bmp.height;
        hudCanvas.width = bmp.width;
        hudCanvas.height = bmp.height;
      }
      fpvCtx.drawImage(bmp, 0, 0);
      bmp.close();
      state.lastFrameAt = Date.now();
      drawHudOverlay();
    } catch (_) {
      // ignore transient frame decode
    } finally {
      renderingFrame = false;
    }
  };

  ws.onclose = () => {
    if (!state.useMjpegFallback && state.wsVideo === ws) {
      setTimeout(connectVideoWs, VIDEO_RECONNECT_MS);
    }
  };
}

export function drawHudOverlay() {
  const w = hudCanvas.width;
  const h = hudCanvas.height;
  hudCtx.clearRect(0, 0, w, h);
  const t = state.telemetry;
  if (!t) return;

  // Artificial horizon
  hudCtx.save();
  hudCtx.translate(w / 2, h / 2);
  const rollRad = ((t.roll_deg || 0) * Math.PI) / 180;
  const pitchPx = (t.pitch_deg || 0) * HUD_PITCH_PX_PER_DEG;
  hudCtx.rotate(-rollRad);
  hudCtx.strokeStyle = "rgba(255, 255, 255, 0.55)";
  hudCtx.lineWidth = 1.5;
  hudCtx.beginPath();
  hudCtx.moveTo(-70, pitchPx);
  hudCtx.lineTo(-22, pitchPx);
  hudCtx.moveTo(22, pitchPx);
  hudCtx.lineTo(70, pitchPx);
  hudCtx.stroke();
  hudCtx.restore();

  // Center reticle
  hudCtx.strokeStyle = "rgba(255, 255, 255, 0.5)";
  hudCtx.lineWidth = 1.25;
  hudCtx.beginPath();
  hudCtx.arc(w / 2, h / 2, 4, 0, Math.PI * 2);
  hudCtx.stroke();

  // AI target boxes
  for (const tgt of t.targets || []) {
    const bw = tgt.width * w;
    const bh = tgt.height * h;
    const bx = tgt.cx * w - bw / 2;
    const by = tgt.cy * h - bh / 2;
    const color = tgt.locked ? "#12b76a" : "#5b8def";

    hudCtx.strokeStyle = color;
    hudCtx.lineWidth = tgt.locked ? 2.5 : 1.75;
    hudCtx.beginPath();
    if (hudCtx.roundRect) hudCtx.roundRect(bx, by, bw, bh, 6);
    else hudCtx.rect(bx, by, bw, bh);
    hudCtx.stroke();

    const tag = `${tgt.locked ? "● " : ""}${titleCase(tgt.label)} #${tgt.target_id} · ${tgt.distance_m.toFixed(1)} m`;
    hudCtx.font = "600 11px -apple-system, BlinkMacSystemFont, sans-serif";
    const tw = hudCtx.measureText(tag).width + 14;
    const ty = Math.max(2, by - 22);
    hudCtx.fillStyle = tgt.locked ? "#12b76a" : "rgba(255, 255, 255, 0.95)";
    hudCtx.beginPath();
    if (hudCtx.roundRect) hudCtx.roundRect(bx, ty, tw, 19, 9.5);
    else hudCtx.rect(bx, ty, tw, 19);
    hudCtx.fill();
    hudCtx.fillStyle = tgt.locked ? "#ffffff" : "#101828";
    hudCtx.fillText(tag, bx + 7, ty + 13.5);
  }
}

/** Map a click on the stage into normalized frame coords, honoring object-fit: contain letterboxing. */
function stagePointToFrame(ev) {
  const rect = $("video-stage").getBoundingClientRect();
  const fw = hudCanvas.width || 16;
  const fh = hudCanvas.height || 9;
  const scale = Math.min(rect.width / fw, rect.height / fh);
  const dw = fw * scale;
  const dh = fh * scale;
  const ox = rect.left + (rect.width - dw) / 2;
  const oy = rect.top + (rect.height - dh) / 2;
  return {
    x: Math.max(0, Math.min(1, (ev.clientX - ox) / dw)),
    y: Math.max(0, Math.min(1, (ev.clientY - oy) / dh)),
  };
}

on("video-stage", "click", async (ev) => {
  const { x, y } = stagePointToFrame(ev);
  const hit = (state.telemetry?.targets || []).find(
    (t) => Math.abs(x - t.cx) <= t.width / 2 && Math.abs(y - t.cy) <= t.height / 2
  );
  try {
    const res = hit
      ? await api("/api/tracking/lock_target", "POST", { target_id: hit.target_id })
      : await api("/api/tracking/lock_point", "POST", { x, y });
    toast(`Locked target #${res.selected_id || hit?.target_id || "?"}`);
  } catch (e) {
    toast(`Track failed: ${e.message}`, "error");
  }
});
