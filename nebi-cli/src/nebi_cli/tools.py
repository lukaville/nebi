"""`nebi tools ...`: local robot utilities that talk to the robot (or a cockpit) directly, not via the agent daemon.

The former `nebi` CLI (minus `serve`, which is now `nebi-cockpit serve`):

    nebi tools sim | status | discover | pair-ble | say | setup-wifi

`setup-wifi`, `pair-ble` and `sim` are also top-level (`nebi setup-wifi ...`); `status`, `discover` and `say` exist
as agent commands with a different meaning (daemon status, quick discovery, agent speech), so their tool versions
are reachable only as `nebi tools status|discover|say`. Output: JSON on stdout, progress on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nebi.ble import NebiBleClient
from nebi.client import AsyncNebiClient
from nebi.exceptions import NebiError
from nebi.md_ts import DEFAULT_IDENTITY_STORE
from nebi.network import discover_robots, get_current_wifi_ssid, list_network_interfaces
from nebi.types import SOFTAP_HOST, ConnectionStage

FLOW_CHOICES = ["direct", "auto", "lan_sta", "usb_ncm", "softap", "ble_bootstrap"]
BUTTON_PROMPT = ">>> ACTION REQUIRED: {} <<<"
WIFI_CREDENTIALS = Path.home() / ".config" / "pynebi" / "wifi.json"  # {"ssid": ..., "password": ...}, chmod 600


def _err(msg: str) -> None:
    """Progress line on stderr (stdout stays machine-readable JSON)."""
    print(msg, file=sys.stderr, flush=True)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2))


async def _run_sim(args: argparse.Namespace) -> None:
    from nebi.simulator import MockNebiRobot  # dev/test infrastructure: only for this command

    sim = MockNebiRobot(host=args.host, ucp_port=args.ucp_port, http_port=args.http_port)
    await sim.start()
    print(
        f"MockNebiRobot running on UCP udp://{sim.host}:{sim.ucp_port} "
        f"and HTTP http://{sim.host}:{sim.http_port} (Ctrl+C to stop)"
    )
    try:
        await asyncio.Event().wait()  # until Ctrl+C cancels the run
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await sim.stop()


async def _run_status(args: argparse.Namespace) -> None:
    if args.flow != "direct":
        robot = await AsyncNebiClient.connect_flow(
            args.flow,
            host=args.robot_ip or None,
            bind_interface=args.interface or None,
        )
    else:
        robot = AsyncNebiClient(
            host=args.robot_ip or SOFTAP_HOST,
            ucp_port=args.ucp_port,
            config_port=args.config_port,
            media_port=args.media_port,
            bind_interface=args.interface or None,
        )
        await robot.connect(start_joystick_stream=False, auto_start_video=False)

    try:
        ping = await robot.system.ping()
        await asyncio.sleep(0.35)
        _print_json({"ping": ping, "telemetry": robot.telemetry.to_dict()})
    finally:
        await robot.close()


async def _run_discover(args: argparse.Namespace) -> None:
    ifaces = [i.to_dict() for i in list_network_interfaces()]
    robots = [
        r.to_dict()
        for r in await discover_robots(
            include_ble=not args.no_ble,
            deep_lan_scan=args.deep_lan,
            timeout=args.timeout,
        )
    ]
    _print_json(
        {
            "current_wifi_ssid": get_current_wifi_ssid(),
            "md_ts_identity": DEFAULT_IDENTITY_STORE.summary_dict(),
            "interfaces": ifaces,
            "robots": robots,
        }
    )


async def _run_pair_ble(args: argparse.Namespace) -> None:
    _err(
        f"[nebi] Client identity: {DEFAULT_IDENTITY_STORE.identity.client_id.hex()} "
        f"(saved at {DEFAULT_IDENTITY_STORE.path})"
    )
    ble = NebiBleClient(security_mode="md-ts" if args.require_md_ts else "auto")
    try:
        _err("[nebi] Scanning / connecting to Nebi BLE GATT (0000FFF0)...")
        await ble.connect(args.ble_address or None, timeout=8.0)
        _err(f"[nebi] Connected to {ble.connected_name or 'Nebi'} ({ble.connected_address})")
        creds = await ble.bootstrap_wifi_credentials(
            country_code=args.country_code,
            wait_for_button_sec=args.wait_button_sec,
            on_awaiting_button=lambda: _err(
                "\n" + BUTTON_PROMPT.format("Hold Nebi's physical button for 3-5 seconds until it chimes!") + "\n"
            ),
        )
        _print_json({"ok": True, "credentials": creds.to_dict(), "identity": DEFAULT_IDENTITY_STORE.summary_dict()})
    finally:
        await ble.close()


def _saved_wifi() -> dict[str, Any]:
    """Saved home Wi-Fi credentials (Nebi must be re-provisioned after every power-on, so don't ask each time)."""
    try:
        saved = json.loads(WIFI_CREDENTIALS.read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


async def _run_setup_wifi(args: argparse.Namespace) -> None:
    saved = _saved_wifi()
    ssid = args.ssid or saved.get("ssid") or get_current_wifi_ssid(args.interface or None) or ""
    if not ssid:
        raise SystemExit("Error: --ssid is required (could not auto-detect current OS Wi-Fi SSID).")
    password = args.password
    if password is None and saved.get("ssid") == ssid and "password" in saved:
        password = saved["password"]
    if password is None and not args.open_network:
        password = getpass.getpass(f"Wi-Fi password for '{ssid}': ")
    password = password or ""

    def _on_stage(st: ConnectionStage, detail: str, _error: NebiError | None) -> None:
        if st == ConnectionStage.BLE_AWAITING_BUTTON_AUTH:
            _err("\n" + BUTTON_PROMPT.format(detail) + "\n")
        else:
            _err(f"[{st.value}] {detail}")

    res, client = await AsyncNebiClient.pair_ble_and_provision_home_wifi(
        ssid=ssid,
        password=password,
        security=0 if not password else 2,
        ble_address=args.ble_address or None,
        country_code=args.country_code,
        wifi_interface=args.interface or None,
        switch_os_wifi=not args.no_os_wifi_switch,
        connect_lan_after=not args.no_lan_verify,
        wait_for_button_sec=args.wait_button_sec,
        timeout_sec=args.timeout_sec,
        on_stage=_on_stage,
    )
    ping = None
    if client is not None:
        try:
            ping = await client.system.ping()
        finally:
            await client.close()
    _print_json({"ok": True, "provision": res, "lan_ping": ping})


def _cmd_say(args: argparse.Namespace) -> None:
    from nebi_cli.intercom_say import main_say  # pulls in TTS / audio helpers only for this command

    main_say(args)


def _async(fn: Callable[[argparse.Namespace], Any]) -> Callable[[argparse.Namespace], None]:
    return lambda args: asyncio.run(fn(args))


def build_parser(prog: str = "nebi tools") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="Local Nebi robot utilities (direct robot link / cockpit intercom; no agent daemon)"
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<tool>")

    p_sim = sub.add_parser("sim", help="Run standalone MockNebiRobot protocol & H.265 video simulator")
    p_sim.add_argument("--host", default="127.0.0.1")
    p_sim.add_argument("--ucp-port", type=int, default=19007)
    p_sim.add_argument("--http-port", type=int, default=18080)
    p_sim.set_defaults(func=_async(_run_sim))

    p_stat = sub.add_parser("status", help="Connect to a Nebi robot via any local flow and print JSON telemetry")
    p_stat.add_argument("--flow", default="direct", choices=FLOW_CHOICES)
    p_stat.add_argument("--robot-ip", default=SOFTAP_HOST)
    p_stat.add_argument("--interface", default="", help="Bind sockets to OS interface (e.g. en7, wlan1)")
    p_stat.add_argument("--ucp-port", type=int, default=9007)
    p_stat.add_argument("--config-port", type=int, default=8010)
    p_stat.add_argument("--media-port", type=int, default=8084)
    p_stat.set_defaults(func=_async(_run_status))

    p_disc = sub.add_parser("discover", help="Scan USB-NCM, Home Wi-Fi LAN, SoftAP, and BLE for Nebi robots")
    p_disc.add_argument("--timeout", type=float, default=2.0)
    p_disc.add_argument("--no-ble", action="store_true")
    p_disc.add_argument("--deep-lan", action="store_true", help="Run active /24 Home LAN TCP :8010 sweep")
    p_disc.set_defaults(func=_async(_run_discover))

    p_pair = sub.add_parser("pair-ble", help="Pair nebi's X25519 identity over BLE (MD-TS) and fetch SoftAP credentials")
    p_pair.add_argument("--ble-address", default="", help="Optional BLE address/UUID (blank = scan nearby Nebi-*)")
    p_pair.add_argument("--country-code", default="US")
    p_pair.add_argument("--wait-button-sec", type=float, default=35.0)
    p_pair.add_argument("--require-md-ts", action="store_true", help="Require encrypted MD-TS handshake")
    p_pair.set_defaults(func=_async(_run_pair_ble))

    p_say = sub.add_parser("say", help="Speak text (TTS) or play a WAV through Nebi's speaker via the cockpit intercom")
    p_say.add_argument("text", nargs="*", default=["Hello, I am Nebi. Intercom test."])
    p_say.add_argument("--wav", help="Audio file to play instead of text")
    p_say.add_argument("--voice", help="TTS voice preset (system engine: an installed OS voice, e.g. 'Samantha')")
    p_say.add_argument("--engine", help="TTS engine (default: $PYNEBI_TTS_ENGINE, else system, else chirp)")
    p_say.add_argument("--gain", type=float, default=1.0, help="Linear gain applied before sending")
    p_say.add_argument("--server", default="http://127.0.0.1:8000", help="Running `nebi-cockpit serve` cockpit URL")
    p_say.add_argument("--keep", action="store_true", help="Leave the intercom running afterwards")
    p_say.set_defaults(func=_cmd_say)

    p_wifi = sub.add_parser(
        "setup-wifi",
        help="Pair via BLE (MD-TS), provision Nebi onto Home Wi-Fi (STA), restore host Wi-Fi, and connect over LAN",
    )
    p_wifi.add_argument("--ssid", default="", help="Home Wi-Fi SSID (default: ~/.config/pynebi/wifi.json, else the current host Wi-Fi SSID)")
    p_wifi.add_argument("--password", default=None, help="Home Wi-Fi password (default: ~/.config/pynebi/wifi.json; prompts securely if neither)")
    p_wifi.add_argument("--open-network", action="store_true", help="Connect to an open Wi-Fi network without WPA2 PSK")
    p_wifi.add_argument("--ble-address", default="", help="Optional BLE address/UUID")
    p_wifi.add_argument("--country-code", default="US")
    p_wifi.add_argument("--interface", default="", help="Host Wi-Fi interface (e.g. en0 on macOS)")
    p_wifi.add_argument("--wait-button-sec", type=float, default=35.0)
    p_wifi.add_argument("--timeout-sec", type=int, default=30)
    p_wifi.add_argument("--no-os-wifi-switch", action="store_true", help="Do not switch host OS Wi-Fi to SoftAP")
    p_wifi.add_argument("--no-lan-verify", action="store_true", help="Do not open LAN session after cutover")
    p_wifi.set_defaults(func=_async(_run_setup_wifi))
    return parser


TOOL_NAMES = ("sim", "status", "discover", "pair-ble", "say", "setup-wifi")
TOP_LEVEL_TOOLS = ("setup-wifi", "pair-ble", "sim")  # no clash with an agent command: also `nebi <tool>`


def main(argv: list[str] | None = None, *, prog: str = "nebi tools") -> None:
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        raise SystemExit(0)
    try:
        args.func(args)
    except NebiError as err:
        print(json.dumps({"ok": False, "error": err.to_dict()}, indent=2), file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
