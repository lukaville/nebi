import {
  $,
  api,
  closeModal,
  confirmDialog,
  esc,
  fmtBytes,
  fmtDur,
  icon,
  mediaContentUrl,
  on,
  openModal,
  post,
  run,
  setSegment,
  state,
} from "./core.js";

// Robot photo / video album: gallery grid, viewer modal, delete and format.

// Media
const GALLERY_LIMIT = 200;

// Album states that are not "here are your files": each gets its own card instead of an empty grid.
const MEDIA_STATES = {
  MEDIA_AUTH_REQUIRED: { title: "Robot refused album access", cls: "media-state-error" },
  MEDIA_UNREACHABLE: { title: "Robot media server unreachable", cls: "media-state-warn" },
  MEDIA_STORAGE_BUSY: { title: "Robot storage busy", cls: "media-state-warn" },
  NOT_CONNECTED: { title: "Not connected", cls: "media-state-warn" },
  LINK_LOST: { title: "Not connected", cls: "media-state-warn" },
};

function mediaStateCard(err) {
  const nerr = err.nebiError || {};
  const st = MEDIA_STATES[nerr.code] || { title: "Album unavailable", cls: "media-state-error" };
  const msg = nerr.message || err.message;
  const hint = nerr.hint ? `<div class="media-state-hint">${esc(nerr.hint)}</div>` : "";
  const code = nerr.code ? ` <code>${esc(nerr.code)}</code>` : "";
  return `<div class="empty media-state ${st.cls}" data-media-state="${esc(nerr.code || "ERROR")}">
    <div class="media-state-title">${esc(st.title)}${code}</div><div>${esc(msg)}</div>${hint}</div>`;
}

export async function refreshMediaGallery() {
  const ftype = state.mediaFilter;
  const qs = `?limit=${GALLERY_LIMIT}${ftype ? `&file_type=${encodeURIComponent(ftype)}` : ""}`;
  const [statsRes, listRes] = await Promise.allSettled([api("/api/media/stats"), api(`/api/media/files${qs}`)]);
  if (statsRes.status === "fulfilled") {
    const st = statsRes.value;
    $("media-stats-lbl").textContent = `${st.photos ?? 0} photos · ${st.videos ?? 0} videos · ${fmtBytes(st.total_bytes || 0)}`;
  } else {
    $("media-stats-lbl").textContent = "Album unavailable";
  }
  if (listRes.status === "rejected") {
    state.mediaFiles = [];
    $("media-grid").innerHTML = mediaStateCard(listRes.reason);
    return;
  }
  const files = (state.mediaFiles = listRes.value.files || []);
  $("media-grid").innerHTML = files.length
    ? files
        .map((f, idx) => {
          const url = mediaContentUrl(f.file_id);
          const isVid = f.file_type === "video";
          const thumbId = f.thumbnail_file_id ?? (isVid ? null : f.file_id);
          // Never point a <video> preview at the original: robot recordings are multi-GB 4K files.
          const preview = thumbId
            ? `<img class="media-thumb" src="${mediaContentUrl(thumbId)}" alt="${esc(f.filename)}" loading="lazy" data-idx="${idx}" />`
            : isVid && f.proxy_file_id
            ? `<video class="media-thumb" src="${mediaContentUrl(f.proxy_file_id)}#t=0.1" muted preload="metadata" data-idx="${idx}"></video>`
            : isVid && f.size_bytes < 64 * 1024 * 1024
            ? `<video class="media-thumb" src="${url}#t=0.1" muted preload="metadata" data-idx="${idx}"></video>`
            : `<div class="media-thumb media-thumb-none" data-idx="${idx}">${icon(isVid ? "video" : "image", "i")}</div>`;
          const when = f.created_at ? new Date(f.created_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "";
          // Robot-supplied metadata goes into innerHTML: numbers only (Number() drops anything injected).
          const dur = Number(f.duration_s) || 0, height = Math.round(Number(f.height) || 0), fps = Math.round(Number(f.fps) || 0);
          const extra = isVid && dur ? ` · ${esc(fmtDur(dur))}` : "";
          const res = isVid && height ? ` · ${height >= 2160 ? "4K" : `${height}p`}${fps ? `${fps}` : ""}` : "";
          return `<div class="media-item">
            ${preview}
            ${isVid ? `<span class="media-type">▶ Video${extra}${res}</span>` : ""}
            <div class="media-item-footer">
              <div class="media-meta">
                <div class="media-name" title="${esc(f.filename)}">${esc(f.filename)}</div>
                <div class="media-sub">${fmtBytes(f.size_bytes || 0)}${when ? ` · ${when}` : ""}${f.highlights?.length ? ` · ★${f.highlights.length}` : ""}</div>
              </div>
              <div class="media-actions">
                <a class="btn btn-ghost" href="${url}" download="${esc(f.filename)}" title="Download original">${icon("download")}</a>
                <button class="btn btn-ghost" data-del="${esc(f.file_id)}" data-name="${esc(f.filename)}" title="Delete">${icon("trash")}</button>
              </div>
            </div>
          </div>`;
        })
        .join("")
    : `<div class="empty" data-media-state="EMPTY">No ${ftype ? `${ftype}s` : "media"} on the robot yet. Use <strong>Photo</strong> or <strong>Record</strong> under the video.</div>`;
}

function openMedia(idx) {
  const f = state.mediaFiles[idx];
  if (!f) return;
  state.mediaIndex = idx;
  const url = mediaContentUrl(f.file_id);
  const isVid = f.file_type === "video";
  // Play the robot's low-res proxy (LRV) when there is one; the original stays one click away (download).
  const playUrl = isVid && f.proxy_file_id ? mediaContentUrl(f.proxy_file_id) : url;
  $("media-modal-title").textContent = f.filename || f.file_id;
  $("media-modal-meta").textContent = [
    f.width && f.height ? `${f.width}×${f.height}${f.fps ? ` @ ${f.fps} fps` : ""}` : "",
    fmtBytes(f.size_bytes || 0),
    f.duration_s ? fmtDur(f.duration_s) : "",
    f.created_at ? new Date(f.created_at * 1000).toLocaleString() : "",
    isVid && f.proxy_file_id ? "preview: low-res proxy" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  $("media-modal-download").href = url;
  $("media-modal-download").setAttribute("download", f.filename || f.file_id);
  $("media-modal-body").innerHTML = isVid
    ? `<video src="${playUrl}" controls autoplay></video>`
    : `<img src="${url}" alt="${esc(f.filename)}" />`;
  $("media-prev").classList.toggle("hidden", idx <= 0);
  $("media-next").classList.toggle("hidden", idx >= state.mediaFiles.length - 1);
  openModal("media-modal");
}
export const stepMedia = (d) => openMedia(Math.max(0, Math.min(state.mediaFiles.length - 1, state.mediaIndex + d)));

on("media-grid", "click", async (e) => {
  const thumb = e.target.closest(".media-thumb");
  if (thumb) return openMedia(Number(thumb.dataset.idx));
  const del = e.target.closest("[data-del]");
  if (del) {
    const ok = await confirmDialog("Delete file?", `${del.dataset.name || del.dataset.del} will be removed from the robot.`, "Delete");
    if (!ok) return;
    const res = await run(() => api(`/api/media/files/${encodeURIComponent(del.dataset.del)}`, "DELETE"), `Deleted ${del.dataset.name || del.dataset.del}`);
    if (res !== undefined) refreshMediaGallery();
  }
});
on("media-prev", "click", () => stepMedia(-1));
on("media-next", "click", () => stepMedia(1));
on("btn-media-refresh", "click", refreshMediaGallery);
on("media-filter", "click", (e) => {
  const b = e.target.closest("[data-ftype]");
  if (!b) return;
  state.mediaFilter = b.dataset.ftype;
  setSegment($("media-filter"), (x) => x === b);
  refreshMediaGallery();
});
on("btn-media-close", "click", () => closeModal($("media-modal")));
on("btn-media-format", "click", async () => {
  const ok = await confirmDialog("Format robot storage?", "Every photo and video on Nebi will be permanently erased.", "Format");
  if (!ok) return;
  const res = await post("/api/media/format", {}, "Storage formatted");
  if (res) refreshMediaGallery();
});
