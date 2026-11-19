# `nebi-cockpit` — Web Control & Live FPV Cockpit compatible with the Beni Robot

> [!WARNING]
> **Independent, unofficial project.** nebi is not affiliated with, endorsed by or supported by the manufacturer of
> the Beni robot. "Beni" and the model names LZ110 / LZ100 are used only to identify the compatible hardware.
>
> - Use it only with a robot **you own**. Using it may **void your warranty** or violate the manufacturer's terms.
> - It **physically moves the robot.** Supervise the robot and keep a clear space around it: the software can make it
>   fall over, jump, or drive into people, pets and things.
> - **Firmware update and factory reset** commands can brick the device or wipe its data.
> - Provided **AS IS, without warranty** of any kind (Apache-2.0, see the LICENSE and NOTICE files).

FastAPI control server + HTML5 cockpit compatible with the Beni robot (LZ110/LZ100), built on the `nebi` SDK (part
of the nebi monorepo). It owns the single robot link (the robot accepts one UCP client), decodes the H.265
FPV stream, bridges the two-way intercom and hosts extensions — including the `nebi` agent API when
`nebi-cli` is installed.

Depends on: `nebi-lib[sim]`. Never imports `nebi_cli` (enforced by `../tests/test_architecture.py`).

## Run

```bash
cd nebi
.venv/bin/nebi-cockpit serve --host 127.0.0.1 --port 8000          # no robot yet: connect from the menu
.venv/bin/nebi-cockpit serve --sim                                 # built-in simulator instead of a robot
.venv/bin/nebi-cockpit serve --disable-extension nebi-cli          # without the `nebi` agent API
.venv/bin/nebi-cockpit serve --no-extensions                       # without any extension
```

`nebi-cockpit` with no command is `nebi-cockpit serve`; `python -m nebi_cockpit` works too, as does
`uvicorn nebi_cockpit.app:create_app --factory`. Defaults come from `PYNEBI_HOST` / `PYNEBI_PORT`.

Open **`http://127.0.0.1:8000`**:
- Drive with **dual virtual joysticks**, **WASD + Arrow keys** (`Space` to Jump), or an **Xbox / PlayStation Gamepad**.
- Watch the live **Annex-B H.265 -> WebSocket Canvas / MJPEG** FPV stream with real-time attitude HUD and **AI person/pet bounding boxes** (click any box or point on the video to lock & track).
- Trigger **Stand, Fold, E-Stop, Jump, 4-way Flips, 180° Spin, Auto Dock/Undock, Pet mode** (only starts while standing still; the cockpit reports whether the robot entered it), **Video Recording**, **46 Camera Profiles**, **5 Interactive Games**, **Media Gallery**, **UWB remote vibration**, **HMS health faults** (live raise/clear from `/status/hms/event_broadcast`, recovery hints, event log), **Wi-Fi scan/connect**, and **OTA uploads**.
- Click the **connection chip** (e.g. `DISCONNECTED`) in the top bar to open **Connect to Nebi**, a plain-language menu (default: the flow used last from this browser, else Home Wi-Fi):
  - **Home Wi-Fi** (default): Nebi is already on your Wi-Fi. Enter its IP address, or leave it blank to search your network.
  - **Wake up over Bluetooth and connect to Home Wi-Fi**: use this after every power-on, because Nebi does not rejoin your Wi-Fi by itself. Keep
    Nebi near the computer and enter your Wi-Fi name and password (the first time, hold Nebi's button for 3–5 seconds
    to approve pairing). The computer briefly joins Nebi's hotspot, hands over your Wi-Fi details, returns to your
    Wi-Fi and connects.
  - **Nebi's hotspot**: this computer is already joined to Nebi's own Wi-Fi hotspot (no internet while on it).
  - **Hotspot via Bluetooth**: pairs over Bluetooth to read the hotspot name and password, and can move this computer
    onto the hotspot for you.
  - **Simulator**: a built-in virtual robot for trying the cockpit without hardware.
  - Under *Advanced*: network interface, ports, auto-reconnect and *Try everything automatically* (home Wi-Fi, then
    the hotspot, then Bluetooth).
  - **USB-C cable**: Nebi plugged into this computer with a USB-C data cable shows up as a network adapter
    (192.168.123.1). Some robots or firmware versions only charge over USB-C; then use Wi-Fi.

**Intercom in the browser:** Sound tab → *Intercom* → *Start intercom*, then hold *Hold to talk* (or the `T` key), or
switch to *Open mic* for hands-free full duplex. The microphone requires `http://127.0.0.1`/`localhost` or HTTPS;
elsewhere the card is listen-only.

**Intercom test from the terminal:** with the cockpit running and connected, `nebi tools say "Hello from the intercom"`
(nebi-cli) speaks the text through Nebi's speaker (the default `nebi.tts` engine — the OS's own speech via the built-in `system` engine — → the same `/ws/intercom`
path the browser uses). `--voice NAME` picks an OS voice, `--engine` another engine, `--wav file.wav` plays an audio
file (decoded with PyAV, no ffmpeg needed), `--gain 2` boosts it.

**Exposed beyond loopback** (`--host 0.0.0.0`): remote peers must present a token (printed at start; `PYNEBI_TOKEN`
chooses it; header `X-Pynebi-Token`, `?token=` or cookie). Loopback peers never need it; Host/Origin checks always apply.

## Layout

```
src/nebi_cockpit/app.py                create_app(): manager, routers, security middleware, static, extensions
                 connection_manager.py NebiServerManager: the single robot link, simulator, video hub, link stages
                 routes_*.py           REST (connection, motion, camera, media, system); deps.py, schemas.py, errors.py
                 ws_routes.py          /ws/control (dead-man joystick), /ws/video, /ws/intercom
                 intercom_ws.py        browser PCM <-> robot Opus bridge;  video.py  H.265 -> JPEG worker
                 security.py           Host (DNS rebinding) / Origin (CSWSH, CSRF) / token checks
                 extensions.py         ServerExtensionContext (the cockpit's extension host)
                 cli.py                `nebi-cockpit serve`
                 static/               the cockpit page (ES modules under static/js, intercom worklets, agent.js pill)
docs/web-architecture.md               end-to-end architecture
```

**Safety invariants (keep them).** Dead-man layers 1–2 of the joystick path live here: the page keeps re-sending a
held stick at least every 80 ms, and `/ws/control` applies neutral even while the link is down (layers 3–5 are in
nebi: `MotionController` axis expiry, UCP heartbeat hold word, hold word + bit-59 latch per session).

## Extensions (server side)

Extensions are discovered by nebi's loader (entry-point group `nebi.extensions`, see the
nebi README, section *Extensions*). `create_app` calls each one's `setup_server(ctx)` with a
`nebi_cockpit.extensions.ServerExtensionContext`:

- `include_router(router, **kw)`, `mount_static(path, directory)`, `add_ui_module(url, styles=...)`,
  `add_allowed_origins(*origins)`;
- `add_lifespan(factory)`: `factory(app)` → async context manager, entered after the robot link starts and exited
  before it stops (a failing one is reported, not fatal);
- `ctx.app`, `ctx.manager` (the `NebiServerManager`), `ctx.port` (the served port when known).

UI modules use `window.NebiUI.registerTab(...)` / `registerView(...)` (see `docs/web-architecture.md` and
`static/js/extensions.js`). `GET /api/extensions` lists the loaded extensions, their errors, commands and UI modules.

**The `nebi` agent API is such an extension:** nebi-cli declares the entry point `nebi-cli`, whose `setup_server`
mounts `/agent/call`, `/agent/ping` and `/agent/activity` and runs the agent runtime for the app's lifetime, so the
`nebi` CLI drives through this cockpit's robot link. The cockpit's "agent driving" pill (`static/agent.js`) stays
hidden when those routes are absent.

## Development

```bash
cd nebi/nebi-cockpit && "$PWD/../.venv/bin/python" -m pytest -q
```

Lint/types: see the monorepo's root README.
