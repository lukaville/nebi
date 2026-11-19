"""`VideoStreamHub`: the simulator's H.265 stream decodes to JPEG frames (PyAV)."""

from __future__ import annotations

import asyncio

from nebi import AsyncNebiClient
from nebi.simulator import MockNebiRobot
from nebi_cockpit.video import VideoStreamHub


async def test_video_hub_decodes_simulator_h265_to_jpeg() -> None:
    sim = MockNebiRobot(ucp_port=0, http_port=0, video_fps=20)
    await sim.start()
    vhub = VideoStreamHub()
    try:
        async with AsyncNebiClient(**sim.client_kwargs()) as robot:
            robot.add_video_listener(vhub.on_h265_access_unit)
            robot.camera.request_keyframe()
            for _ in range(100):
                if vhub.frames_decoded >= 3 and vhub.latest_jpeg is not None:
                    break
                await asyncio.sleep(0.05)
            assert vhub.frames_decoded >= 3
            assert vhub.latest_jpeg is not None and vhub.latest_jpeg.startswith(b"\xff\xd8")
    finally:
        await sim.stop()
