"""`nebi-cockpit` console script: `nebi-cockpit serve [--host --port --sim]` (no command == `serve`).

By default the cockpit starts with no robot connected (pick one in the connection menu); `--sim` starts the
built-in simulator instead.
"""

from __future__ import annotations

import argparse
import os
import sys

from nebi_cockpit.security import generate_token, is_loopback_host


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from nebi.extension_loader import ENV_DISABLE

    if args.no_extensions:
        os.environ[ENV_DISABLE] = "1"
    elif args.disable_extension:
        current = os.environ.get(ENV_DISABLE, "").strip()
        names = [n for n in current.split(",") if n.strip()] if current not in ("1", "true", "yes", "on", "all") else []
        os.environ[ENV_DISABLE] = ",".join([*names, *args.disable_extension])

    from nebi_cockpit.app import configure_security, create_app

    # Installed extensions plug in here; with nebi-cli installed that includes the `nebi` agent API (/agent/*),
    # so the `nebi` CLI shares this process and its single robot link.
    app = create_app(start_simulator=args.sim, port=args.port)

    # Exposed beyond loopback: remote peers must present a token (local CLI and helpers on 127.0.0.1 don't).
    token = None
    if not is_loopback_host(args.host):
        token = os.environ.get("PYNEBI_TOKEN") or generate_token()
    configure_security(app, bind_host=args.host, token=token)
    if token:
        shown = "<this-machine-ip>" if args.host in ("0.0.0.0", "::", "") else args.host
        _err(
            f"[nebi-cockpit] Listening on {args.host}:{args.port} (non-loopback): remote access needs the token.\n"
            f"[nebi-cockpit] Open  http://{shown}:{args.port}/?token={token}\n"
            f"[nebi-cockpit] (set PYNEBI_TOKEN to choose it; API clients send header X-Pynebi-Token)"
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nebi-cockpit",
        description=(
            "nebi web cockpit (control + live FPV video), compatible with the Beni robot (LZ110/LZ100). "
            "Unofficial, not affiliated with the robot's manufacturer. It physically moves the robot: supervise it "
            "and keep clear space. Use only with a robot you own; provided AS IS (Apache-2.0, see LICENSE)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", help="Start the FastAPI control + live FPV video web server")
    p_serve.add_argument("--host", default=os.environ.get("PYNEBI_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PYNEBI_PORT", "8000")))
    p_serve.add_argument("--sim", action="store_true", help="Start the built-in robot simulator (default: no robot; connect from the UI)")
    p_serve.add_argument("--no-sim", action="store_true", help=argparse.SUPPRESS)  # old flag: now the default
    p_serve.add_argument(
        "--disable-extension", action="append", default=[], metavar="NAME",
        help="Skip this installed extension (repeatable), e.g. `nebi-cli` to not host the `nebi` agent API",
    )
    p_serve.add_argument("--no-extensions", action="store_true", help="Load no extensions at all (PYNEBI_DISABLE_EXTENSIONS=1)")
    p_serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd is None:  # `nebi-cockpit` with no command == `nebi-cockpit serve` with its defaults
        args = parser.parse_args(["serve"])
    args.func(args)


if __name__ == "__main__":
    main()
