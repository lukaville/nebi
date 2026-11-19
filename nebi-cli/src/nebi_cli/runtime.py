"""AgentRuntime: the composition root of the `nebi` daemon.

It owns the services every command shares and dispatches commands to the `@command` handlers of the
command groups (one module per area):

  services  motion.MotionGate (lock, epochs, bounded holds) · activity.ActivityTracker · safety_ops.SafetyGate ·
            voice_ops.SpeechService · express_ops.Expression · idle_ops.IdleLoop · events.EventLog + TelemetryWatcher
  commands  session_ops · perceive_ops · locomotion_ops · camera_ops · system_ops · express_ops · voice_ops ·
            idle_ops · pet_ops
  extensions  `hooks` (extensions.AgentHooks: carried state, people, event sources, look views, wait events, care,
            look-at targets) and `services`, filled by each loaded extension's `setup_agent` (nebi.extensions)

Design rules (they are what makes the CLI pleasant for LLM agents):
  * Every motion is bounded (distance/duration caps) and always ends with axes back at neutral.
  * `stop` preempts any running drive/turn/move from another CLI process (motion epochs).
  * Every reply carries a compact post-action `state`, so agents rarely need a follow-up `nebi state`.
  * Errors are structured: `code`, `message`, `hint` (the next command to try) and an exit code.
  * A background watcher turns telemetry diffs into an event log (`nebi events`) and powers `nebi wait`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from nebi.exceptions import NebiError, NebiLinkLostError, NebiNotConnectedError, NebiRpcTimeoutError
from nebi_cli import spec as S
from nebi_cli.activity import ActivityTracker
from nebi_cli.camera_ops import CameraCommands
from nebi_cli.errors import AgentError, bad_arg
from nebi_cli.events import EventLog, TelemetryWatcher
from nebi_cli.express_ops import ExpressCommands, Expression
from nebi_cli.extensions import AgentHooks, setup_agent_extensions
from nebi_cli.idle_ops import IdleCommands, IdleLoop
from nebi_cli.locomotion_ops import LocomotionCommands
from nebi_cli.motion import MotionGate
from nebi_cli.perceive_ops import PerceiveCommands
from nebi_cli.pet_ops import PetCommands
from nebi_cli.registry import CommandGroup, Handler, collect_handlers
from nebi_cli.safety_ops import SafetyGate
from nebi_cli.session_ops import SessionCommands
from nebi_cli.settings import DAEMON_LOG, DEFAULT_PORT
from nebi_cli.system_ops import SystemCommands
from nebi_cli.views import compact_state
from nebi_cli.voice_ops import SpeechService, VoiceCommands

if TYPE_CHECKING:
    from nebi.client import AsyncNebiClient
    from nebi_cockpit.app import NebiServerManager

logger = logging.getLogger(__name__)


def agent_error_from_nebi(err: NebiError) -> AgentError:
    """Map a robot/transport NebiError to an AgentError with an exit code and an actionable hint."""
    exit_code = S.EXIT_FAILED
    if isinstance(err, (NebiNotConnectedError, NebiLinkLostError)):
        exit_code = S.EXIT_UNAVAILABLE
    elif err.code in ("ROBOT_CHARGING_LOCKED", "POSTURE_UNKNOWN"):
        exit_code = S.EXIT_SAFETY
    elif isinstance(err, NebiRpcTimeoutError):
        exit_code = S.EXIT_TIMEOUT
    elif err.code.startswith("LED_"):  # bad eye arguments (nebi.domains.led)
        exit_code = S.EXIT_USAGE
    hint = err.hint
    if err.code == "POSTURE_UNKNOWN" and not hint:
        hint = ("No fresh posture report from the robot yet. Wait a second and retry (`nebi state` shows posture); "
                "if you can see it is folded and safe, `nebi stand --force`.")
    if isinstance(err, NebiRpcTimeoutError) and not hint:
        hint = "The robot did not answer in time. Check `nebi status`; retry once, then `nebi up --reconnect`."
    if isinstance(err, NebiNotConnectedError):
        hint = "Run `nebi up` (or `nebi up --sim` / `nebi up --host <ip>`), then retry."
    return AgentError(err.code, err.message, hint=hint, exit_code=exit_code, details=err.details or None)


class AgentRuntime:
    """Agent-level behaviour on top of the cockpit's `NebiServerManager` connection."""

    def __init__(self, manager: NebiServerManager, *, port: int = DEFAULT_PORT, embedded: bool = False) -> None:
        self.manager = manager
        self.port = port
        self.embedded = embedded  # True: hosted inside the user's `nebi-cockpit serve` cockpit
        self.started_at = time.time()
        # shared services
        self.events = EventLog()
        self.motion = MotionGate()
        self.activity = ActivityTracker()
        self.hooks = AgentHooks()
        self.services: dict[str, Any] = {}
        self.safety = SafetyGate(self.hooks)
        self.speech = SpeechService(self.activity)
        self.expression = Expression(self)
        self.idle = IdleLoop(self)
        self._watcher = TelemetryWatcher(manager, self.events)
        self._background: set[asyncio.Task[Any]] = set()
        self._request_shutdown: Callable[[], None] | None = None
        groups: list[CommandGroup] = [
            SessionCommands(self), PerceiveCommands(self), LocomotionCommands(self), CameraCommands(self),
            SystemCommands(self), ExpressCommands(self), VoiceCommands(self), IdleCommands(self), PetCommands(self),
        ]
        self.handlers: dict[str, Handler] = collect_handlers(groups)
        setup_agent_extensions(self)  # adds extension commands to `handlers` and fills `hooks` / `services`

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._watcher.start()

    async def stop(self) -> None:
        await self.idle.stop()  # first: it uses the extension event sources and the robot link
        await self._watcher.stop()
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.wait(self._background, timeout=2.0)
        for cb in list(self.hooks.shutdown):
            try:
                await cb()
            except Exception:  # an extension's cleanup failure must not block shutdown
                logger.warning("extension shutdown callback failed", exc_info=True)

    def set_shutdown_callback(self, cb: Callable[[], None]) -> None:
        """How `nebi down` ends the standalone daemon process (set by daemon.main)."""
        self._request_shutdown = cb

    def request_shutdown(self, delay_s: float = 0.2) -> bool:
        """Schedule process shutdown after `delay_s` (so the reply still goes out). False when not standalone."""
        if self._request_shutdown is None:
            return False
        asyncio.get_running_loop().call_later(delay_s, self._request_shutdown)
        return True

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Run a fire-and-forget coroutine: referenced until done, failures logged, cancelled on stop()."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("background task %s failed: %r", name, t.exception())

        task.add_done_callback(_done)
        return task

    # ------------------------------------------------------------------ dispatch

    async def call(self, command: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one command from outside (`POST /agent/call`) and return the JSON reply envelope."""
        handler = self.handlers.get(command.replace("-", "_"))
        warnings: list[str] = []
        env: dict[str, Any] = {"ok": False, "command": command}
        try:
            if handler is None:
                raise AgentError(
                    "UNKNOWN_COMMAND",
                    f"Unknown command {command!r}.",
                    hint="Run `nebi schema` for the list of commands.",
                    exit_code=S.EXIT_USAGE,
                )
            # Internal keys (`_warnings`, `_auto`, `_speed_cap`, ...) can never come from outside.
            a = {k: v for k, v in dict(args or {}).items() if not str(k).startswith("_")}
            spec = S.COMMANDS_BY_NAME.get(command)
            if spec is not None:
                try:
                    a = S.coerce_args(spec, a)
                except ValueError as exc:
                    raise bad_arg(str(exc), hint=f"See `nebi schema {command}`.") from None
            a["_warnings"] = warnings
            await self.safety.precheck(command, a)
            tok = self.activity.begin(command) if spec is not None and spec.moves_robot else None
            try:
                result = await handler(a)
            finally:
                if tok is not None:
                    self.activity.end(tok)
            env.update(ok=True, result=result)
        except AgentError as err:
            env.update(ok=False, error=err.to_dict(), exit_code=err.exit_code)
        except NebiError as err:
            ae = agent_error_from_nebi(err)
            env.update(ok=False, error=ae.to_dict(), exit_code=ae.exit_code)
        except asyncio.TimeoutError:
            env.update(
                ok=False,
                error={"code": "TIMEOUT", "message": "Operation timed out.", "hint": "Check `nebi status` and retry."},
                exit_code=S.EXIT_TIMEOUT,
            )
        except Exception as exc:
            # Arguments were validated above, so anything else (KeyError/TypeError/ValueError included) is a bug
            # or an unexpected robot/extension payload, not the caller's fault.
            logger.exception("command %s crashed", command)
            env.update(
                ok=False,
                error={
                    "code": "INTERNAL_ERROR",
                    "message": f"`{command}` failed internally: {type(exc).__name__}: {exc}",
                    "hint": f"Not an argument problem. Retry once; check `nebi status`; details in the daemon log ({DAEMON_LOG}).",
                },
                exit_code=S.EXIT_FAILED,
            )
        if warnings:
            env["warnings"] = warnings
        try:
            env["state"] = compact_state(self.manager, self.manager.client)
        except Exception:  # the reply must go out even if telemetry is mid-reconnect
            logger.debug("compact_state failed", exc_info=True)
        return env

    async def invoke(self, command: str, args: dict[str, Any], *, source: str = "") -> Any:
        """Run another command from inside a command or the idle loop with the same safety precheck and activity
        tracking as a top-level `call()` (so nested stand/turn/body/say obey the carried rules too)."""
        handler = self.handlers[command]
        a = dict(args)
        a.setdefault("_warnings", [])
        await self.safety.precheck(command, a)
        spec = S.COMMANDS_BY_NAME.get(command)
        tok = self.activity.begin(f"{source}:{command}" if source else command) if spec and spec.moves_robot else None
        try:
            return await handler(a)
        finally:
            if tok is not None:
                self.activity.end(tok)

    # ------------------------------------------------------------------ shared accessors

    def client(self) -> AsyncNebiClient:
        """The connected robot client, or NOT_CONNECTED with the connection's last error as a hint."""
        c = self.manager.client
        if c is None or not c.connected:
            conn = self.manager.connection_summary()
            last = conn.get("last_error") or {}
            raise AgentError(
                "NOT_CONNECTED",
                f"Robot is not connected (stage: {conn.get('stage')}).",
                hint=(last.get("hint") or "")
                + (" " if last.get("hint") else "")
                + "Run `nebi up` (or `nebi up --sim` / `nebi up --host <ip>`).",
                exit_code=S.EXIT_UNAVAILABLE,
                details={"last_error": last} if last else None,
            )
        return c

    def connected_client(self) -> AsyncNebiClient | None:
        """The robot client when connected, else None (for commands that also work without the robot)."""
        c = self.manager.client
        return c if c is not None and c.connected else None

    def activity_snapshot(self) -> dict[str, Any]:
        """For the cockpit's 'agent driving' pill (`GET /agent/activity`)."""
        out = self.activity.snapshot()
        out["idle_running"] = self.idle.running()
        return out
