# `nebi` — Async Python SDK & Simulator compatible with the Beni Robot

> [!WARNING]
> **Independent, unofficial project.** nebi is not affiliated with, endorsed by or supported by the manufacturer of
> the Beni robot. "Beni" and the model names LZ110 / LZ100 are used only to identify the compatible hardware.
>
> - Use it only with a robot **you own**. Using it may **void your warranty** or violate the manufacturer's terms.
> - It **physically moves the robot.** Supervise the robot and keep a clear space around it: the software can make it
>   fall over, jump, or drive into people, pets and things.
> - **Firmware update and factory reset** commands can brick the device or wipe its data.
> - Provided **AS IS, without warranty** of any kind (Apache-2.0, see the LICENSE and NOTICE files).

`nebi` is a complete, protocol-verified `asyncio` Python SDK and real-time H.265 robot simulator compatible with the **Beni** (`lz110` / `lz100`) two-legged wheeled self-balancing robot. It is a pure library: the web cockpit lives in the `nebi-cockpit` package, the `nebi` command line in `nebi-cli` (all three are part of the nebi monorepo; relative paths below refer to it).

It speaks the robot's local protocols (UDP control/video/audio link, encrypted pub/sub bus, local HTTP services),
ships a simulator so everything runs without hardware, and is tested against a physical `LZ110`.

---

## Documentation

- `nebi-cockpit/docs/web-architecture.md` (in the monorepo) — `AsyncNebiClient`, `MockNebiRobot`, `VideoStreamHub`, and web cockpit architecture
- API reference: the docstrings (`help(nebi.AsyncNebiClient)`); agent manual: `nebi guide`

---

## Quick Start

### 0. Pair a Retail Nebi over Bluetooth LE & Switch It to Your Home Wi-Fi

Retail `LZ110` / `LZ100` robots use an encrypted **`MD-TS`** X25519 + AES-256-GCM transport across both BLE (`0x55`/`0x56`) and Zenoh-over-UCP (`app/security/session` ↔ `device/security/session`), and the official mobile app does not expose a UI button to switch Nebi onto your Home Wi-Fi router.

> **Every power-on:** Nebi does not rejoin the home Wi-Fi by itself — it only advertises BLE. Run the BLE → Wi-Fi
> provisioning below after each boot (`nebi setup-wifi` with no arguments uses `~/.config/pynebi/wifi.json`
> `{"ssid": …, "password": …}` when present), then reconnect the cockpit (Connection dialog or `POST /api/connection/retry`).

Close/disconnect the official app on your phone (so the robot resumes BLE `0000FFF0` advertising), then run (the command
line tools are in `nebi-cli`; in Python: `AsyncNebiClient.pair_ble_and_provision_home_wifi(...)`):

```bash
cd nebi
# Pairs nebi's persistent X25519 identity (~/.config/pynebi/identity.json) over BLE
# (hold Nebi's physical button for 3-5 seconds when prompted on first pairing),
# provisions Nebi onto your Home Wi-Fi, restores your Mac/PC Wi-Fi, and discovers Nebi's new LAN IP:
.venv/bin/nebi setup-wifi --ssid "YourHomeWiFi" --password "YourWiFiPassword"
```

Or just pair BLE and inspect Nebi's SoftAP credentials + X25519 public key without changing Wi-Fi:

```bash
.venv/bin/nebi pair-ble
.venv/bin/nebi tools discover --deep-lan
```

### 1. Use `AsyncNebiClient` in Python

```python
import asyncio
from nebi import AsyncNebiClient, CameraSubMode, StuntDirection

async def main() -> None:
    # Connect to physical Nebi on SoftAP (or pass **sim.client_kwargs() for simulator)
    async with AsyncNebiClient("192.168.7.1") as robot:
        # Subscribe to live Annex-B H.265 video frames
        robot.add_video_listener(
            lambda au, is_kf, ts_us: print(f"H.265 AU: {len(au)}B keyframe={is_kf}")
        )

        # Ping & inspect telemetry
        print(await robot.system.ping())
        await robot.motion.stand()

        # Drive forward at 50% speed while panning gimbal up
        robot.motion.set_axes(ly=0.5, ry=0.2)
        await asyncio.sleep(1.5)
        robot.motion.set_axes(ly=0.0, ry=0.0)

        # Stunts (joystick-word triggers)
        await robot.motion.jump(height=0.8)
        await robot.motion.flip(StuntDirection.BACK_FLIP)

        # Camera & AI Vision Follow
        await robot.camera.set_zoom(1.5)  # firmware range 1.0-2.0x
        await robot.camera.start_recording()  # no still photos on LZ110 firmware: record clips instead
        await robot.camera.stop_recording()  # action=2, confirmed via camera status
        await robot.tracking.lock_point(0.5, 0.5)
        await robot.tracking.set_follow_mode(CameraSubMode.FOLLOW_ORBIT, distance_m=2.0)

        # Browse captured media via HTTP :8084
        album = await robot.media.list_files(limit=10)
        print("Media files:", [f["filename"] for f in album["files"]])

        # Two-way intercom: s16le 48 kHz mono PCM in both directions (Opus handled for you)
        robot.intercom.add_pcm_listener(lambda pcm: print(f"Nebi mic: {len(pcm)} bytes"))
        await robot.intercom.start()          # robot mic on (/cmd/audio/op_req 0 = START_RECORD)
        robot.intercom.send_pcm(my_pcm_chunk)  # → Nebi's speaker via /data/audio/playback_data
        await robot.intercom.stop()

asyncio.run(main())
```

---

## Architecture

Dependencies only point downwards; simulator code is never imported by production modules.

```
types, exceptions, catalog         enums, JoystickState wire layout (single source), telemetry dataclasses, errors
transport/  md_ts/  ble  network/  UCP codec + ARQ (ucp_codec, ucp), Zenoh tunnel/bus/RPC/security (zenoh_*),
                                   MD-TS crypto + identity, BLE pairing, discovery / interfaces / OS Wi-Fi, bit-59 latch
domains/                           one controller per robot area (motion, camera, audio, led, wifi, system, tracking,
                                   games, media), all `DomainController`s that survive reconnects via rebind()
client  flows  onboarding          AsyncNebiClient facade (session lifecycle, reconnect, listeners), connection
telemetry                          strategies (@register_flow), BLE onboarding, topic decoders (@topic_decoder)
tts  intercom                      pluggable TTS engines (`nebi say`), full-duplex Opus intercom
extension_loader  extensions       generic extension discovery (entry points / PYNEBI_EXTENSIONS) and loading; the
                                   hosts of the hooks are nebi-cli (agent) and nebi-cockpit (server/UI)
sim/                               MockNebiRobot and robot-side peers (UcpServer, MD-TS authority, mock BLE, topics,
                                   physics, rendering, HTTP media); `nebi.simulator` re-exports it. Extra `nebi-lib[sim]`
                                   (its mock HTTP daemons use FastAPI/uvicorn; nothing else in nebi does)
```

nebi never imports `nebi_cockpit` or `nebi_cli` (enforced by `../tests/test_architecture.py`).

**Extension points.**
- New connection flow: `@register_flow(ConnectionFlow.X)` in `flows.py`.
- New telemetry topic: `@topic_decoder("/status/...")` in `telemetry.py`.
- New robot area: a `DomainController` subclass wired in `AsyncNebiClient`.
- New `nebi` command: see the nebi-cli README.
- Anything outside the core (other sensors, maps, cockpit tabs): a separate package using the extension API below.

**Safety invariants (keep them).** The joystick path has five independent dead-man layers (1–2 live in
nebi-cockpit, 3–5 here):
1. The cockpit keeps re-sending a held stick at least every 80 ms.
2. `/ws/control` applies neutral even while the link is down.
3. `MotionController` expires stick axes after 0.5 s.
4. The UCP heartbeat falls back to a hold word after 0.25 s: sticks neutral, triggers cleared, latch kept.
5. Every session starts from the hold word plus the persisted bit-59 latch.

Bit 59 (stand/fold) is a latched switch. Every frame must carry its current value, and `stand()` needs a fresh posture
report.

## Development

Part of the nebi monorepo: one venv at `nebi/.venv`, see the monorepo's root README.

```bash
cd nebi/nebi && "$PWD/../.venv/bin/python" -m pytest -q   # unit + simulator integration tests
```

`import nebi` is lazy: names are resolved on first use, so the `nebi` CLI starts without loading BLE, Zenoh or PyAV.

## Media gallery access

The robot's media server (HTTPS `:8084`, photos/videos on the internal storage) only answers requests carrying a
static bearer token that is the same on every retail robot. **nebi does not ship that token.** Provide it yourself:

```bash
mkdir -p ~/.config/pynebi
printf '%s\n' '<token>' > ~/.config/pynebi/media_token
chmod 600 ~/.config/pynebi/media_token
# or, per process: export PYNEBI_MEDIA_TOKEN='<token>'
```

Lookup order: `MediaHttpClient(token=...)` > `$PYNEBI_MEDIA_TOKEN` > `~/.config/pynebi/media_token`. Without one, media
calls fail with `MEDIA_TOKEN_MISSING`; a wrong one gives `MEDIA_AUTH_REQUIRED`, whose `details.token.matches_retail_firmware`
says whether your token matches the SHA-256 digest the firmware checks (`nebi.http.media_support.token_matches_firmware`).
The bundled simulator accepts the public `SIM_MEDIA_TOKEN`, so tests and `--sim` need no setup.

## Text-to-speech

`nebi say` (nebi-cli; and the babble of `emote` / `body` / `idle`) renders speech with a pluggable engine (`nebi.tts`).
Two engines are built in:

- `chirp` (numpy only) makes emotion-shaped robot chirps — babble, or one chirp per syllable of the text — so speech
  always works (priority 0).
- `system` (`nebi.tts_system`, no extra dependencies) speaks real words with the OS's own speech: macOS `say`,
  Windows System.Speech (SAPI, via PowerShell), Linux `espeak-ng` / `espeak` / `pico2wave`. It is registered only when
  such a backend exists (priority 5: above `chirp`, below extension engines; `PYNEBI_TTS_SYSTEM=0` disables it).
  Presets: `default` (the OS default voice), then the installed voices of the current locale's language; any other
  installed voice is accepted by name (`--preset Zarvox`, `--preset en-us+m3`). Emotions (same names as `chirp`)
  change speaking rate and pitch; babble is delegated to `chirp`. Text goes to the backend on stdin / in an env var,
  output is a 16-bit WAV read with the stdlib (no ffmpeg); subprocesses have timeouts.

Selection: `nebi say --engine NAME`, else `PYNEBI_TTS_ENGINE`, else the registered engine with the highest `priority`
(default 10). `nebi.tts.read_audio_file(path)` decodes any audio file (PyAV) to `SpeechAudio`. Emoji in the text pick the emotion;
night/far-listener/low-battery context, speech priority over idle babble and `nebi stop` work for every engine.
If an engine raises, a built-in robot sound effect plays instead.

An engine implements `TtsEngine`: `name`, `emotions`, `presets` (first = default), optional `priority`, and
`synthesize(text, *, emotion, babble, preset) -> SpeechAudio` (mono float32/int16 PCM + sample rate, `text`,
`emotion`, `mode`, and `beats` = (time_s, strength) per syllable/word, used for gimbal nods; the eye-pulse
envelope is computed from the audio). `synthesize` runs in a worker thread. Adapter around any TTS library:

```python
import numpy as np

from nebi.tts import EMOTIONS, SpeechAudio, TtsError, register_engine


class MyTts:
    name = "mytts"
    emotions = EMOTIONS          # or the subset your library can express
    presets = ("default",)
    priority = 20                # preferred over the built-in chirp engine

    def __init__(self) -> None:
        import some_tts_library  # hypothetical: load the model once
        self.model = some_tts_library.load()

    def synthesize(self, text, *, emotion, babble, preset):
        if babble or not text:
            raise TtsError("no babble in this engine")      # -> the caller falls back to a robot sound
        wav, rate, word_times = self.model.speak(text, style=emotion)   # float32 mono, Hz, [seconds]
        return SpeechAudio(np.asarray(wav, np.float32), rate, text=text, emotion=emotion, mode="words",
                           beats=[(t, 1.0) for t in word_times])


register_engine(MyTts())   # plain library code, in the process that hosts the agent (your own app / daemon)
# or, from an extension:  def setup_agent(self, ctx): ctx.register_tts_engine(MyTts())   (nebi_cli.extensions)
```

## Extensions

The core only uses the robot's public link (camera, targets, voice, eyes, motion, telemetry). Features that need more,
such as the senses, maps and an Explore / 3D-map cockpit UI, plug in as extensions. nebi owns only the
generic part, `nebi.extensions` / `nebi.extension_loader`:

- **Discovery:** entry points in the group `nebi.extensions`, plus `PYNEBI_EXTENSIONS="module:attr,..."` for
  development. `PYNEBI_DISABLE_EXTENSIONS=1` turns all of them off (the test suites set it);
  `PYNEBI_DISABLE_EXTENSIONS=name1,name2` skips just those. Load or setup failures are logged and listed in
  `nebi status` / `GET /api/extensions`; they never break the core.
- **Extension object** (`NebiExtension`): `name`, and optionally `version`, `setup_agent(ctx)`, `setup_server(ctx)`,
  `guide_markdown()`.
- **Hosts:** `setup_agent` is called by nebi-cli with an `AgentExtensionContext` (`nebi_cli.extensions`, which also
  re-exports `Cmd`, `Arg`, `command`, `CommandGroup`, `AgentError`, ...; see the nebi-cli README),
  `setup_server` by nebi-cockpit with a `ServerExtensionContext` (`nebi_cockpit.extensions`; see the
  nebi-cockpit README). Hosts plug their bookkeeping into the loader with
  `register_reset_hook` / `register_report_hook`, so nebi never imports them.
