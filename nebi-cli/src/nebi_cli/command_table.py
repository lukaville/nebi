"""The `nebi` command table: every command, its arguments, help, examples and safety class. Pure data.

`spec.py` is the public facade (active command set, lookups, schema); add a command here plus one
`@command("name")` handler in an *_ops.py module.
"""

from __future__ import annotations

from nebi_cli.spec_model import Arg, Cmd

LOOK_VIEWS: tuple[str, ...] = ("camera",)  # extensions add more (AgentExtensionContext.add_look_view)

BODY_CHOICES = ("nod", "shake", "perk", "cower", "wiggle", "curious", "sleep", "wake", "look-at")

FORCE = Arg(
    "force",
    "bool",
    "Override this command's own refusal (flip confirmation, obstacle/clearance stop, not-standing). "
    "Never bypasses the being-carried check (see --ignore-carried).",
)
IGNORE_CARRIED = Arg(
    "ignore_carried",
    "bool",
    "Move even though the robot reports being carried/held, or the carried state is unknown (no carried-state source). "
    "Rarely right: only when a human is holding it on purpose.",
)

CORE_COMMANDS: tuple[Cmd, ...] = (
    # ------------------------------------------------------------------ session
    Cmd(
        "up",
        "session",
        "Start the background daemon (if needed) and connect to a robot or the simulator. Idempotent.",
        (
            Arg("sim", "bool", "Connect to the built-in simulator (safe sandbox, no hardware)."),
            Arg("host", "str", "Robot IP on your LAN, its SoftAP or over USB-C (e.g. 192.0.2.10, 192.168.7.1, 192.168.123.1)."),
            Arg(
                "flow",
                "str",
                "Connection flow to use instead of --host/--sim.",
                choices=("auto", "lan_sta", "usb_ncm", "softap", "ble_bootstrap"),
            ),
            Arg("interface", "str", "Bind robot sockets to this OS interface (e.g. en7)."),
            Arg("reconnect", "bool", "Force a fresh connection even if already connected to the same target."),
        ),
        (
            "nebi up --sim",
            "nebi up --host 192.0.2.10",
            "nebi up --flow auto",
            "nebi up            # reuse the last target",
        ),
        notes="Without arguments reuses the last saved target (~/.config/pynebi/agent.json). "
        "Other commands auto-run `nebi up` with the saved target when the daemon is not running.",
        local=True,
        timeout_s=90.0,
    ),
    Cmd("down", "session", "Safely stop motion, disconnect and shut down the daemon.", (), ("nebi down",), local=True),
    Cmd(
        "status",
        "session",
        "Daemon + link status (connected?, flow, stage, cockpit URL, last error, loaded extensions). Never auto-starts anything.",
        (),
        ("nebi status",),
        local=True,
    ),
    Cmd(
        "discover",
        "session",
        "Scan USB-C, home LAN, SoftAP and BLE for robots (does not need a connection).",
        (
            Arg("deep", "bool", "Also sweep the whole /24 LAN subnet (slower)."),
            Arg("no_ble", "bool", "Skip Bluetooth scan."),
            Arg("timeout", "duration", "Scan time budget.", default=2.5),
        ),
        ("nebi discover", "nebi discover --deep"),
        local=True,
    ),
    # ------------------------------------------------------------------ perception / state
    Cmd(
        "state",
        "perceive",
        "Compact robot state: posture, battery, mission, height, obstacle distance, targets, faults.",
        (Arg("full", "bool", "Return the complete raw telemetry snapshot instead of the compact view."),),
        ("nebi state", "nebi state --full"),
    ),
    Cmd(
        "look",
        "perceive",
        "Save what the robot camera sees right now as a JPEG and return its path plus detected targets.",
        (
            Arg("out", "path", "Output file (default: ~/.cache/pynebi/snapshots/look-<time>.jpg).", short="-o"),
            Arg("annotate", "bool", "Draw target boxes + ids and a 3x3 grid on the image (helps pick targets/points)."),
            Arg("max_width", "int", "Downscale so width <= this (saves tokens when viewing).", default=1024),
            Arg(
                "view",
                "str",
                "camera = what the camera sees (extensions may add more views).",
                choices=LOOK_VIEWS,
                default="camera",
            ),
        ),
        ("nebi look", "nebi look --annotate -o /tmp/view.jpg"),
        notes="Read the returned `path` with your image viewer/tool to actually see the scene. Views added by "
        "extensions also return a text `summary`.",
    ),
    Cmd(
        "targets",
        "perceive",
        "List people/pets currently detected by onboard AI vision (id, label, position, distance).",
        (),
        ("nebi targets",),
    ),
    Cmd(
        "wait",
        "perceive",
        "Block until a state condition holds (AND of comma-separated clauses) or time out (exit 5).",
        (
            Arg(
                "condition",
                "str",
                "e.g. 'posture=BALANCING', 'battery>30', 'targets>0', 'docked=true,charging=true'.",
                positional=True,
            ),
            Arg(
                "for",
                "str",
                "Wait for something to happen instead (see notes for the event names).",
            ),
            Arg("timeout", "duration", "Give up after this long.", default=15.0),
        ),
        (
            "nebi wait posture=BALANCING",
            "nebi wait 'targets>0' --timeout 30s",
            "nebi wait --for person --timeout 5m",
        ),
        notes="Fields: any key of `nebi state` (posture, battery, charging, docked, mission, mode, motion, targets, "
        "locked_target, obstacle_cm, recording, faults, ...) or any key of `nebi state --full`. "
        "Operators: = != < > <= >=. Strings compare case-insensitively.",
        timeout_s=3600.0,
    ),
    Cmd(
        "events",
        "perceive",
        "Things that happened since a cursor: robot events (posture, targets, faults, link) plus the event "
        "streams of loaded extensions.",
        (
            Arg("since", "int", "Only robot events with seq > this (use `next_since` from the previous call).", default=0),
            Arg("source", "str", "Which event streams.", choices=("all", "robot"), default="all"),
            Arg("wait", "duration", "Long-poll: wait up to this long for at least one new event.", default=0.0),
            Arg("limit", "int", "Max events to return.", default=50),
        ),
        ("nebi events", "nebi events --since 42 --wait 10s"),
        timeout_s=3600.0,
    ),
    Cmd(
        "info",
        "perceive",
        "Robot identity: serial number, hardware/firmware versions, link flow, capabilities.",
        (),
        ("nebi info",),
    ),
    Cmd(
        "faults",
        "perceive",
        "Active HMS health faults with human-readable messages and recovery hints; optionally clear one.",
        (Arg("clear", "str", "Fault code to clear (e.g. 0xF0010012)."),),
        ("nebi faults", "nebi faults --clear 0xF0010012"),
    ),
    # ------------------------------------------------------------------ locomotion
    Cmd(
        "stand",
        "move",
        "Stand up and self-balance (required before driving). Waits until balancing.",
        (FORCE, IGNORE_CARRIED, Arg("no_wait", "bool", "Return right after sending the command.")),
        ("nebi stand",),
        moves_robot=True, motion_kind="body",
    ),
    Cmd(
        "sit",
        "move",
        "Fold legs and sit down (safe resting pose). Waits until folded.",
        (Arg("no_wait", "bool", "Return right after sending the command."),),
        ("nebi sit",),
        notes="Allowed while the robot is carried (folding is the safe resting pose).",
        moves_robot=True, motion_kind="rest",
    ),
    Cmd(
        "stop",
        "move",
        "Immediately zero all motion, cancel any running drive/turn/motion, silence speech and pause `idle` "
        "pet life, keep balancing. Always safe.",
        (),
        ("nebi stop",),
        notes="Result has idle_stopped=true when idle life was running; resume it with `nebi idle start`.",
    ),
    Cmd(
        "estop",
        "move",
        "EMERGENCY STOP (hardware e-stop toggle). Use only if `stop` is not enough; robot may drop.",
        (),
        ("nebi estop",),
    ),
    Cmd(
        "drive",
        "move",
        "Drive a bounded distance or duration, then stop. Uses odometry, stops early for obstacles.",
        (
            Arg("direction", "str", "Where to go.", positional=True, required=True, choices=("forward", "backward", "left", "right")),
            Arg("distance", "distance", "How far (e.g. 1m, 50cm, 0.3). Max 5m per call.", positional=True),
            Arg("for", "duration", "Drive for a duration instead of a distance (e.g. 2s). Max 20s.", flag="--for"),
            Arg("speed", "float", "Stick deflection 0.05..1.0 (0.3 = calm walking pace).", default=0.3),
            Arg("clearance", "distance", "Stop if an obstacle is closer than this (forward only; 0 disables).", default=0.3),
            FORCE, IGNORE_CARRIED,
        ),
        ("nebi drive forward 1m", "nebi drive backward 30cm --speed 0.2", "nebi drive left --for 1.5s"),
        notes="left/right strafe sideways. To change heading use `nebi turn`. Result tells you how far it actually "
        "went and why it stopped (reached|obstacle|timeout|stopped|duration).",
        moves_robot=True, motion_kind="body",
        timeout_s=60.0,
    ),
    Cmd(
        "turn",
        "move",
        "Rotate in place by an angle (open-loop timed; ~±15% accuracy), then stop.",
        (
            Arg("direction", "str", "Turn direction.", positional=True, required=True, choices=("left", "right", "around")),
            Arg("degrees", "angle", "Angle (default 90; `around` = 180 quick spin).", positional=True, default=90.0),
            Arg("speed", "float", "Yaw stick deflection 0.1..1.0.", default=0.5),
            FORCE,
            IGNORE_CARRIED,
        ),
        ("nebi turn left", "nebi turn right 45", "nebi turn around"),
        notes="Verify with `nebi look` after turning if exact heading matters.",
        moves_robot=True, motion_kind="body",
        timeout_s=40.0,
    ),
    Cmd(
        "move",
        "move",
        "Low-level: hold raw joystick axes for a bounded duration, then return to neutral.",
        (
            Arg("forward", "float", "Forward(+)/backward(-) velocity stick, -1..1.", default=0.0),
            Arg("strafe", "float", "Right(+)/left(-) strafe stick, -1..1.", default=0.0),
            Arg("yaw", "float", "Turn right(+)/left(-) stick, -1..1.", default=0.0),
            Arg("pitch", "float", "Camera gimbal up(+)/down(-) stick, -1..1.", default=0.0),
            Arg("for", "duration", "How long to hold (max 10s).", flag="--for", default=1.0),
            FORCE, IGNORE_CARRIED,
        ),
        ("nebi move --forward 0.3 --yaw 0.2 --for 2s", "nebi move --pitch 0.5 --for 0.5s"),
        moves_robot=True, motion_kind="body",
        timeout_s=30.0,
    ),
    Cmd(
        "tilt",
        "move",
        "Tilt the camera gimbal up/down for a short time (or `center`).",
        (
            Arg("direction", "str", "Gimbal direction.", positional=True, required=True, choices=("up", "down", "center")),
            Arg("for", "duration", "How long to push the gimbal stick.", flag="--for", default=0.5),
        ),
        ("nebi tilt up", "nebi tilt down --for 1s", "nebi tilt center"),
        moves_robot=True, motion_kind="gimbal",
    ),
    Cmd(
        "height",
        "move",
        "Set standing body height (0 = crouch ~8cm, 1 = tall ~14cm). Persists until changed.",
        (Arg("value", "float", "0.0..1.0", positional=True, required=True), IGNORE_CARRIED),
        ("nebi height 0.5", "nebi height 1"),
        moves_robot=True, motion_kind="body",
    ),
    Cmd(
        "gear",
        "move",
        "Set speed gear (affects all driving).",
        (Arg("gear", "str", "Gear.", positional=True, required=True, choices=("soft", "standard", "sport")),),
        ("nebi gear soft",),
    ),
    Cmd("jump", "move", "Jump in place (~15-20cm). Needs clear space; refused unless balancing.", (FORCE, IGNORE_CARRIED), ("nebi jump",), moves_robot=True, motion_kind="stunt"),
    Cmd(
        "flip",
        "move",
        "Acrobatic flip. RISKY: needs ~1.5m clear space and a flat surface. Requires --force.",
        (Arg("direction", "str", "Flip direction.", positional=True, default="back", choices=("front", "back", "left", "right")), FORCE, IGNORE_CARRIED),
        ("nebi flip back --force",),
        moves_robot=True, motion_kind="stunt",
    ),
    Cmd(
        "dock",
        "move",
        "Autonomously return to the charging dock (ArUco + IR). Use `nebi wait docked=true` to await.",
        (IGNORE_CARRIED,),
        ("nebi dock && nebi wait docked=true --timeout 3m",),
        moves_robot=True, motion_kind="body",
    ),
    Cmd("undock", "move", "Leave the charging dock.", (IGNORE_CARRIED,), ("nebi undock",), moves_robot=True, motion_kind="body"),
    Cmd(
        "mode",
        "move",
        "Switch behaviour mode: manual (you drive), companion (autonomous pet), ego (pet interaction: reacts to touch, "
        "gestures, voice), calibrate. `ego` needs the robot standing still (EGO_MODE_NEEDS_STANDING otherwise); the "
        "result's `entered` says whether the robot really switched.",
        (Arg("mode", "str", "Mode.", positional=True, required=True, choices=("manual", "companion", "ego", "calibrate")), IGNORE_CARRIED),
        ("nebi mode manual", "nebi mode companion"),
        moves_robot=True, motion_kind="body",
    ),
    # ------------------------------------------------------------------ follow / tracking
    Cmd(
        "track",
        "follow",
        "Lock AI tracking onto a target id (from `nebi targets`) or an image point (x,y in 0..1).",
        (
            Arg("target", "str", "Target id, or `x,y` normalized image point (0,0 = top-left).", positional=True, required=True),
        ),
        ("nebi track 3", "nebi track 0.5,0.6"),
    ),
    Cmd(
        "follow",
        "follow",
        "Autonomously follow the locked target (lock one first with `nebi track`, or it locks the nearest person).",
        (
            Arg("style", "str", "Follow style.", positional=True, default="trail", choices=("trail", "parallel", "lead", "orbit", "spotlight")),
            Arg("distance", "distance", "Desired following distance.", default=None),
            IGNORE_CARRIED,
        ),
        ("nebi follow", "nebi follow orbit --distance 2m"),
        moves_robot=True, motion_kind="body",
    ),
    Cmd("unfollow", "follow", "Stop following/tracking and return to manual mode.", (), ("nebi unfollow",)),
    # ------------------------------------------------------------------ camera
    Cmd(
        "photo",
        "camera",
        "Take a full-resolution photo on the robot's storage; optionally download it.",
        (Arg("download", "path", "Also download the new photo to this local path."),),
        ("nebi photo", "nebi photo --download ./shot.jpg"),
        timeout_s=45.0,
    ),
    Cmd(
        "record",
        "camera",
        "Start/stop MP4 recording on the robot (or mark a highlight).",
        (Arg("action", "str", "Action.", positional=True, required=True, choices=("start", "stop", "highlight")),),
        ("nebi record start", "nebi record stop"),
    ),
    Cmd("zoom", "camera", "Digital zoom factor (1.0 = none).", (Arg("factor", "float", "1.0..4.0", positional=True, required=True),), ("nebi zoom 2",)),
    Cmd(
        "media",
        "camera",
        "Browse/download/delete photos & videos stored on the robot.",
        (
            Arg("action", "str", "Action.", positional=True, default="ls", choices=("ls", "get", "rm", "stats")),
            Arg("file_id", "str", "File id for get/rm.", positional=True),
            Arg("out", "path", "Destination for `get` (default ~/.cache/pynebi/media/<name>).", short="-o"),
            Arg("limit", "int", "Max files for `ls`.", default=20),
            Arg("type", "str", "Filter for `ls`.", choices=("photo", "video")),
        ),
        ("nebi media", "nebi media get <file_id> -o ./clip.mp4", "nebi media rm <file_id>"),
        timeout_s=300.0,
    ),
    # ------------------------------------------------------------------ expression
    Cmd(
        "eyes",
        "express",
        "Set the LED eyes: an animated expression or a solid color, for a while or until released; `release` gives "
        "the eyes back to the robot's own look. `nebi eyes list` shows expressions, colors and what is showing now.",
        (
            Arg(
                "what",
                "str",
                "Expression (happy, wink, heart, cool, sleepy, dizzy, angry, music), color (#ff8800, red, ...), "
                "off (dark), release (robot's own eyes), status, or list.",
                positional=True,
                required=True,
            ),
            Arg("repeat", "int", "Expression loops (~1.4 s each); 0 = until `nebi eyes release`.", default=3),
            Arg("hold", "duration", "How long to hold a color (or `off`).", default=30.0),
            Arg("until_release", "bool", "Keep the color/expression until `nebi eyes release` (ignores --hold/--repeat)."),
            Arg("brightness", "float", "Brightness 0..1.", default=1.0),
        ),
        ("nebi eyes list", "nebi eyes happy", "nebi eyes blue --hold 10", "nebi eyes '#ff8800' --until-release",
         "nebi eyes release"),
        notes="Eyes are LED frames streamed to the robot (they never use the motion player, so they combine with body "
        "moves). When a hold/expression ends, or on `release`, the robot's own idle eyes come back by themselves. "
        "`emote`, `body`, `idle` and `say` (voice pulse) also drive the eyes; a newer eye command replaces the current one.",
        motion_kind="express",
    ),
    Cmd(
        "sound",
        "express",
        "Play a built-in sound effect by name/id, or a raw on-robot WAV path. No args lists sounds.",
        (Arg("name", "str", "Sound name or id (see list), or /md/etc/... wav path, or `stop`.", positional=True),),
        ("nebi sound", "nebi sound meow", "nebi sound 5"),
        motion_kind="express",
    ),
    Cmd(
        "say",
        "express",
        "Speak in Nebi's small-robot voice through its speaker, with emotion. No text = pet babble.",
        (
            Arg("text", "text", "Words to say. Keep it short (pets use 1-6 words). Emoji set the emotion (😊 👀 😴 ❤️).", positional=True),
            Arg(
                "emotion",
                "str",
                "Mood of the voice.",
                short="-e",
                choices=("neutral", "happy", "excited", "curious", "sad", "scared", "sleepy", "grumpy", "love", "uh-oh"),
            ),
            Arg("babble", "bool", "Pet gibberish (chirps/coos) in the chosen emotion instead of words."),
            Arg("volume", "float", "0..1.5 (auto-adjusted: quieter at night, louder for far-away people)."),
            Arg("to", "str", "Where to play (auto = robot speaker, or this computer when on the simulator).", choices=("auto", "robot", "local", "both"), default="auto"),
            Arg("no_wait", "bool", "Return immediately and keep speaking in the background (talk while moving)."),
            Arg("no_animate", "bool", "Do not pulse the LED eyes with the voice."),
            Arg("nod", "bool", "Nod the camera gimbal on stressed syllables (only when standing still)."),
            Arg("preset", "str", "Voice preset of the engine (default: the engine's first preset; chirp: robot, tiny, deep; system: an installed OS voice)."),
            Arg("engine", "str", "TTS engine (default: $PYNEBI_TTS_ENGINE or the best registered; built in: system (OS speech, when available), chirp)."),
            Arg("out", "path", "Also save the audio here (.wav) plus a .json body-sync timeline.", short="-o"),
        ),
        (
            "nebi say 'Hello!' -e happy",
            "nebi say 'Ball? 👀'",
            "nebi say --babble -e excited",
            "nebi say 'Wheee!' --no-wait",
            "nebi say 'Battery low. Going home.' -e sleepy -o ./line.wav",
        ),
        notes="Pet style: prefer babble or 1-6 words with a clear emotion over long sentences. Context is applied "
        "automatically (low battery -> tired voice, night -> quieter, far target -> louder); see result.context. "
        "`nebi stop` silences it.",
        moves_robot=True, motion_kind="express",
        timeout_s=60.0,
    ),
    Cmd(
        "emote",
        "express",
        "One-shot expressive combo (eyes + voice babble, or a sound): happy, curious, love, sad, celebrate, greet. "
        "Plays even while carried.",
        (Arg("emotion", "str", "Emotion.", positional=True, required=True, choices=("happy", "curious", "love", "sad", "celebrate", "greet")),),
        ("nebi emote greet",),
        moves_robot=True, motion_kind="express",
    ),
    # ------------------------------------------------------------------ pet life
    Cmd(
        "body",
        "pet",
        "Body language in place: nod (yes), shake (no), perk (attentive), cower (scared), wiggle (happy), "
        "curious (head tilt), look-at (turn to the nearest person/pet), sleep (curl up), wake (get up). Adds matching eyes+voice "
        "(except look-at; none with --silent).",
        (
            Arg("gesture", "str", "Gesture.", positional=True, required=True, choices=BODY_CHOICES),
            Arg("target", "str", "For look-at: person (default) / pet.", positional=True),
            Arg("intensity", "float", "0.4..1.5 (subtle .. big).", default=1.0),
            Arg("silent", "bool", "No voice/eyes, body only."),
            FORCE, IGNORE_CARRIED,
        ),
        ("nebi body nod", "nebi body curious", "nebi body look-at", "nebi body sleep"),
        moves_robot=True, motion_kind="body",
    ),
    Cmd(
        "listen",
        "pet",
        "Listen through the robot's microphone and transcribe speech (stops ~1 s after the speaker goes quiet).",
        (
            Arg("seconds", "duration", "Maximum listening time.", default=6.0),
            Arg("fixed", "bool", "Record the full duration instead of stopping at silence."),
            Arg("language", "str", "Force a language code (en, ru, ...); default auto-detect."),
            Arg("out", "path", "Where to save the .wav (default ~/.cache/pynebi/audio/).", short="-o"),
        ),
        ("nebi listen", "nebi listen --seconds 10", "nebi listen --language en"),
        notes="Returns `text` ('' for silence). Speech-to-text runs locally (mlx-whisper). The first call loads the "
        "model (can take minutes on first download): if it is not ready in time the result has text=null, "
        "stt='loading' and a warning; retry shortly. Idle pet life stays quiet while listening.",
        timeout_s=150.0,
    ),
    Cmd(
        "meet",
        "pet",
        "Learn the face currently in front of the camera under a name (call 2-4 times from different angles).",
        (
            Arg("name", "str", "Person's name.", positional=True),
            Arg("list", "bool", "List known people."),
            Arg("forget", "bool", "Forget this person's face."),
        ),
        ("nebi meet Alice", "nebi meet --list", "nebi meet Alice --forget"),
        timeout_s=60.0,
    ),
    Cmd("who", "pet", "Who is in front of the camera (known names, or 'someone unknown'), with left/center/right.", (), ("nebi who",), timeout_s=60.0),
    Cmd(
        "note",
        "pet",
        "Remember a fact for later (long-term memory across sessions): preferences, events, people.",
        (
            Arg("text", "text", "What to remember.", positional=True),
            Arg("about", "str", "Who/what it is about (person name, place, toy)."),
            Arg("tags", "str", "Comma-separated tags."),
            Arg("forget", "str", "Delete the memory with this id instead."),
        ),
        ("nebi note 'Alice likes the red ball' --about Alice", "nebi note --forget 12"),
    ),
    Cmd(
        "recall",
        "pet",
        "Search long-term memory (keywords), or list the most recent memories.",
        (Arg("query", "text", "Keywords, e.g. 'Alice ball'.", positional=True), Arg("limit", "int", "Max results.", default=10)),
        ("nebi recall Alice", "nebi recall"),
    ),
    Cmd(
        "care",
        "pet",
        "Self-care check: battery, charging, faults, link quality (+ extension fields) -> needs + advice + mood.",
        (),
        ("nebi care",),
    ),
    Cmd(
        "idle",
        "pet",
        "Background 'pet life': reacts to people coming into view, low battery and faults (plus touch/gesture "
        "events from extensions) with eyes, voice and in-place body language; small ambient behaviours; quiet at "
        "night. Never drives.",
        (
            Arg("action", "str", "start / stop / status.", positional=True, choices=("start", "stop", "status"), default="status"),
            Arg("level", "str", "How lively.", choices=("calm", "playful"), default="calm"),
            Arg("quiet_hours", "str", "No sounds/motion in this local-time window, e.g. 22-8.", default="22-8"),
            Arg("silent", "bool", "Never speak (eyes and body only)."),
        ),
        ("nebi idle start", "nebi idle start --level playful --quiet-hours 23-7", "nebi idle status", "nebi idle stop"),
        notes="Idle stays still while any agent command runs, while `say`/`listen` are busy, while carried, and "
        "(for body moves and firmware eye effects) unless the robot is in manual camera/idle mode with no "
        "follow/tracking/docking. `nebi stop` also stops idle (resume with `nebi idle start`).",
    ),
    # ------------------------------------------------------------------ system
    Cmd(
        "params",
        "system",
        "Read or write tunable robot parameters (max speed, yaw rate, ...).",
        (
            Arg("keys", "str", "Comma-separated keys to read (default: all known).", positional=True),
            Arg("set", "json", 'JSON object of values to write, e.g. \'{"md_function.max_speed": 1.0}\'.'),
        ),
        ("nebi params", "nebi params md_function.max_speed", "nebi params --set '{\"md_function.max_speed\": 1.0}'"),
    ),
    Cmd(
        "remote",
        "system",
        "Vibrate a paired UWB remote (to get the owner's attention).",
        (Arg("slot", "int", "Remote slot 0..3.", default=0), Arg("level", "int", "Strength 1..3.", default=2)),
        ("nebi remote",),
    ),
    # ------------------------------------------------------------------ raw protocol escape hatch
    Cmd(
        "topics",
        "raw",
        "List protocol topics (184) usable with `nebi pub` / `nebi req`.",
        (Arg("search", "str", "Substring filter.", positional=True),),
        ("nebi topics led", "nebi topics /cmd/camera"),
    ),
    Cmd(
        "pub",
        "raw",
        "Publish a raw protobuf message to a topic (expert escape hatch).",
        (
            Arg("topic", "str", "Topic, e.g. /cmd/audio/play_file_req.", positional=True, required=True),
            Arg("fields", "json", "JSON object of `data` fields.", positional=True, default="{}"),
        ),
        ("nebi pub /cmd/audio/play_file_req '{\"audio_path\": \"/md/etc/trumpet.wav\", \"play_action\": 1}'",),
    ),
    Cmd(
        "req",
        "raw",
        "Send a raw request and wait for its response topic (expert escape hatch).",
        (
            Arg("topic", "str", "Request topic, e.g. /cmd/common/get_version_req.", positional=True, required=True),
            Arg("fields", "json", "JSON object of `data` fields.", positional=True, default="{}"),
            Arg("response", "str", "Response topic (default: derived from *_req -> *_rsp)."),
            Arg("timeout", "duration", "RPC timeout.", default=3.0),
        ),
        ("nebi req /cmd/common/get_version_req",),
    ),
    # ------------------------------------------------------------------ meta
    Cmd("guide", "meta", "Print the agent manual: safety rules, workflows, recipes. Read this first.", (), ("nebi guide",), local=True),
    Cmd(
        "schema",
        "meta",
        "Machine-readable description of every command, argument and exit code (JSON).",
        (Arg("name", "str", "Only this command.", positional=True),),
        ("nebi schema", "nebi schema drive"),
        local=True,
    ),
    Cmd(
        "run",
        "meta",
        "Run several commands in one call (args or stdin lines, `#` comments). Stops at first failure.",
        (
            Arg("commands", "str", "Commands, each quoted, e.g. 'stand' 'drive forward 1m'.", positional=True),
            Arg("keep_going", "bool", "Continue after a failing step."),
        ),
        ("nebi run 'stand' 'drive forward 1m' 'turn left' 'look'", "nebi run < plan.txt"),
        local=True,
    ),
)

CORE_GROUP_TITLES: dict[str, str] = {
    "session": "Session",
    "perceive": "Perceive",
    "move": "Move",
    "follow": "Follow & track",
    "camera": "Camera & media",
    "express": "Express",
    "pet": "Pet life",
    "system": "System",
    "raw": "Raw protocol",
    "meta": "Meta",
}
