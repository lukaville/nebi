# nebi-cockpit: SDK, Simulator & Web Cockpit Architecture

## 1. End-to-End Architecture

```mermaid
flowchart LR
    subgraph Browser["Browser FPV Cockpit"]
        UI["HTML5 Cockpit UI<br/>Virtual Sticks + WASD + Gamepad"]
        Canvas["FPV Canvas + AI Target HUD"]
    end

    subgraph Server["FastAPI Control & Video Server (nebi_cockpit)"]
        WS["/ws/control (JSON)<br/>/ws/video (JPEG)<br/>/ws/intercom (PCM 48k, 2-way)"]
        REST["REST /api/{motion,camera,tracking,games,audio,media,system}"]
        VHub["VideoStreamHub<br/>(PyAV HEVC Decoder -> JPEG/MJPEG)"]
    end

    subgraph SDK["AsyncNebiClient (nebi)"]
        Domains["7 Domain Controllers<br/>motion · camera · tracking<br/>audio · games · media · system"]
        ZBus["ZenohTopicBus + ZenohUcpClientTunnel<br/>(0xA4..0xA7 + MD-TS AES-256-GCM)"]
        UCP["UcpClient (:9007 UDP)<br/>ARQ + 0x05 SACK + Reliable H.265/Opus"]
        HTTP["httpx Clients<br/>Config :8010 · Media HTTPS :8084 · OTA :8085"]
    end

    subgraph Target["Robot Target (Live Switchable)"]
        Sim["MockNebiRobot Simulator<br/>UcpServer + Zenoh + MD-TS + libx265 FPV"]
        Real["Physical Nebi Robot<br/>Home LAN STA / 192.168.7.1 / 192.168.123.1"]
    end

    UI <-->|"25 Hz Joy / 15 Hz Telemetry"| WS
    Canvas <-- "Binary JPEG / MJPEG" --- WS
    UI <--> REST
    WS <--> Domains
    REST <--> Domains
    VHub --> WS
    UCP -->|"Annex-B H.265 AUs"| VHub
    Domains --> ZBus
    Domains --> HTTP
    ZBus <-->|"Ch 3 / Ch 0"| UCP
    UCP <-->|"UDP :9007"| Sim
    UCP -.->|"UDP :9007"| Real
    HTTP <--> Sim
    HTTP -.-> Real
```

---

## 2. Async Python SDK (`AsyncNebiClient`)

`AsyncNebiClient` ([nebi-lib/client.py](../../nebi-lib/src/nebi/client.py)) provides a single async context manager wrapping all transports and 7 domain controllers:

| Attribute | Class | Responsibilities |
|---|---|---|
| `robot.motion` | `MotionController` | 50 Hz `RcJoystick` stream, `stand()`, `sit()`, `emergency_stop()`, `jump()`, 4-way `flip()`, `turn_around()`, `speed_gear`, `set_mission()`, `return_to_dock()`, `undock()`, `start_ego_mode()` |
| `robot.camera` | `CameraController` | `start_live_stream()`, `stop_live_stream()`, `request_keyframe()`, `take_photo()`, `start_recording()` / `stop_recording()`, `mark_highlight()`, `set_zoom()`, `set_eis()`, `set_horizon_lock()`, `set_profile()` (46 sensor profiles), `set_pro_exposure()`, `list_trajectory_replays()` |
| `robot.tracking` | `TrackingController` | `lock_target(id)`, `lock_point(x, y)` click-to-track, `unlock_target()`, `set_follow_mode()` (`TRAIL`, `PARALLEL`, `LEAD`, `ORBIT`, `SPOTLIGHT`), `switch_target_type()`, `set_pnl_mode()` |
| `robot.intercom` | `Intercom` | Full-duplex intercom: `start()` / `stop()` (`/cmd/audio/op_req` `0`/`1`), `send_pcm()` / `send_opus()` (Zenoh `/data/audio/playback_data`), `add_pcm_listener()` / `add_opus_listener()` (UCP Ch 2 robot mic), auto re-arm after reconnect |
| `robot.audio` | `AudioController` | `send_intercom_opus()` (shim → `robot.intercom`), `play_sound_effect()` (8 built-in `/md/etc/.../*.wav` + horn presets), `play_audio_file()` (`/cmd/audio/play_file_req`), `set_voice_wakeup()`, `scan_wireless_mics()`, `select_mic_source()`, `forget_wireless_mic()` |
| `robot.games` | `GamesController` | `start_game()`, `pause_game()`, `resume_game()`, `restart_game()`, `stop_game()`, `send_player_input()` across all 5 robot games |
| `robot.media` | `MediaController` | `stats()`, `list_sessions()`, `list_files()`, `download_bytes()` (HTTP `Range` streaming, auto-upgrading `:8084` `http://` → self-signed `https://`), `save_file()`, `delete_file()`, `format_storage()` |
| `robot.system` | `SystemController` | `ping()`, `get_version()`, `get_serial_number()`, `get_parameters()` / `set_parameters()` (`:8010`), eye LEDs `set_color()` / `play_expression()` / `release()` / `eyes_status()` (also `robot.eyes`; headerless `/cmd/led/ctrl_oneway` `[u32; 14]` frames, 25 Hz animations, 5 Hz colour holds; legacy `set_led_rgbw()` / `set_led_expression()`), `vibrate_remote()`, `clear_hms_fault()`, `scan_wifi()`, `connect_wifi()`, `provision_home_wifi_and_handoff()`, `start_ota_upgrade()` |

---

## 3. Video Pipeline (`VideoStreamHub`)

1. **Robot / Simulator TX**: Transmits fragmented **Annex-B H.265 (HEVC)** Access Units over UCP Channel 1 (`reliable=1` `0x0101` on physical `LZ110` hardware at `960x720@53–60fps`, or `0x0001` unreliable; `MORE_FRAGMENTS = 0x0200` on non-terminal UDP datagrams, `0x0000` on final fragment).
2. **`UcpEndpointSession` RX**: Reassembles complete Access Units up to 2 MB across both reliable and unreliable Channel 1 streams and scans NAL headers (`nal_type in {16..21, 32, 33}`) for IRAP/parameter-set keyframe identification.
3. **`VideoStreamHub` ([video.py](../src/nebi_cockpit/video.py))**:
   - Decodes every HEVC packet in-process using `PyAV` (`av.CodecContext.create("hevc", "r")`) in `<1 ms` per frame (preserving inter-frame reference pictures) while pacing RGB → JPEG conversion (~35 fps with active WebSocket viewers, 4 fps idle).
   - Fans out simultaneously to:
     - **`WS /ws/video`**: Binary JPEG blobs rendered directly onto `<canvas>` via `createImageBitmap()` with bounded queue drop (`maxsize=2`) so latency never accumulates.
     - **`GET /api/video/mjpeg`**: Standard `multipart/x-mixed-replace` stream for `<img>` tags or external tools.
     - **`GET /api/video/snapshot.jpg`**: Instant single-frame JPEG snapshot.

---

## 4. `MockNebiRobot` Protocol Simulator

`MockNebiRobot` ([nebi-lib/sim](../../nebi-lib/src/nebi/sim/)) runs the **real robot wire stack** locally without shortcuts:
- `UcpServer` + `ZenohUcpServerTunnel` (`0xA4..0xA7`) + `eclipse-zenoh` peer session (`device/**` ↔ `app/**`)
- Embedded HTTP server implementing `:8010/config/v1/local/*`, `:8084/media/*` (with HTTP `206 Partial Content` `Range` support), and `:8088` OTA `/upload`
- 24 Hz 3D perspective arena renderer (`Pillow`) + real-time `libx265` (`zerolatency`) HEVC encoder streaming over UCP Channel 1, projecting moving `person` (`#101`) and `pet` (`#202`) actors into live `/data/tracking/target_data` bounding boxes and recording real playable `.mp4` videos and `.jpg` photos into the media gallery.


## Two-Way Intercom (`/ws/intercom`)

```
Browser mic ─getUserMedia─▶ AudioWorklet "nebi-mic" (resample → 48 kHz s16 960-sample frames)
   └─ WS binary ─▶ IntercomHub (nebi_cockpit/intercom_ws.py) ─▶ Intercom.send_pcm ─▶ OpusEncoder (PyAV libopus, VOIP, FEC)
         ─▶ Zenoh app/data/audio/playback_data (AudioPlaybackData, seq_num_audio from 0) ─▶ robot audio service ─▶ speaker

Nebi mic ─▶ robot audio service (only after op_req 0 = START_RECORD) ─▶ Opus ─▶ UCP Ch 2 reliable
   ─▶ Intercom (OpusDecoder) ─▶ WS binary PCM ─▶ AudioWorklet "nebi-speaker" (60 ms jitter buffer, resample) ─▶ speakers
```

- The browser only handles PCM, so no WebCodecs `AudioEncoder` is required (works in Chrome, Firefox and Safari). Costs ~768 kbit/s per direction between browser and cockpit server, which is fine on localhost/LAN; the robot link itself carries ~32 kbit/s Opus.
- `IntercomHub` shares the robot's single intercom across browser tabs: the robot mic is switched off when the last tab leaves, and only one tab can talk at a time (each talk press restarts `seq_num_audio` at `0`, which resets the robot audio service's jitter buffer).
- UI: Sound tab → **Intercom** card. *Push to talk* (default; hold the button or `T`) or *Open mic* (full duplex; `T` toggles mute). The mic needs a secure context (`http://127.0.0.1` / `localhost` or HTTPS); over plain-HTTP LAN addresses the card falls back to listen-only.
- REST: `POST /api/audio/intercom {"enabled": bool}`, `GET /api/audio/intercom` (status).
- Eyes (`routes_eyes.py`, `static/js/eyes.js`, Sound tab "Eyes" card): `GET /api/eyes`, `POST /api/eyes/color {hex|r,g,b,w, hold_sec|null, brightness}`, `POST /api/eyes/expression {name, loops (0 = until release), brightness}`, `POST /api/eyes/release` (stop sending; the firmware eyes come back).
- Simulator: `MockNebiRobot` implements the robot audio service's behaviour (op codes, seq-0 sessions, decoding the downlink) and streams a robot-mic uplink with room tone, a chirp every 3 s and a ~360 ms echo of whatever you say, so the round trip can be heard without hardware.

## Security and safety (2026-11-18)

- **Who may call the cockpit:** a middleware (`nebi_cockpit/security.py`) checks the `Host` header (DNS rebinding) and the
  `Origin` of every WebSocket and state-changing request. Allowed origins are the same host:port, its loopback
  aliases, origins registered by extensions and `PYNEBI_ALLOWED_ORIGINS`.
  Clients that send no Origin (the `nebi`
  CLI, curl, nebi drive) pass. A non-loopback `--host` requires a token (`PYNEBI_TOKEN` or a generated one, via
  `X-Pynebi-Token`, `?token=` or a cookie); loopback peers never need it.
- **Dead-man layers:**
  1. The browser re-sends a held stick every ≤80 ms and sends neutral on release, blur or hidden tab.
  2. The server `/ws/control` dead-man applies neutral even while the link is down.
  3. `MotionController` expires stick axes 0.5 s after the last `set_axes`.
  4. The UCP heartbeat falls back to a hold word (sticks neutral, triggers cleared, latch kept) 0.25 s after the
     last fresh joystick word.
  5. Each new session starts from a hold word with the bit-59 latch re-read from disk.
- `/ws/audio`, `/ws/h265` and `/api/audio/record_toggle` were removed (no consumers).
- Run with uvicorn directly via `uvicorn nebi_cockpit.app:create_app --factory`.

## Extensions in the cockpit

`GET /api/extensions` lists the loaded nebi extensions (name, version, ok, errors, commands, `ui_modules`,
`styles`). `static/js/main.js` imports every listed UI module after the core UI is up (`static/js/extensions.js`);
a module adds UI only through `window.NebiUI`: `registerTab({id, title, icon, html, onShow})` (side-panel tab, pane
`#tab-<id>`, System stays last, number keys follow the tab order) and `registerView({id, label, element, fullStage,
onShow, onHide})` (a video-stage view next to Camera; the stage gets `view-<id>`, key `M` cycles, the choice is
remembered). A module that fails to import is reported with `console.warn` and a toast. Server-side, extensions add
routers, static mounts (`/ext/<name>`) and allowed origins through `ServerExtensionContext` (`nebi_cockpit/extensions.py`).
