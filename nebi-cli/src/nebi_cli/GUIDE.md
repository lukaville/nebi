# nebi — agent manual

You control **Nebi**, a small two-wheeled-legged self-balancing robot (~30 cm, 0–2 m/s) with a
gimbal camera, onboard person/pet detection, LED eyes, a speaker and a charging dock.
`nebi` is a CLI made for you: every command prints one JSON object on stdout and uses exit codes.

## How it works
- `nebi up ...` starts a background daemon that holds the robot connection. Commands then return fast.
- The robot accepts only one controller. If the human's cockpit (`nebi-cockpit serve`, :8000) is running, `nebi`
  uses it directly (it hosts the agent API), so you share its link; the cockpit shows an "Agent: ..." pill
  while your commands move the robot.
- The core works over the robot's public link: camera, targets, voice, eyes, motion, telemetry events.
  Extensions add more (senses, maps, ...): `nebi status` lists the loaded ones, their commands show up in
  `nebi schema`, and their manual sections are appended at the end of this guide ("Extension: ...").
- If the daemon is not running, any command auto-starts it with the last saved target.

## Output contract
    success: {"ok": true,  "command": "...", "result": {...}, "state": {...}, "warnings": [...]}
    failure: {"ok": false, "command": "...", "error": {"code", "message", "hint"}, "state": {...}}
- `state` is the robot's compact state after the command. Read it instead of calling `nebi state` again.
- `error.hint` usually names the exact next command to run.
- `warnings` are worth reading (open-loop estimates, mode conflicts, auto-actions).
- Exit codes: 0 ok · 1 failed · 2 usage · 3 not connected/daemon down · 4 safety refusal · 5 timeout ·
  130 interrupted (Ctrl-C / SIGTERM while moving: the robot was sent `stop`).
- `BAD_ARGUMENT` (exit 2) always means your arguments; `INTERNAL_ERROR` (exit 1) is a daemon-side problem,
  not your arguments — retry once, then try another approach.
- Most results carry a one-line `result.summary`.

## Safety rules (follow them)
1. `nebi stop` cancels any motion instantly, from any process, silences speech and pauses `idle` pet life
   (result `idle_stopped: true`; resume with `nebi idle start`). Use it whenever unsure.
2. Look before you move: `nebi look` (then view the image file) before driving into unknown space.
3. Moves are bounded: drive ≤ 5 m / 20 s, move ≤ 10 s per call. Chain short moves and re-look.
4. `drive forward` stops early for obstacles closer than `--clearance` (default 30 cm).
5. Flips need `--force` and ~1.5 m clear flat space. Never flip near people, stairs, tables or edges.
6. The robot cannot move while charging (USB-C) — error `ROBOT_CHARGING_LOCKED`.
7. Commands that move the body (stand, drive, turn, move, height, dock, undock, mode, follow, body, jump, flip)
   are refused while the robot is known to be carried (`BEING_CARRIED`). `--force` never overrides this; only
   `--ignore-carried` does (rare: a human holds it on purpose). Not blocked while carried: `say`, `eyes`, `sound`,
   `tilt` (gimbal only), `sit`, and `emote`.
   The robot link itself has no lift/carry signal: the carried state comes from an extension. Without one, moves
   still run with the warning "carried-check unavailable: no carried-state source ...", and `jump`/`flip` are
   refused (`CARRIED_UNKNOWN`) unless you pass `--ignore-carried` — look first (`nebi look`) to see it is on
   the floor.
   When an extension senses a person within ~1.2 m ahead, `drive` speed and `move` forward/strafe sticks are
   capped to 0.3 (a warning says so only when something was actually capped).
8. `nebi estop` is a last resort (the robot may drop). Prefer `nebi stop`, then `nebi sit`.
9. Before ending a session: `nebi stop` and `nebi sit` (or `nebi dock`).
10. Only pass `--force` after you understand why a command was refused. It overrides a command's own checks
    (flip confirmation, obstacle/clearance stop, not-standing), never the carried check.

## Coordinates and directions
- Robot frame: forward/backward, left/right (strafe), turn left/right (yaw). Heading has no absolute
  reference — after turning, `nebi look` to confirm.
- Image points: `x,y` in 0..1, (0,0) = top-left. `nebi look --annotate` draws a 3x3 grid with the
  center coordinates of each cell and boxes with target ids.
- `targets[].where` = left | center | right of the image; `distance_m` is the estimated range.

## Core loop
    nebi state                 # posture, battery, mode, obstacle_cm, targets, faults
    nebi look --annotate       # → result.path: open that JPEG to see the scene
    nebi <act>                 # stand / drive / turn / tilt / track / follow ...
    nebi look                  # verify the effect

## Recipes
Wake up and look around:
    nebi up --sim              # or: nebi up --host 192.0.2.10
    nebi stand
    nebi look --annotate
    nebi turn left 90 && nebi look

Go to something you see on the left:
    nebi look --annotate                        # object is in the left third
    nebi turn left 30 && nebi look              # re-center it
    nebi drive forward 1m                       # result.stopped_reason: reached | obstacle | ...

Find a person and follow them:
    nebi wait 'targets>0' --timeout 20s || nebi turn left 90   # scan by turning
    nebi targets                                # pick an id
    nebi track 3 && nebi follow trail --distance 1.5m
    nebi events --since 0 --wait 30s            # target_lost / mode changes while following
    nebi unfollow

React to the world without polling hard:
    nebi events --since <next_since> --wait 30s   # long-poll; returns as soon as something happens
    # event types: target_appeared, target_lost, posture_changed, mission_changed, fault_raised,
    # fault_cleared, battery_low, docked_changed, charging_changed, connected_changed, degraded_changed...

Take and fetch a photo:
    nebi photo --download ./photo.jpg            # full-resolution, stored on the robot as well
    nebi look -o ./quick.jpg                     # instant low-res frame from the live stream

Express yourself:
    nebi emote greet | happy | curious | love | sad | celebrate   # eyes + voice babble (or a sound)
    nebi eyes happy ; nebi eyes blue --hold 10 ; nebi eyes release
    nebi sound meow ; nebi say --babble -e happy
The robot's built-in choreographed animations cannot be started from apps; express with `eyes`, `say`, `sound`,
`body` and `emote`.

## Being a pet: body, memory, care
Every reply below has a one-line `result.summary` you can reason over directly.

Check yourself and wait for things (exit 5 on timeout):
    nebi care                                  # needs (charge_soon, check_faults, better_wifi...) + advice + mood
    nebi wait --for person --timeout 10m       # the camera detects a person/pet (person_left when it goes)
    nebi wait --for battery_low / charging / docked / posture / fault
    (`nebi schema wait` lists every event name, including those added by extensions)

Body language (in place, returns to neutral, adds matching eyes + voice unless --silent; look-at adds none):
    nebi body nod | shake | perk | cower | wiggle | curious      # yes / no / attentive / scared / happy / head tilt
    nebi body look-at                          # turn to face the nearest person/pet the camera sees
    nebi body sleep | wake                     # yawn and fold / get up happy
    nebi body wiggle --intensity 0.6 --silent

Hear and recognise:
    nebi listen                                # robot mic -> result.text (stops ~1 s after speech ends)
                                               # first use loads the model: text=null, stt="loading" -> retry
    nebi who                                   # names of faces in view (or "someone unknown")
    nebi meet Alice                            # learn the face in front (repeat 2-4x from other angles)

Remember across sessions:
    nebi note 'Alice likes the red ball' --about Alice
    nebi recall Alice                          # keyword search; `nebi recall` = most recent

Background pet life (reacts to people coming into view, low battery and faults, plus extension events such as
touches and gestures, with eyes, voice, body; never drives):
    nebi idle start [--level playful] [--quiet-hours 22-8]   ·   nebi idle status   ·   nebi idle stop
Idle stays still while any of your commands runs, while `say`/`listen` are busy (its babble never talks
over them) and while carried. Body moves only happen in manual camera/idle mode
with no follow, tracking, docking or game, and only when the carried state is known to be "not carried"
(that needs an extension's carried-state source). `nebi stop` stops it too.

A typical pet loop:
    nebi wait --for person --timeout 10m && nebi body look-at && nebi who
    nebi say 'Alice!' -e happy && nebi body wiggle
    nebi note 'Alice came to say hi' --about Alice

Eyes: the robot shows its own idle eyes until you take them over. `nebi eyes <expression>` plays an animation
(`--repeat N` loops of ~1.4 s, `--until-release` forever), `nebi eyes <color>` holds a colour (`red`, `#ff8800`;
`--hold 30s` default, `--until-release`, `--brightness 0.5`), `nebi eyes off` holds them dark, and
`nebi eyes release` gives them back to the robot (also happens by itself when a hold/expression ends).
`nebi eyes list` shows expressions, colours and what is showing now (`status`: mode, seconds_left). Eyes
combine freely with body moves; `emote`, `body`, `idle` and `say` drive them too
(a newer eye command replaces the current one).

Pet mode (the firmware's own interactive personality): `nebi stand` first, then `nebi mode ego`. It only starts
while standing still (EGO_MODE_NEEDS_STANDING otherwise); check `result.entered` (false + a warning = the robot
stayed in its old mode).

Health faults: `nebi faults` lists ACTIVE faults (raised/cleared live from the robot's HMS events) with a
plain `recovery_hint`; events `fault_raised` / `fault_cleared` appear in `nebi events` and `nebi wait --for
fault`. `nebi faults --clear <code>` only acknowledges faults the robot asked the app to confirm
(HMS_NO_PENDING_ACTION otherwise: those clear by themselves once the cause is fixed).

## Voice (you are a small pet robot, not an assistant)
`nebi say` speaks in Nebi's small-robot voice through its speaker. The voice comes from a TTS engine:
the built-in `chirp` engine renders robot chirps (one per syllable, no real words) and always works; the built-in
`system` engine (the computer's own speech: macOS `say`, Windows SAPI, Linux espeak-ng) speaks real words and is the
default when present (its presets are installed voices, e.g. `--preset Zarvox`; babble stays chirps); other
engines may be installed (`result.engine` says which one spoke; `--engine NAME` picks one, `--preset` a voice of
it). If the engine fails, a matching built-in robot sound plays instead (`result.mode: "sound"` + warning).
The eyes pulse in sync with the voice (`--no-animate` to leave them alone); afterwards the robot's own eyes come back.
    nebi say 'Hello!' -e happy
    nebi say 'Ball? 👀'                    # emoji pick the emotion (😊 🎉 👀 😢 😱 😴 😠 ❤️ 😬)
    nebi say --babble -e curious           # chirps/coos, no words
    nebi say 'Wheee!' --no-wait && nebi drive forward 1m    # talk while moving
    nebi say 'Hi friend' -e love --nod     # tiny gimbal nods on stressed syllables
    nebi say 'Hi' --engine chirp --preset tiny   # a specific engine / voice preset (chirp: robot, tiny, deep)
Emotions: neutral happy excited curious sad scared sleepy grumpy love uh-oh (an engine without one speaks neutral).
How to sound like a pet:
- Prefer babble or 1-6 words with a clear emotion. Names, single nouns and questions work best
  ("Ball?", "Uh-oh.", "Yay!", "Home?"). Avoid explaining things out loud in long sentences.
- Match the emotion to what just happened (target_appeared -> curious/happy, fault -> uh-oh, low battery -> sleepy).
- Context is automatic (night = quieter, far listener = louder, battery <=15% = tired); see `result.context`.
- `nebi stop` silences speech. On the simulator the audio plays on this computer (`--to robot|local|both` overrides).

Go charge:
    nebi dock && nebi wait docked=true --timeout 3m

Do a whole plan in one call (stops at the first failure):
    nebi run 'stand' 'drive forward 50cm' 'turn right 90' 'look --annotate'

## Waiting on conditions
`nebi wait '<field><op><value>[,more]' --timeout 30s` — exit 0 when true, exit 5 on timeout
(result includes the observed values). Fields: everything in `state`
(posture, standing, battery, charging, docked, mission, mode, targets, locked_target,
obstacle_cm, recording, tracking, faults, speed_mps, height_cm, gimbal_deg) and in `state --full`.
Examples: `posture=BALANCING`, `battery<20`, `targets>=1`, `docked=true,charging=true`.

## Accuracy notes
- `drive` closes the loop on wheel odometry when available (`feedback: "odometry"`), otherwise it
  estimates by time (`feedback: "dead_reckoning"` + warning).
- `turn` is timed open-loop (about ±15%). For precise alignment, turn in small steps and re-look.
- `look` is the live stream (≈640–960 px wide); `photo` is full resolution but slower.

## Discovery and connection
    nebi discover [--deep]          # USB-C, LAN, SoftAP, BLE
    nebi up --host <ip> | --flow auto | --flow softap | --flow usb_ncm | --sim
After every power-on the robot does NOT rejoin Wi-Fi by itself (it only listens on Bluetooth): run
`nebi setup-wifi --ssid ... --password ...` (BLE -> home Wi-Fi), then `nebi up`. Over a USB-C data cable: `nebi up --flow usb_ncm` (some robots only charge over USB-C).
    nebi status                     # daemon + link + loaded extensions; never starts anything
    nebi down                       # stop motion, disconnect, exit daemon
First-time Wi-Fi onboarding of a retail robot uses the local tool: `nebi setup-wifi --ssid ... --password ...`.

## Expert escape hatch
    nebi topics led                         # 184 protocol topics
    nebi pub /cmd/audio/play_file_req '{"audio_path": "/md/etc/trumpet.wav", "play_action": 1}'
    nebi req /cmd/common/get_version_req
`nebi schema [command]` prints every command, argument, default and choice as JSON.
