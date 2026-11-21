"""Command-line interface for `nebi-ai`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from nebi_ai.agent import PetAgentSession
from nebi_ai.bridge import RobotBridge, RobotTarget
from nebi_ai.config import DEFAULT_MODEL, MemoryStore


def _target_from_args(args: argparse.Namespace) -> RobotTarget:
    if args.daemon_url:
        return RobotTarget(daemon_url=args.daemon_url)
    if args.sim:
        return RobotTarget(sim=True)
    if args.host:
        return RobotTarget(host=args.host, interface=args.interface or "")
    return RobotTarget(flow=args.flow or "auto", interface=args.interface or "")


def _apply_cli_config(store: MemoryStore, args: argparse.Namespace) -> Any:
    cfg = store.load_config()
    updates: dict[str, Any] = {}
    if getattr(args, "model", None):
        updates["model"] = args.model
    if getattr(args, "live_model", None):
        updates["live_model"] = args.live_model
    if getattr(args, "no_live", False):
        updates["live_enabled"] = False
    if getattr(args, "pet_name", None):
        updates["pet_name"] = args.pet_name
    if getattr(args, "no_autonomy", False):
        updates["autonomy_enabled"] = False
    if getattr(args, "interval", None) is not None:
        updates["autonomy_interval_sec"] = float(args.interval)
    if updates:
        cfg = cfg.with_updates(updates)
        store.save_config(cfg)
    return cfg


def _cmd_run(args: argparse.Namespace) -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    store = MemoryStore(Path(args.memory_dir) if args.memory_dir else None)
    cfg = _apply_cli_config(store, args)
    target = _target_from_args(args)
    bridge = RobotBridge(target)
    session = PetAgentSession(bridge, memory_store=store, config=cfg)

    if args.no_web:
        async def _headless() -> None:
            await bridge.start()
            session.start_autonomy()
            print(f"{cfg.pet_name} is awake (target={target.label}, model={cfg.model}, memory={store.memories_dir})")
            try:
                await asyncio.Event().wait()
            finally:
                await session.stop_autonomy()
                await bridge.stop()

        try:
            asyncio.run(_headless())
        except KeyboardInterrupt:
            pass
        return 0

    from nebi_ai.web import create_web_app

    app = create_web_app(session, manage_lifecycle=True, start_autonomy=True)
    url = f"http://{args.web_host}:{args.web_port}/"
    print(f"{cfg.pet_name} Web UI: {url}")
    print(f"  target: {target.label}")
    print(f"  live:   {cfg.live_model if cfg.live_enabled else 'disabled'}")
    print(f"  brain:  {cfg.model}")
    print(f"  memory: {store.memories_dir}")

    if args.open:
        def _open_later() -> None:
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=_open_later, daemon=True).start()

    uvicorn.run(app, host=args.web_host, port=args.web_port, log_level="warning")
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    store = MemoryStore(Path(args.memory_dir) if args.memory_dir else None)
    cfg = _apply_cli_config(store, args)
    bridge = RobotBridge(_target_from_args(args))
    session = PetAgentSession(bridge, memory_store=store, config=cfg)

    async def _once() -> int:
        await bridge.start()
        try:
            record = await session.chat(args.message)
            if args.json:
                print(json.dumps(record.to_dict(), indent=2, ensure_ascii=False))
            else:
                if record.tool_calls:
                    names = ", ".join(tc.get("name", "?") for tc in record.tool_calls)
                    print(f"[tools: {names}]", file=sys.stderr)
                if record.error:
                    print(f"Error: {record.error}", file=sys.stderr)
                    return 1
                print(record.reply)
            return 0
        finally:
            await bridge.stop()

    return asyncio.run(_once())


def _cmd_memory(args: argparse.Namespace) -> int:
    store = MemoryStore(Path(args.memory_dir) if args.memory_dir else None)
    store.ensure_default_memory()
    action = args.memory_cmd

    if action == "list":
        print(f"Memory directory: {store.memories_dir}")
        for item in store.list_memories():
            print(f"  {item['path']}  ({item['size']} B)")
        return 0
    if action == "show":
        print(store.read_memory(args.file))
        return 0
    if action == "reset":
        store.reset_memories()
        print(f"Reset memories in {store.memories_dir}")
        return 0
    return 1


def _add_robot_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--sim", action="store_true", help="Connect to the built-in Nebi simulator")
    p.add_argument("--host", default="", help="Robot IP address (e.g. 192.168.1.50)")
    p.add_argument("--flow", default="", help="Connection flow (auto, lan_sta, softap, usb_ncm)")
    p.add_argument("--interface", default="", help="Network interface for SoftAP / USB-NCM")
    p.add_argument(
        "--daemon-url",
        default="",
        help="Attach to an already running nebi-cockpit / nebi daemon URL (e.g. http://127.0.0.1:8000)",
    )
    p.add_argument("--model", default=None, help=f"LangChain provider:model string (default: {DEFAULT_MODEL})")
    p.add_argument("--pet-name", default=None, help="Pet name (default: Nebi)")
    p.add_argument("--memory-dir", default=None, help="Override home directory for memories & config (default: ~/.nebi-ai)")


def build_parser() -> argparse.ArgumentParser:
    """Build the `nebi-ai` argument parser."""
    parser = argparse.ArgumentParser(
        prog="nebi-ai",
        description="DeepAgents-powered autonomous pet robot agent and web UI for Nebi.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Connect to the robot, start the pet loop, and launch the Web UI")
    _add_robot_args(p_run)
    p_run.add_argument("--web-host", default="127.0.0.1", help="Web UI bind host (default: 127.0.0.1)")
    p_run.add_argument("--web-port", type=int, default=8780, help="Web UI bind port (default: 8780)")
    p_run.add_argument("--no-web", action="store_true", help="Run headless without the Web UI")
    p_run.add_argument("--no-live", action="store_true", help="Disable Gemini Live and use chunked STT fallback")
    p_run.add_argument("--live-model", default=None, help="Gemini Live model ID (default: gemini-3.8-live)")
    p_run.add_argument("--no-autonomy", action="store_true", help="Start with autonomous pet ticks paused")
    p_run.add_argument("--interval", type=float, default=None, help="Autonomous pet tick interval in seconds")
    p_run.add_argument("--open", action="store_true", help="Open the Web UI in your browser automatically")
    p_run.set_defaults(func=_cmd_run)

    p_chat = sub.add_parser("chat", help="Send a single chat message to the pet robot and exit")
    _add_robot_args(p_chat)
    p_chat.add_argument("message", help="Message to send to the pet")
    p_chat.add_argument("--json", action="store_true", help="Print the full JSON turn record")
    p_chat.set_defaults(func=_cmd_chat)

    p_mem = sub.add_parser("memory", help="Inspect or reset persistent pet memory files in ~/.nebi-ai/memories")
    p_mem.add_argument("--memory-dir", default=None, help="Override home directory (default: ~/.nebi-ai)")
    mem_sub = p_mem.add_subparsers(dest="memory_cmd", required=True)
    mem_sub.add_parser("list", help="List memory files")
    p_show = mem_sub.add_parser("show", help="Print a memory file")
    p_show.add_argument("file", default="MEMORY.md", nargs="?", help="Relative path inside memories/ (default: MEMORY.md)")
    mem_sub.add_parser("reset", help="Reset memories to the default template")
    p_mem.set_defaults(func=_cmd_memory)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `nebi-ai`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
