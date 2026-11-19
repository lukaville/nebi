"""Host OS Wi-Fi control: current SSID lookup (cached) and opt-in joining of a network (`networksetup` / `nmcli`)."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import threading
import time
from typing import Any

from nebi.exceptions import NebiError
from nebi.network.interfaces import NEBI_SOFTAP_PREFIX, NEBI_USB_NCM_PREFIX, wait_for_interface_subnet

logger = logging.getLogger(__name__)

_SSID_CACHE_TTL = 5.0
_ssid_cache: dict[str | None, tuple[float, str | None]] = {}
_ssid_cache_lock = threading.Lock()


def get_current_wifi_ssid(interface: str | None = None) -> str | None:
    """Best-effort lookup of the host's currently associated Wi-Fi SSID (macOS / Linux).

    Blocking (runs `networksetup`/`ipconfig`/`nmcli`); results are cached for 5 s. From async code use
    `get_current_wifi_ssid_async`.
    """
    now = time.monotonic()
    with _ssid_cache_lock:
        hit = _ssid_cache.get(interface)
        if hit is not None and now - hit[0] < _SSID_CACHE_TTL:
            return hit[1]
    ssid = _query_current_wifi_ssid(interface)
    with _ssid_cache_lock:
        _ssid_cache[interface] = (time.monotonic(), ssid)
    return ssid


def _invalidate_ssid_cache() -> None:
    with _ssid_cache_lock:
        _ssid_cache.clear()


async def get_current_wifi_ssid_async(interface: str | None = None) -> str | None:
    return await asyncio.to_thread(get_current_wifi_ssid, interface)


def _query_current_wifi_ssid(interface: str | None) -> str | None:
    try:
        if sys.platform == "darwin":
            iface = interface or "en0"
            out = subprocess.check_output(
                ["networksetup", "-getairportnetwork", iface],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            ).strip()
            m = re.search(r"Current Wi-Fi Network:\s*(.+)$", out)
            if m:
                ssid = m.group(1).strip()
                if ssid and "<redacted>" not in ssid.lower():
                    return ssid

            out_sum = subprocess.check_output(
                ["ipconfig", "getsummary", iface],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            )
            m2 = re.search(r"^\s*SSID\s*:\s*(.+)$", out_sum, re.MULTILINE)
            if m2:
                ssid = m2.group(1).strip()
                if ssid and "<redacted>" not in ssid.lower():
                    return ssid
        elif sys.platform.startswith("linux"):
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            )
            for line in out.splitlines():
                if line.startswith("yes:"):
                    ssid = line[4:].strip()
                    if ssid:
                        return ssid
    except Exception as exc:  # best-effort: tools missing or SSID redacted by the OS
        logger.debug("Wi-Fi SSID lookup failed: %r", exc)
    return None


async def join_os_wifi(ssid: str, password: str = "", interface: str | None = None) -> dict[str, Any]:
    """Opt-in helper to join a Wi-Fi network (Nebi's SoftAP or Home Wi-Fi) via macOS `networksetup` or Linux `nmcli`.

    Secret handling: on Linux the password is fed to `nmcli --ask` over stdin, so it never appears in the
    process list. macOS `networksetup -setairportnetwork` has no stdin/file option, so there the password is
    an argv element and is briefly visible to other local users via `ps` while the command runs (~seconds).
    The password is never included in raised errors or logs.
    """
    stdin_data: bytes | None = None
    if sys.platform == "darwin":
        iface = interface or "en0"
        cmd = ["networksetup", "-setairportnetwork", iface, ssid]
        if password:
            cmd.append(password)  # unavoidable: see docstring
    elif sys.platform.startswith("linux"):
        cmd = ["nmcli"]
        if password:
            # `--ask` makes nmcli prompt for the missing secret; we answer on stdin instead of argv.
            cmd.append("--ask")
            stdin_data = (password + "\n").encode("utf-8")
        cmd.extend(["dev", "wifi", "connect", ssid])
        if interface:
            cmd.extend(["ifname", interface])
    else:
        raise NebiError(
            "OS_WIFI_UNSUPPORTED",
            f"Automatic OS Wi-Fi switching is not supported on platform '{sys.platform}'.",
            stage="joining_softap",
            recoverable=True,
            hint=f"Join Wi-Fi SSID '{ssid}' manually in OS settings, then click Connect.",
        )

    _invalidate_ssid_cache()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(stdin_data), timeout=18.0)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise NebiError(
            "OS_WIFI_JOIN_FAILED",
            f"OS Wi-Fi join for '{ssid}' timed out after 18 s.",
            stage="joining_softap",
            recoverable=True,
            hint=f"Join SSID '{ssid}' manually or verify the Wi-Fi password.",
            details={"ssid": ssid, "interface": interface},
        ) from None
    finally:
        _invalidate_ssid_cache()
    out_s = out_b.decode("utf-8", errors="replace")
    err_s = err_b.decode("utf-8", errors="replace")
    out_txt = f"{out_s} {err_s}".strip()
    if password:
        out_txt = out_txt.replace(password, "***")
    if proc.returncode != 0 or "Failed to join" in out_txt or "Error:" in out_txt:
        # On macOS Sequoia+, calling `networksetup -setairportnetwork en0 <SavedSSID>` without a password
        # can return `-3900 tmpErr` when CoreWLAN has already auto-rejoined the preferred Home Wi-Fi.
        if sys.platform == "darwin" and not password:
            lan_iface = await wait_for_interface_subnet(
                "",
                interface=interface or "en0",
                exclude_prefixes=(NEBI_SOFTAP_PREFIX, NEBI_USB_NCM_PREFIX, "169.254."),
                timeout=5.0,
            )
            if lan_iface is not None:
                return {"ok": True, "ssid": ssid, "interface": lan_iface.name, "ipv4": lan_iface.ipv4}
        raise NebiError(
            "OS_WIFI_JOIN_FAILED",
            f"OS Wi-Fi join for '{ssid}' failed: {out_txt or f'exit {proc.returncode}'}",
            stage="joining_softap",
            recoverable=True,
            hint=f"Join SSID '{ssid}' manually or verify the Wi-Fi password.",
            details={"ssid": ssid, "interface": interface, "password_provided": bool(password)},
        )
    return {"ok": True, "ssid": ssid, "interface": interface}
