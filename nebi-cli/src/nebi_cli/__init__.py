"""nebi-cli: the agent-friendly `nebi` CLI, compatible with the Beni robot (LZ110/LZ100) — a thin, JSON-speaking command client plus the
agent runtime that runs inside a nebi-cockpit app (the user's cockpit, or a standalone daemon).

Layout:
  spec layer (stdlib only, shared by CLI and daemon)
    - `spec.py`          — facade: active command set (core + extensions), lookups, `nebi schema`
    - `command_table.py` — every command (args, help, examples, safety class). Pure data.
    - `spec_model.py`    — `Arg` / `Cmd` dataclasses, exit codes
    - `units.py`         — distance/duration/angle parsers, argument coercion
    - `errors.py`        — AgentError + the error-code catalogue; `settings.py` — paths and ports
  client
    - `cli.py`           — thin client: parses argv from the spec, talks HTTP to the daemon, prints JSON
  daemon
    - `daemon.py`        — the /agent/* API attached to a nebi-cockpit app + the standalone daemon entry point
    - `cockpit_ext.py`   — entry point (`nebi.extensions`): every `nebi-cockpit serve` hosts the agent API
  tools
    - `tools.py`         — `nebi tools ...` (and `nebi setup-wifi | pair-ble | sim`): local robot utilities that
                           do not go through the daemon (the former `nebi` CLI); `intercom_say.py` (`tools say`)
    - `runtime.py`       — AgentRuntime: composition root, dispatch (`call` / `invoke`)
    - `registry.py`      — `@command("name")` handlers grouped in CommandGroup classes
    - services           — `motion.py` (lock, epochs, bounded holds), `safety_ops.py` (carried check, speed cap),
                           `activity.py`, `events.py` (event log + telemetry watcher), `views.py` (compact state),
                           `conditions.py`
    - command groups     — `session_ops`, `perceive_ops`, `locomotion_ops`, `camera_ops`, `system_ops`,
                           `express_ops` (+ Expression service), `voice_ops` (+ SpeechService), `idle_ops`
                           (+ IdleLoop), `pet_ops` (body language, faces, memory, care)
    - `extensions.py`    — agent-side extension API: AgentExtensionContext + the hooks the runtime consults
                           (public facade for extensions)
    - `ops.py`           — compatibility re-exports of the former monolith
  - `GUIDE.md`           — the manual an LLM reads via `nebi guide`
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    __version__: str  # resolved lazily from the installed distribution metadata (pyproject.toml is the single source)


def __getattr__(name: str) -> Any:
    if name == "__version__":  # lazy: importlib.metadata costs ~40 ms, and every `nebi` call imports this package
        from importlib.metadata import PackageNotFoundError, version

        try:
            value = version("nebi-cli")
        except PackageNotFoundError:  # running from a source tree that was never installed
            value = "0+unknown"
        globals()[name] = value
        return value
    raise AttributeError(f"module 'nebi_cli' has no attribute {name!r}")
