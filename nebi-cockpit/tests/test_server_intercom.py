"""Cockpit `/ws/intercom` bridge (browser PCM <-> robot Opus) against the simulator."""

from __future__ import annotations

import array
import json
import math

from nebi.intercom import INTERCOM_FRAME_BYTES, INTERCOM_FRAME_SAMPLES


def _tone(n_frames: int, freq: float = 440.0, amp: int = 9000) -> bytes:
    total = n_frames * INTERCOM_FRAME_SAMPLES
    return array.array("h", [int(amp * math.sin(2 * math.pi * freq * i / 48000)) for i in range(total)]).tobytes()


def test_intercom_websocket_bridge() -> None:
    from fastapi.testclient import TestClient

    from nebi_cockpit.app import create_app

    app = create_app(start_simulator=True)
    with TestClient(app) as tc:
        assert tc.get("/api/health").json()["connected"] is True
        with tc.websocket_connect("/ws/intercom") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["sample_rate"] == 48000
            assert hello["frame_samples"] == INTERCOM_FRAME_SAMPLES

            ws.send_json({"type": "start"})
            state: dict = {}
            for _ in range(10):
                state = ws.receive_json()
                if state.get("type") == "state" and state.get("joined"):
                    break
            assert state["active"] is True and state["connected"] is True

            pcm = _tone(10)

            got = 0
            for _ in range(200):
                msg = ws.receive()
                if msg.get("bytes"):
                    assert len(msg["bytes"]) == INTERCOM_FRAME_BYTES
                    got += 1
                    if got >= 5:
                        break
            assert got >= 5  # robot mic -> browser

            ws.send_json({"type": "talk", "on": True})
            ws.send_bytes(pcm)  # browser mic -> robot speaker (only while talking)
            tx = 0
            for _ in range(400):
                msg = ws.receive()
                if msg.get("text"):
                    st = json.loads(msg["text"])
                    if st.get("type") == "state":
                        assert st["talking"] is True
                        tx = st.get("tx_frames", 0)
                        if tx >= 10:
                            break
            assert tx >= 10  # 10 x 20 ms frames encoded and published on /data/audio/playback_data

            ws.send_json({"type": "stop"})

