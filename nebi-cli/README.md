# `nebi-cli` — Agent-Friendly `nebi` CLI compatible with the Beni Robot

> [!WARNING]
> **Independent, unofficial project.** nebi is not affiliated with, endorsed by or supported by the manufacturer of
> the Beni robot. "Beni" and the model names LZ110 / LZ100 are used only to identify the compatible hardware.
>
> - Use it only with a robot **you own**. Using it may **void your warranty** or violate the manufacturer's terms.
> - It **physically moves the robot.** Supervise the robot and keep a clear space around it: the software can make it
>   fall over, jump, or drive into people, pets and things.
> - **Firmware update and factory reset** commands can brick the device or wipe its data.
> - Provided **AS IS, without warranty** of any kind (Apache-2.0, see the LICENSE and NOTICE files).

`nebi` is a JSON-speaking command line built for AI agents and scripts (part of the nebi monorepo), compatible
with the Beni robot (LZ110/LZ100). The robot accepts one controller, so commands are executed by an **agent runtime
that lives inside a nebi-cockpit app** — the user's running cockpit, or a standalone daemon — and the CLI itself is
a thin, fast HTTP client. Every command is a bounded, structured call. The manual an agent reads is
`src/nebi_cli/GUIDE.md` (`nebi guide`).

Depends on: `nebi`, `nebi-cockpit`.

```bash
nebi up --sim                      # or: nebi up --host 192.0.2.10 | --flow auto
nebi guide                         # the manual an agent should read first
nebi stand && nebi look --annotate # look -> JPEG path + detected targets
nebi drive forward 1m              # odometry-closed, obstacle-aware, always ends stopped
nebi turn left 90 ; nebi follow orbit --distance 2m ; nebi unfollow
nebi wait 'docked=true' --timeout 3m ; nebi events --since 0 --wait 30s
nebi run 'stand' 'drive forward 50cm' 'emote greet' 'look'
nebi schema                        # machine-readable command/arg/exit-code spec
```

Every reply is `{"ok", "command", "result"|"error"{code,message,hint}, "state", "warnings"}`; exit codes:
`0` ok, `1` failed, `2` usage, `3` not connected, `4` safety refusal (`--force`), `5` timeout.

## Where the agent runs

- **Embedded in the cockpit (preferred).** nebi-cli declares an entry point in the `nebi.extensions` group
  (`nebi-cli = "nebi_cli.cockpit_ext:extension"`). Its `setup_server` mounts `/agent/call|ping|activity` on every
  `nebi-cockpit serve` and runs the `AgentRuntime` for the app's lifetime (`ctx.add_lifespan`), so the human's
  cockpit and the agent share one robot link. `nebi down` then only stops motion (`"embedded": true`).
  `nebi-cockpit serve --disable-extension nebi-cli` opts out.
- **Standalone daemon.** When no cockpit answers (port from `--port`, `NEBI_PORT`, `~/.config/pynebi/agent.json`, else
  :8765, falling back to a running cockpit on `PYNEBI_PORT`/:8000), `nebi up` spawns `python -m nebi_cli.daemon`:
  a cockpit app (`nebi_cockpit.app.create_app`) plus the same agent API, with its FPV page at the daemon URL.

Config and caches stay where they were: `~/.config/pynebi/` (`agent.json`, `wifi.json`, identity) and `~/.cache/pynebi/`.

## Local tools (the former `nebi` CLI)

Utilities that talk to the robot (or the cockpit's intercom) directly, without the daemon:

| old | new | notes |
|---|---|---|
| `nebi serve` | `nebi-cockpit serve` | in nebi-cockpit; `--no-agent` → `--disable-extension nebi-cli` |
| `nebi setup-wifi` | `nebi setup-wifi` (= `nebi tools setup-wifi`) | BLE (MD-TS) → home Wi-Fi provisioning |
| `nebi pair-ble` | `nebi pair-ble` (= `nebi tools pair-ble`) | |
| `nebi sim` | `nebi sim` (= `nebi tools sim`) | standalone MockNebiRobot |
| `nebi status` | `nebi tools status` | `nebi status` is the agent's daemon/link status |
| `nebi discover` | `nebi tools discover` | `nebi discover` (agent, same scan, robots only) also exists |
| `nebi say` | `nebi tools say` | cockpit intercom TTS test; `nebi say` is agent speech |

Tools keep their old flags and output (JSON on stdout, progress on stderr). They are not allowed inside `nebi run`.

## Text-to-speech

`nebi say` (and the babble of `emote` / `body` / `idle`) uses nebi's pluggable engines (`nebi.tts`, see the
nebi README, section *Text-to-speech*). Selection: `nebi say --engine NAME`, else `PYNEBI_TTS_ENGINE`,
else the registered engine with the highest `priority`. If an engine raises, a robot sound effect plays instead.

## Extensions (agent side)

Extensions are discovered by nebi's loader (see the nebi README, section *Extensions*). nebi-cli
calls `setup_agent(ctx)` with an `AgentExtensionContext` — once per process in spec-only mode (`ctx.runtime is None`:
the CLI building its parser, `nebi schema`, `nebi guide`) and once per `AgentRuntime`. Import everything from
`nebi_cli.extensions`:

- `add_commands(cmds, HandlerGroup, group_titles=...)`, `add_error_codes`, `add_service`,
  `set_carried_state_provider` (SafetyGate; without one the carried state is unknown: body moves warn, stunts need
  `--ignore-carried`), `set_people_provider` (speed cap near people, look-at bearings), `add_event_source`
  (`nebi events --source <name>`, idle reactions), `add_look_view`, `add_wait_events`, `add_care_provider`,
  `add_target_locator`, `register_tts_engine`, `on_shutdown`.
- `nebi schema`, `nebi guide` (an "Extension: <name>" section per extension) and `nebi status` include extensions.

A minimal extension:

```python
# hello_ext.py  —  run with PYNEBI_EXTENSIONS=hello_ext:extension nebi hello Bob
from typing import Any

from nebi_cli.extensions import AgentExtensionContext, Arg, Args, Cmd, CommandGroup, command


class HelloCommands(CommandGroup):
    @command("hello")
    async def hello(self, a: Args) -> dict[str, Any]:
        posture = self.client().telemetry.posture_name
        return {"summary": f"hello {a.get('who') or 'world'}, I am {posture.lower()}"}


class HelloExtension:
    name = "hello"
    version = "0.1.0"

    def setup_agent(self, ctx: AgentExtensionContext) -> None:
        ctx.add_commands(
            [Cmd("hello", "hello", "Greet someone.", (Arg("who", "str", "Name.", positional=True),), ("nebi hello Bob",))],
            HelloCommands,
            group_titles={"hello": "Hello"},
        )

    def guide_markdown(self) -> str:
        return "    nebi hello Bob             # says hello"


extension = HelloExtension()
```

Packaged, it declares `[project.entry-points."nebi.extensions"] hello = "hello_ext:extension"`. A cockpit half
(`setup_server`) uses `nebi_cockpit.extensions.ServerExtensionContext`; import it lazily / under `TYPE_CHECKING` so the
extension still loads where nebi-cockpit is absent.

## Layout

See the module map in `src/nebi_cli/__init__.py`: spec layer (`spec.py` is the single
source of truth for commands, `command_table.py`), thin client (`cli.py`), agent API + daemon (`daemon.py`,
`cockpit_ext.py`), runtime and command groups (`runtime.py`, `*_ops.py`), local tools (`tools.py`, `intercom_say.py`).
New `nebi` command: one entry in `command_table.py` plus a method decorated with `@command("name")`.

## Development

```bash
cd nebi/nebi-cli && "$PWD/../.venv/bin/python" -m pytest -q
```

Lint/types: see the monorepo's root README.
