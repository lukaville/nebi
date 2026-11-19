"""`nebi` — agent-friendly command line for the Nebi robot.

Thin client: parses argv from `spec.COMMANDS` (core + the command specs of loaded extensions), forwards to the
daemon over HTTP (127.0.0.1), prints JSON. Only stdlib (and the stdlib-only spec layer) at module level so every
invocation starts fast.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from nebi_cli import spec as S
from nebi_cli.errors import AgentError
from nebi_cli.extensions import ensure_agent_spec, extension_guides
from nebi_cli.settings import CACHE_DIR, CONFIG_DIR, DEFAULT_PORT
from nebi_cli.settings import DAEMON_LOG as LOG_PATH

CONFIG_PATH = Path(os.environ.get("NEBI_CONFIG", CONFIG_DIR / "agent.json"))
COCKPIT_PORT = int(os.environ.get("PYNEBI_PORT", "8000"))
DAEMON_START_TIMEOUT_S = 25.0

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy localhost


class CliError(AgentError):
    """An AgentError raised on the CLI side (daemon unreachable, usage, ...)."""

    def __init__(self, code: str, message: str, hint: str = "", exit_code: int = S.EXIT_FAILED) -> None:
        super().__init__(code, message, hint=hint, exit_code=exit_code)

    def envelope(self, command: str) -> dict[str, Any]:
        return {"ok": False, "command": command, "error": {"code": self.code, "message": self.message, "hint": self.hint}, "exit_code": self.exit_code}


# =============================================================================== config


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def resolve_port(cli_port: int | None) -> int:
    if cli_port:
        return int(cli_port)
    if os.environ.get("NEBI_PORT"):
        return int(os.environ["NEBI_PORT"])
    cfg_port = load_config().get("port")
    if cfg_port:
        return int(cfg_port)
    # Prefer the user's running cockpit (`nebi-cockpit serve` hosts the agent API too): the robot accepts one client.
    if not daemon_alive(DEFAULT_PORT) and daemon_alive(COCKPIT_PORT):
        return COCKPIT_PORT
    return DEFAULT_PORT


# =============================================================================== daemon transport


def _http(port: int, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def daemon_alive(port: int) -> bool:
    try:
        return bool(_http(port, "GET", "/agent/ping", timeout=0.8).get("agent"))
    except (OSError, ValueError, urllib.error.URLError):
        return False


def spawn_daemon(port: int) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "ab") as log:  # the child keeps its own copy of the descriptor
        subprocess.Popen(
            [sys.executable, "-m", "nebi_cli.daemon", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=str(Path.home()),
        )
    deadline = time.monotonic() + DAEMON_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if daemon_alive(port):
            return
        time.sleep(0.15)
    raise CliError(
        "DAEMON_START_FAILED",
        f"Daemon did not come up on port {port} within 25s.",
        hint=f"See the log: {LOG_PATH} (port busy? try --port 8766).",
        exit_code=S.EXIT_UNAVAILABLE,
    )


def call(port: int, command: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        return _http(port, "POST", "/agent/call", {"command": command, "args": args}, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise CliError("DAEMON_HTTP_ERROR", f"Daemon returned HTTP {exc.code}.", hint=f"See {LOG_PATH}.") from exc
    except TimeoutError as exc:
        raise CliError("CLI_TIMEOUT", f"No reply from daemon within {timeout:.0f}s.", hint="Check `nebi status`.", exit_code=S.EXIT_TIMEOUT) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CliError(
            "DAEMON_UNREACHABLE",
            f"Cannot reach the nebi daemon on port {port}: {exc}",
            hint="Run `nebi up` (or `nebi up --sim` / `nebi up --host <ip>`).",
            exit_code=S.EXIT_UNAVAILABLE,
        ) from exc


def _target_args(target: dict[str, Any]) -> dict[str, Any]:
    return {k: target.get(k) for k in ("sim", "host", "flow", "interface") if target.get(k)}


def ensure_daemon(port: int, warnings: list[str]) -> None:
    """Make sure a daemon is running; auto-`up` with the saved target if it is not."""
    if daemon_alive(port):
        return
    target = load_config().get("target")
    if not target:
        raise CliError(
            "NOT_STARTED",
            "The nebi daemon is not running and no robot target is saved yet.",
            hint="Run `nebi up --sim` (simulator) or `nebi up --host <robot-ip>` / `nebi up --flow auto`; `nebi discover` finds robots.",
            exit_code=S.EXIT_UNAVAILABLE,
        )
    spawn_daemon(port)
    env = call(port, "connect", _target_args(target), timeout=90.0)
    if not env.get("ok"):
        err = env.get("error") or {}
        raise CliError(
            err.get("code", "CONNECT_FAILED"),
            "Auto-start: " + str(err.get("message", "connection failed")),
            hint=err.get("hint", ""),
            exit_code=int(env.get("exit_code") or S.EXIT_UNAVAILABLE),
        )
    warnings.append(f"Daemon was not running; auto-started it and connected to saved target {_target_args(target)}.")


# =============================================================================== argv parsing


class _JsonArgParser(argparse.ArgumentParser):
    """argparse that reports usage errors as JSON (exit 2) instead of free text."""

    def error(self, message: str) -> None:  # type: ignore[override]
        cmd = self.prog.split()[-1] if " " in self.prog else ""
        hint = f"See `nebi {cmd} --help` or `nebi schema {cmd}`." if cmd else "See `nebi --help` or `nebi schema`."
        raise CliError("USAGE", message, hint=hint, exit_code=S.EXIT_USAGE)


def _converter(kind: str) -> Callable[[str], Any]:
    def conv(v: str) -> Any:
        try:
            if kind == "int":
                return int(v, 0)
            if kind == "float":
                return float(v)
            if kind == "distance":
                return S.parse_distance(v)
            if kind == "duration":
                return S.parse_duration(v)
            if kind == "angle":
                return S.parse_angle(v)
            if kind == "json":
                return json.loads(v)
            if kind == "path":
                return str(Path(v).expanduser().resolve())
            return v
        except (ValueError, json.JSONDecodeError) as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    conv.__name__ = kind
    return conv


def build_parser() -> argparse.ArgumentParser:
    ensure_agent_spec()  # extension commands / views / wait events become part of the parser
    p = _JsonArgParser(
        prog="nebi",
        description=(
            "Agent-friendly CLI compatible with the Beni robot (LZ110/LZ100). JSON on stdout. Start with `nebi guide`.\n\n"
            "Unofficial, not affiliated with the robot's manufacturer. It physically moves the robot: supervise it\n"
            "and keep clear space. Use only with a robot you own; provided AS IS (Apache-2.0, see LICENSE)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=overview_text(),
    )
    _add_globals(p)
    sub = p.add_subparsers(dest="command", metavar="<command>", parser_class=_JsonArgParser)
    for c in S.COMMANDS:
        epilog = ""
        if c.examples:
            epilog += "examples:\n" + "\n".join(f"  {e}" for e in c.examples)
        if c.notes:
            epilog += ("\n\n" if epilog else "") + "notes:\n  " + c.notes
        sp = sub.add_parser(
            c.name,
            help=c.summary,
            description=c.summary,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        _add_globals(sp, suppress=True)
        for a in c.args:
            kw: dict[str, Any] = {"help": a.help}
            if a.choices:
                kw["choices"] = a.choices
            if a.positional:
                if (c.name == "run" and a.name == "commands") or a.type == "text":
                    kw["nargs"] = "*"
                elif not a.required:
                    kw["nargs"] = "?"
                kw["type"] = _converter(a.type)
                kw["default"] = a.default
                sp.add_argument(a.name, **kw)
            else:
                flags = [a.flag or "--" + a.name.replace("_", "-")]
                if a.short:
                    flags.append(a.short)
                kw["dest"] = a.name
                if a.type == "bool":
                    kw["action"] = "store_true"
                else:
                    kw["type"] = _converter(a.type)
                    kw["default"] = a.default
                    kw["metavar"] = {"str": "TEXT", "path": "PATH", "json": "JSON"}.get(a.type, a.type.upper())
                    if a.default is not None:
                        kw["help"] = f"{a.help} (default: {a.default})"
                sp.add_argument(*flags, **kw)
    return p


def _add_globals(p: argparse.ArgumentParser, suppress: bool = False) -> None:
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--port", type=int, default=d, help="Daemon port (env NEBI_PORT, default 8765).")
    p.add_argument("--pretty", action="store_true", default=d, help="Indented JSON output (default on a terminal).")
    p.add_argument("--compact", action="store_true", default=d, help="Single-line JSON output (default when piped).")
    p.add_argument("--no-state", dest="no_state", action="store_true", default=d, help="Omit the trailing `state` object.")


def overview_text() -> str:
    lines = ["commands:"]
    for g, title in S.GROUP_TITLES.items():
        names = [c.name for c in S.COMMANDS if c.group == g]
        lines.append(f"  {title + ':':<16} {' '.join(names)}")
    lines += [
        "",
        "quick start:",
        "  nebi up --sim                 # or: nebi up --host 192.0.2.10",
        "  nebi stand && nebi drive forward 1m && nebi look",
        "  nebi guide                    # full manual for agents",
        "",
        "exit codes: " + ", ".join(f"{k}={v.split(' (')[0]}" for k, v in S.EXIT_CODES.items()),
        "",
        "local tools (direct robot link, no daemon; `nebi tools --help`):",
        "  nebi setup-wifi | pair-ble | sim    nebi tools status | discover | say",
    ]
    return "\n".join(lines)


def ns_to_args(cmd: S.Cmd, ns: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in cmd.args:
        v = getattr(ns, a.name, None)
        if v is None or v is False:
            continue
        if a.type == "text":
            v = " ".join(v) if isinstance(v, list) else str(v)
            if not v:
                continue
        if isinstance(v, str) and a.type in ("json",) and v == a.default:
            v = json.loads(v)  # argparse does not run `type` on non-string defaults; json defaults are strings
        out[a.name] = v
    return out


# =============================================================================== local commands


def cmd_up(ns: argparse.Namespace, port: int, warnings: list[str]) -> dict[str, Any]:
    target = {k: getattr(ns, k) for k in ("sim", "host", "flow", "interface") if getattr(ns, k, None)}
    cfg = load_config()
    if not any(target.get(k) for k in ("sim", "host", "flow")):
        if not cfg.get("target"):
            raise CliError(
                "NO_TARGET",
                "No target given and none saved.",
                hint="Use `nebi up --sim`, `nebi up --host <ip>`, or `nebi up --flow auto` (`nebi discover` lists robots).",
                exit_code=S.EXIT_USAGE,
            )
        target = cfg["target"]
    spawned = False
    if not daemon_alive(port):
        spawn_daemon(port)
        spawned = True
    args = _target_args(target)
    if getattr(ns, "reconnect", False):
        args["reconnect"] = True
    env = call(port, "connect", args, timeout=120.0)
    env["command"] = "up"
    if env.get("ok"):
        cfg["target"] = _target_args(target)
        if getattr(ns, "port", None):
            cfg["port"] = port
        save_config(cfg)
        env["result"]["daemon_spawned"] = spawned
        env["result"]["next"] = "nebi state  |  nebi stand  |  nebi look  |  nebi guide"
    return env


def cmd_down(port: int) -> dict[str, Any]:
    if not daemon_alive(port):
        return {"ok": True, "command": "down", "result": {"was_running": False}}
    try:
        call(port, "shutdown", {}, timeout=15.0)
    except CliError:
        pass
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and daemon_alive(port):
        time.sleep(0.15)
    return {"ok": True, "command": "down", "result": {"was_running": True, "stopped": not daemon_alive(port)}}


def cmd_status(port: int) -> dict[str, Any]:
    cfg = load_config()
    if not daemon_alive(port):
        from nebi_cli.extensions import extension_report

        return {
            "ok": True,
            "command": "status",
            "result": {
                "daemon_running": False,
                "saved_target": cfg.get("target"),
                "extensions": extension_report(),
                "hint": "Run `nebi up` to start"
                + (" (reuses saved target)." if cfg.get("target") else " — e.g. `nebi up --sim` or `nebi up --host <ip>`."),
            },
        }
    env = call(port, "status", {}, timeout=10.0)
    env.setdefault("result", {})["daemon_running"] = True
    env["result"]["saved_target"] = cfg.get("target")
    env["result"]["log"] = str(LOG_PATH)
    return env


def cmd_discover(ns: argparse.Namespace) -> dict[str, Any]:
    import asyncio

    from nebi.network import discover_robots, get_current_wifi_ssid

    robots = asyncio.run(
        discover_robots(include_ble=not ns.no_ble, deep_lan_scan=bool(ns.deep), timeout=float(ns.timeout or 2.5))
    )
    found = [r.to_dict() for r in robots]
    return {
        "ok": True,
        "command": "discover",
        "result": {
            "robots": found,
            "current_wifi_ssid": get_current_wifi_ssid(),
            "hint": "Connect with `nebi up --host <ip>`." if found else "Nothing found. Powered on? Try `--deep`, a USB-C cable, or `nebi up --sim`.",
        },
    }


def guide_text() -> str:
    """The core manual plus one section per loaded extension (`NebiExtension.guide_markdown`)."""
    text = resources.files("nebi_cli").joinpath("GUIDE.md").read_text(encoding="utf-8")
    for name, md in extension_guides():
        text = text.rstrip("\n") + f"\n\n## Extension: {name}\n\n" + md.strip("\n") + "\n"
    return text


# =============================================================================== execution


def _http_timeout(cmd: S.Cmd, args: dict[str, Any]) -> float:
    t = cmd.timeout_s
    for k in ("timeout", "wait", "for"):
        if isinstance(args.get(k), (int, float)):
            t = max(t, float(args[k]) + 15.0)
    if isinstance(args.get("seconds"), (int, float)):  # listen: recording time comes on top of model/transcribe time
        t = max(t, cmd.timeout_s + float(args["seconds"]))
    return t


def execute(argv: list[str], parser: argparse.ArgumentParser) -> tuple[dict[str, Any] | str, argparse.Namespace | None]:
    """Run one command line. Returns (envelope or plain text, namespace)."""
    try:
        ns = parser.parse_args(argv)
    except CliError as err:
        name = next((t for t in argv if t in S.COMMANDS_BY_NAME), next((t for t in argv if not t.startswith("-")), ""))
        return err.envelope(name), None
    name = ns.command
    if not name:
        return parser.format_help(), ns
    cmd = S.COMMANDS_BY_NAME[name]
    port = resolve_port(getattr(ns, "port", None))
    warnings: list[str] = []
    try:
        if name == "guide":
            return guide_text(), ns
        if name == "schema":
            only = getattr(ns, "name", None)
            if only and only not in S.COMMANDS_BY_NAME:
                raise CliError("UNKNOWN_COMMAND", f"No command {only!r}.", hint="`nebi schema` lists all.", exit_code=S.EXIT_USAGE)
            return {"ok": True, "command": "schema", "result": S.schema_dict(only)}, ns
        if name == "up":
            return cmd_up(ns, port, warnings), ns
        if name == "down":
            return cmd_down(port), ns
        if name == "status":
            return cmd_status(port), ns
        if name == "discover":
            return cmd_discover(ns), ns
        if name == "run":
            return run_batch(ns, parser), ns

        args = ns_to_args(cmd, ns)
        ensure_daemon(port, warnings)
        env = _call_interruptible(port, cmd, args)
    except CliError as err:
        env = err.envelope(name)
    if warnings:
        env["warnings"] = warnings + list(env.get("warnings") or [])
    return env, ns


def _call_interruptible(port: int, cmd: S.Cmd, args: dict[str, Any]) -> dict[str, Any]:
    """Call the daemon; if the CLI is interrupted (Ctrl-C / SIGTERM from a harness) while the robot moves, send `stop`."""
    if not cmd.moves_robot:
        return call(port, cmd.name, args, _http_timeout(cmd, args))

    def _term(_sig: int, _frm: Any) -> None:
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGTERM, _term)
    try:
        return call(port, cmd.name, args, _http_timeout(cmd, args))
    except KeyboardInterrupt:
        try:
            call(port, "stop", {}, timeout=5.0)
        except CliError:
            pass
        raise CliError("INTERRUPTED", f"`{cmd.name}` interrupted; robot was sent `stop`.", exit_code=S.EXIT_INTERRUPTED) from None
    finally:
        signal.signal(signal.SIGTERM, old)


def run_batch(ns: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    lines: list[str] = list(ns.commands or [])
    if not lines and not sys.stdin.isatty():
        lines = sys.stdin.read().splitlines()
    steps: list[dict[str, Any]] = []
    ok, exit_code, last_state = True, 0, None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        argv = shlex.split(line)
        if argv and argv[0] == "nebi":
            argv = argv[1:]
        if argv and argv[0] in ("run", "up", "down", "tools", "setup-wifi", "pair-ble", "sim"):
            steps.append({"line": line, "ok": False, "error": {"code": "NOT_ALLOWED_IN_RUN", "message": f"`{argv[0]}` cannot be used inside `nebi run`."}})
            ok, exit_code = False, S.EXIT_USAGE
            if not ns.keep_going:
                break
            continue
        env, _ = execute(argv, parser)
        if isinstance(env, str):
            steps.append({"line": line, "ok": True, "text": env[:2000]})
            continue
        last_state = env.pop("state", last_state)
        step_exit = int(env.pop("exit_code", 0) or 0)
        env.pop("command", None)
        steps.append({"line": line, **env})
        if not env.get("ok"):
            ok = False
            exit_code = exit_code or step_exit or S.EXIT_FAILED
            if not ns.keep_going:
                break
    out: dict[str, Any] = {"ok": ok, "command": "run", "result": {"steps": steps, "completed": sum(1 for s in steps if s.get("ok")), "total": len(steps)}}
    if last_state is not None:
        out["state"] = last_state
    if not ok:
        out["exit_code"] = exit_code
        failed = next(s for s in steps if not s.get("ok"))
        out["error"] = {"code": "STEP_FAILED", "message": f"Step failed: {failed['line']}", "hint": (failed.get("error") or {}).get("hint", "")}
    return out


def emit(env: dict[str, Any] | str, ns: argparse.Namespace | None) -> int:
    if isinstance(env, str):
        sys.stdout.write(env if env.endswith("\n") else env + "\n")
        return 0
    exit_code = int(env.pop("exit_code", 0) or (0 if env.get("ok") else S.EXIT_FAILED))
    if ns is not None and getattr(ns, "no_state", False):
        env.pop("state", None)
    pretty = bool(ns is not None and getattr(ns, "pretty", False))
    compact = bool(ns is not None and getattr(ns, "compact", False))
    indent = 2 if (pretty or (sys.stdout.isatty() and not compact)) else None
    sys.stdout.write(json.dumps(env, indent=indent, ensure_ascii=False, default=str) + "\n")
    return exit_code


def _tool_argv(argv: list[str]) -> tuple[list[str], str] | None:
    """`nebi tools <tool> ...` / `nebi setup-wifi|pair-ble|sim ...` -> (argv for nebi_cli.tools, prog), else None."""
    from nebi_cli.tools import TOP_LEVEL_TOOLS

    if argv and argv[0] == "tools":
        return argv[1:], "nebi tools"
    if argv and argv[0] in TOP_LEVEL_TOOLS:
        return argv, "nebi"
    return None


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    tool = _tool_argv(argv)
    if tool is not None:  # local utilities: own argparse, no daemon, no agent spec
        from nebi_cli import tools

        tools.main(tool[0], prog=tool[1])
        return
    parser = build_parser()
    try:
        env, ns = execute(argv, parser)
        code = emit(env, ns)
    except KeyboardInterrupt:
        code = S.EXIT_INTERRUPTED
    except BrokenPipeError:
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
