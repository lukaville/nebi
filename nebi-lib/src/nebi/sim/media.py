"""Simulated media album: asset records plus JPEG / MP4 encoding of rendered frames."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import av
from PIL import Image


@dataclass
class SimMediaAsset:
    file_id: str  # decimal string of the firmware's integer index id
    session_id: str
    filename: str
    file_type: str  # "photo" | "video" | "thumbnail" | "lrv" (firmware `media_files.file_type`)
    mime_type: str
    width: int
    height: int
    duration_s: float
    created_at: float
    size_bytes: int
    content: bytes
    highlights: list[float] = field(default_factory=list)
    fps: int = 0

    def firmware_dict(self) -> dict[str, Any]:
        """One `/media/files` item exactly as the retail robot's media server serializes it (times in epoch ms)."""
        is_video = self.file_type == "video"
        dur_ms = round(self.duration_s * 1000) if is_video else None
        dims = self.file_type in ("video", "photo")
        return {
            "id": int(self.file_id),
            "file_name": self.filename,
            "file_type": self.file_type,
            "file_size": self.size_bytes,
            "download_url": f"/files/internal/DCIM/100MONDO/{self.filename}",
            "duration_ms": dur_ms,
            "container_duration_ms": dur_ms,
            "width": self.width if dims else None,
            "height": self.height if dims else None,
            "fps": self.fps if is_video else None,
            "session_id": self.session_id,
            "status": "available",
            "created_at": int(self.created_at * 1000),
            "source_platform": "beni" if dims else "beni_lite",
            "source_sn": "LZ110SIM2026X5" if dims else None,
        }


def encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def encode_mp4(frames: list[Image.Image], fps: int) -> bytes:
    """H.264 MP4 of `frames` (CPU-bound; the simulator calls it on stop-recording, like the old inline code)."""
    mp4_buf = io.BytesIO()
    container = av.open(mp4_buf, mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=fps)
    stream.width = frames[0].width
    stream.height = frames[0].height
    stream.pix_fmt = "yuv420p"
    stream.options = {"preset": "ultrafast", "crf": "26"}
    for idx, im in enumerate(frames):
        vf = av.VideoFrame.from_image(im)
        vf.pts = idx
        for pkt in stream.encode(vf):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    return mp4_buf.getvalue()
