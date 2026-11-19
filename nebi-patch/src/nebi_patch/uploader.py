"""Hardened streaming HTTP/HTTPS (`:8085`) firmware uploader & Zenoh OTA controller for Beni."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import ssl
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nebi_patch.packer import md5_file, parse_mass_cfg, sha256_file
from nebi_patch.validator import SafetyValidationError, verify_package

# Verified against md_upgrade (:8085 /upload /list /files/*) and the firmware OTA client.
DEFAULT_OTA_PORT = 8085
DEFAULT_UCP_PORT = 9007
MIN_SAFE_BATTERY_PCT = 30

# OtaUpgradeControlReqData.control_cmd values:
OTA_CMD_START = 0             # "create" / Start (creates v2 task & mints Upload-Token, or starts v1 inbox upgrade)
OTA_CMD_CANCEL = 1            # "cancel" / Cancel (WARNING: 1 is Cancel, NOT Start!)
OTA_CMD_CONFIRM_COMPLETE = 2  # ConfirmComplete
OTA_CMD_QUERY = 3             # "query" / Query
OTA_CMD_INSTALL = 4           # "install" / Install
OTA_CMD_CAPABILITY_PROBE = 127

UNSAFE_ACTIVE_OTA_STATES: frozenset[str] = frozenset(
    {"INSTALLING", "AWAITING_REBOOT", "WAITING_REBOOT", "CONFIRMING", "RECOVERY_REQUIRED"}
)

# RobotPostureState values where leg motors are actively supporting/moving the robot:
UNSAFE_MOTION_STATES: dict[int, str] = {
    1: "STANDING_UP",
    2: "BALANCING",
    3: "FOLDING",
    4: "JUMPING",
    5: "FLIPPING",
    6: "DOCKING",
}

_BEARER_TOKEN_ENV_VARS = ("NEBI_HTTP_BEARER_TOKEN", "PYNEBI_MEDIA_TOKEN")


def _media_token_file() -> Path:
    return Path.home() / ".config" / "pynebi" / "media_token"


def resolve_bearer_token(bearer_token: str | None) -> str:
    """Resolve the optional HTTP Bearer token for `:8085`.

    Resolution order: explicit argument > `$NEBI_HTTP_BEARER_TOKEN` > `$PYNEBI_MEDIA_TOKEN` >
    `~/.config/pynebi/media_token`. Returns `""` if none is configured; nebi does not ship a retail token.
    """
    if bearer_token is not None:
        return bearer_token.strip()
    for env_var in _BEARER_TOKEN_ENV_VARS:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    try:
        return _media_token_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _auth_headers(bearer_token: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    token = resolve_bearer_token(bearer_token)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra:
        headers.update(extra)
    return headers


@dataclass
class UploadResult:
    """Result summary returned by `upload_firmware()`."""

    mode: str
    scheme: str
    host: str
    port: int
    filename: str
    size_bytes: int
    md5: str
    sha256: str
    status_code: int
    response_body: str
    elapsed_sec: float
    upgrade_id: str | None = None
    upload_token: str | None = None
    remote_verified: bool = False
    warnings: list[str] = field(default_factory=list)


def _import_nebi_client() -> Any:
    try:
        from nebi import AsyncNebiClient

        return AsyncNebiClient
    except ImportError as exc:
        raise RuntimeError(
            "nebi-lib is not installed; install `nebi-patch[nebi]` for Zenoh/UCP V2 OTA and pre-flight checks."
        ) from exc


def _extract_pkg_version(zip_path: Path) -> str:
    if zipfile.is_zipfile(zip_path):
        with zipfile.ZipFile(zip_path, "r") as zf:
            cfgs = [n for n in zf.namelist() if n.endswith(".mass.cfg.plain.unsign")]
            if cfgs:
                cfg = parse_mass_cfg(zf.read(cfgs[0]))
                ver = str(cfg.get("package", {}).get("version", ""))
                if ver:
                    return ver
    return zip_path.stem.lstrip("Vv").split("_", 1)[0]


def _make_connection(host: str, port: int, scheme: str, timeout: float) -> http.client.HTTPConnection:
    if scheme == "https":
        # Device OTA server uses a self-signed certificate on the local robot network.
        ctx = ssl._create_unverified_context()
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=ctx)
    if scheme == "http":
        return http.client.HTTPConnection(host, port=port, timeout=timeout)
    raise ValueError(f"Unsupported scheme: {scheme!r}")


def _read_response_after_stream(
    conn: http.client.HTTPConnection,
    send_err: BaseException | None,
) -> tuple[int, str]:
    try:
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body
    except Exception:
        if send_err is not None:
            raise send_err from None
        raise


def detect_scheme(host: str, port: int = DEFAULT_OTA_PORT, timeout: float = 5.0) -> str:
    """Probe `md_upgrade` on `host:port` to determine whether HTTP or HTTPS responds."""
    last_err: Exception | None = None
    for scheme in ("http", "https"):
        conn: http.client.HTTPConnection | None = None
        try:
            conn = _make_connection(host, port, scheme, timeout)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            if resp.status < 500:
                return scheme
        except Exception as exc:
            last_err = exc
        finally:
            if conn is not None:
                conn.close()
    raise ConnectionError(f"Could not reach md_upgrade on {host}:{port} via HTTP or HTTPS: {last_err}")


def check_robot_ota(
    host: str = "192.168.7.1",
    port: int = DEFAULT_OTA_PORT,
    scheme: str = "auto",
    timeout: float = 5.0,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """Query `GET /health` and `GET /list` on the robot's `md_upgrade` server."""
    resolved = detect_scheme(host, port, timeout) if scheme == "auto" else scheme
    headers = _auth_headers(bearer_token)
    out: dict[str, Any] = {"scheme": resolved, "host": host, "port": port}
    for path, key in (("/health", "health"), ("/list", "packages")):
        conn = _make_connection(host, port, resolved, timeout)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = {"status": resp.status, "body": raw}
        finally:
            conn.close()
    return out


def _verify_file_in_remote_list(
    host: str,
    port: int,
    scheme: str,
    filename: str,
    expected_size: int,
    timeout: float = 5.0,
    bearer_token: str | None = None,
) -> tuple[bool, str | None]:
    """Check `GET /list` after V1 upload to verify the package size if reported by the server."""
    conn = _make_connection(host, port, scheme, timeout)
    try:
        conn.request("GET", "/list", headers=_auth_headers(bearer_token))
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400:
            return False, None
        data = json.loads(raw)
    except Exception:
        return False, None
    finally:
        conn.close()

    entries: list[Any] = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key in ("files", "packages", "items", "entries"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break

    for item in entries:
        if isinstance(item, str) and (item == filename or item.endswith("/" + filename)):
            return True, None
        if isinstance(item, dict):
            item_name = str(item.get("name") or item.get("filename") or item.get("path") or "")
            if item_name == filename or item_name.endswith("/" + filename):
                if "size" in item and int(item["size"]) != expected_size:
                    return False, (
                        f"Remote `/list` size mismatch for {filename}: "
                        f"expected {expected_size} bytes, robot has {item['size']} bytes!"
                    )
                return True, None
    return False, None


async def preflight_and_create_zenoh_task(
    *,
    host: str,
    ucp_port: int = DEFAULT_UCP_PORT,
    ota_port: int = DEFAULT_OTA_PORT,
    create_task: bool = False,
    filename: str = "",
    file_size: int = 0,
    sha256_hex: str = "",
    version: str = "",
    upgrade_id: str | None = None,
    device_id: str | None = None,
    min_battery_pct: int = MIN_SAFE_BATTERY_PCT,
    ignore_battery: bool = False,
    check_posture: bool = False,
    ignore_posture: bool = False,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Run live battery, posture, and active-task checks over Zenoh, and optionally mint an `Upload-Token`."""
    client_cls = _import_nebi_client()

    pmu_seen: dict[str, Any] = {}
    posture_seen: dict[str, Any] = {}

    def _on_msg(topic: str, msg: Any, _seq: int) -> None:
        data = getattr(msg, "data", None)
        if data is None:
            return
        if topic == "/status/pmu/osd_status":
            pbits = int(data.power_bits)
            pmu_seen["soc"] = int(data.soc)
            pmu_seen["charging"] = bool(pbits & 0x01)
            pmu_seen["docked"] = bool(pbits & 0x02)
        elif topic == "/status/robot/status":
            posture_seen["robot_state"] = int(data.robot_state)
            posture_seen["velocity_cms"] = int(data.velocity)

    async with client_cls(host=host, ucp_port=ucp_port, ota_port=ota_port) as robot:
        robot.bus.subscribe_all(_on_msg)
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline and (not pmu_seen or (check_posture and not posture_seen)):
            await asyncio.sleep(0.1)

        if pmu_seen and not ignore_battery:
            soc = pmu_seen["soc"]
            charging = pmu_seen["charging"] or pmu_seen["docked"]
            if soc < min_battery_pct and not charging:
                raise SafetyValidationError(
                    f"Robot battery too low for safe flashing: {soc}% (minimum {min_battery_pct}% unless on charger). "
                    "Connect charger or pass --ignore-battery to override."
                )

        if check_posture and posture_seen and not ignore_posture:
            rstate = posture_seen["robot_state"]
            vel = abs(posture_seen["velocity_cms"])
            if rstate in UNSAFE_MOTION_STATES or vel > 5:
                state_label = UNSAFE_MOTION_STATES.get(rstate, f"STATE_{rstate}")
                raise SafetyValidationError(
                    f"Robot is currently active/standing ({state_label}, velocity={vel} cm/s). "
                    "Fold/sit the robot on flat ground before triggering OTA (or pass --ignore-posture)."
                )

        try:
            q_rsp = await robot.bus.request(
                "/cmd/ota/upgrade_control_req",
                "/cmd/ota/upgrade_control_rsp",
                timeout=min(timeout, 4.0),
                control_cmd=OTA_CMD_QUERY,
            )
            q_state = str(q_rsp.data.task.state or "").upper()
            if q_state in UNSAFE_ACTIVE_OTA_STATES:
                raise SafetyValidationError(
                    f"Robot already has an in-flight OTA task in critical state {q_state!r} "
                    f"(upgrade_id={q_rsp.data.task.upgrade_id!r}); refusing to interfere."
                )
        except SafetyValidationError:
            raise
        except Exception:
            q_state = ""

        result: dict[str, Any] = {
            "battery": pmu_seen or None,
            "posture": posture_seen or None,
            "previous_task_state": q_state,
        }

        if create_task:
            task_uuid = upgrade_id or str(uuid.uuid4())
            # md_upgrade's V2 create-task requires protocol_version == 2 AND a non-empty device_id
            # (the robot serial); without either it silently drops the request.
            resolved_device_id = device_id or os.environ.get("NEBI_DEVICE_ID", "")
            if not resolved_device_id:
                try:
                    resolved_device_id = await robot.system.get_serial_number(timeout=min(timeout, 3.0))
                except Exception:
                    resolved_device_id = ""
            if not resolved_device_id:
                raise RuntimeError(
                    "Could not resolve robot serial (device_id) via Zenoh /cmd/robot/sn_req; pass --device-id <SERIAL>."
                )
            c_rsp = await robot.bus.request(
                "/cmd/ota/upgrade_control_req",
                "/cmd/ota/upgrade_control_rsp",
                timeout=timeout,
                control_cmd=OTA_CMD_START,
                task={
                    "protocol_version": 2,
                    "upgrade_id": task_uuid,
                    "device_id": resolved_device_id,
                    "sha256": sha256_hex,
                    "size": file_size,
                    "version": version,
                    "original_filename": filename,
                },
            )
            err_code = str(c_rsp.data.task.error_code or "")
            ret_code = int(c_rsp.data.ret_code)
            token = str(c_rsp.data.task.upload_grant_token or "")
            resp_uid = str(c_rsp.data.task.upgrade_id or task_uuid)
            state = str(c_rsp.data.task.state or "")
            if err_code.upper() in ("NONE", "OK"):
                err_code = ""
            if ret_code != 0 or err_code:
                raise RuntimeError(
                    f"md_upgrade rejected OTA task creation (ret_code={ret_code}, error_code={err_code!r}, state={state!r})"
                )
            result.update(
                {
                    "upgrade_id": resp_uid,
                    "upload_grant_token": token,
                    "state": state,
                    "offset": int(c_rsp.data.task.offset),
                }
            )
        return result


def _upload_v1_multipart_once(
    file_path: Path,
    *,
    host: str,
    port: int,
    scheme: str,
    filename: str,
    md5_hex: str,
    file_size: int,
    upload_token: str | None,
    bearer_token: str | None,
    timeout: float,
    chunk_size: int,
    progress_cb: Callable[[int, int], None] | None,
) -> tuple[int, str]:
    boundary = f"----nebi-patch-{uuid.uuid4().hex}"
    part_md5 = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="md5"\r\n\r\n'
        f"{md5_hex}\r\n"
    ).encode()
    part_size = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="size"\r\n\r\n'
        f"{file_size}\r\n"
    ).encode()
    part_file_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    part_footer = f"\r\n--{boundary}--\r\n".encode()
    total_len = len(part_md5) + len(part_size) + len(part_file_header) + file_size + len(part_footer)

    conn = _make_connection(host, port, scheme, timeout)
    try:
        conn.putrequest("POST", "/upload")
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(total_len))
        for key, val in _auth_headers(bearer_token).items():
            conn.putheader(key, val)
        if upload_token:
            conn.putheader("Upload-Token", upload_token)
        conn.endheaders()

        send_err: BaseException | None = None
        try:
            conn.send(part_md5)
            conn.send(part_size)
            conn.send(part_file_header)

            sent = 0
            with file_path.open("rb") as fp:
                while chunk := fp.read(chunk_size):
                    conn.send(chunk)
                    sent += len(chunk)
                    if progress_cb is not None:
                        progress_cb(sent, file_size)

            conn.send(part_footer)
        except (BrokenPipeError, ConnectionResetError) as exc:
            # Server may reject early (e.g. HTTP 401/403); read HTTP response before raising.
            send_err = exc

        return _read_response_after_stream(conn, send_err)
    finally:
        conn.close()


def _upload_v2_tus_once(
    file_path: Path,
    *,
    host: str,
    port: int,
    scheme: str,
    upgrade_id: str,
    upload_token: str,
    bearer_token: str | None,
    file_size: int,
    timeout: float,
    chunk_size: int,
    progress_cb: Callable[[int, int], None] | None,
) -> tuple[int, str]:
    conn = _make_connection(host, port, scheme, timeout)
    try:
        conn.putrequest("PATCH", f"/v2/upgrades/{upgrade_id}/upload")
        conn.putheader("Content-Type", "application/offset+octet-stream")
        conn.putheader("Content-Length", str(file_size))
        conn.putheader("Tus-Resumable", "1.0.0")
        conn.putheader("Upload-Offset", "0")
        conn.putheader("Upload-Length", str(file_size))
        for key, val in _auth_headers(bearer_token).items():
            conn.putheader(key, val)
        conn.putheader("Upload-Token", upload_token)
        conn.endheaders()

        send_err: BaseException | None = None
        try:
            sent = 0
            with file_path.open("rb") as fp:
                while chunk := fp.read(chunk_size):
                    conn.send(chunk)
                    sent += len(chunk)
                    if progress_cb is not None:
                        progress_cb(sent, file_size)
        except (BrokenPipeError, ConnectionResetError) as exc:
            send_err = exc

        return _read_response_after_stream(conn, send_err)
    finally:
        conn.close()


def wait_v2_task_ready(
    *,
    host: str,
    port: int,
    scheme: str,
    upgrade_id: str,
    upload_token: str,
    bearer_token: str | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Poll `GET /v2/upgrades/{id}` until `md_upgrade` finishes SHA-256 verification (`READY`)."""
    deadline = time.monotonic() + timeout
    last_payload: dict[str, Any] = {}
    headers = _auth_headers(bearer_token, {"Upload-Token": upload_token})
    while time.monotonic() < deadline:
        conn = _make_connection(host, port, scheme, min(10.0, timeout))
        try:
            conn.request("GET", f"/v2/upgrades/{upgrade_id}", headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                raise RuntimeError(f"GET /v2/upgrades/{upgrade_id} failed (HTTP {resp.status}): {raw}")
            last_payload = json.loads(raw) if raw.strip() else {}
        finally:
            conn.close()

        state = str(last_payload.get("state", "")).upper()
        if state in ("READY", "INSTALLING", "SUCCEEDED"):
            return last_payload
        if state in ("FAILED", "CANCELLED", "EXPIRED", "RECOVERY_REQUIRED"):
            raise RuntimeError(f"On-device package verification failed in state {state!r}: {last_payload}")
        time.sleep(1.0)
    raise TimeoutError(
        f"Timed out waiting for md_upgrade task {upgrade_id} to reach READY; last snapshot: {last_payload}"
    )


def trigger_v2_http_install(
    *,
    host: str,
    port: int,
    scheme: str,
    upgrade_id: str,
    upload_token: str,
    bearer_token: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Trigger installation of a `READY` V2 upgrade session via `POST /v2/upgrades/{id}/install`."""
    conn = _make_connection(host, port, scheme, timeout)
    try:
        conn.request(
            "POST",
            f"/v2/upgrades/{upgrade_id}/install",
            headers=_auth_headers(bearer_token, {"Upload-Token": upload_token, "Content-Length": "0"}),
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400:
            raise RuntimeError(f"POST /v2/upgrades/{upgrade_id}/install failed (HTTP {resp.status}): {raw}")
        try:
            parsed: dict[str, Any] = json.loads(raw) if raw.strip() else {"status": resp.status}
            return parsed
        except json.JSONDecodeError:
            return {"status": resp.status, "body": raw}
    finally:
        conn.close()


def _mint_v2_session(
    *,
    file_path: Path,
    host: str,
    ucp_port: int,
    port: int,
    filename: str,
    file_size: int,
    sha256_hex: str,
    upgrade_id: str | None,
    device_id: str | None,
    min_battery_pct: int,
    ignore_battery: bool,
) -> tuple[str, str]:
    task_info = asyncio.run(
        preflight_and_create_zenoh_task(
            host=host,
            ucp_port=ucp_port,
            ota_port=port,
            create_task=True,
            filename=filename,
            file_size=file_size,
            sha256_hex=sha256_hex,
            version=_extract_pkg_version(file_path),
            upgrade_id=upgrade_id,
            device_id=device_id,
            min_battery_pct=min_battery_pct,
            ignore_battery=ignore_battery,
        )
    )
    return str(task_info["upgrade_id"]), str(task_info["upload_grant_token"])


def upload_firmware(
    zip_path: str | Path,
    *,
    host: str = "192.168.7.1",
    port: int = DEFAULT_OTA_PORT,
    ucp_port: int = DEFAULT_UCP_PORT,
    scheme: str = "auto",
    mode: str = "auto",
    remote_filename: str | None = None,
    upload_token: str | None = None,
    upgrade_id: str | None = None,
    device_id: str | None = None,
    bearer_token: str | None = None,
    verify_before_upload: bool = True,
    min_battery_pct: int = MIN_SAFE_BATTERY_PCT,
    ignore_battery: bool = False,
    max_retries: int = 2,
    timeout: float = 300.0,
    chunk_size: int = 1024 * 1024,
    progress_cb: Callable[[int, int], None] | None = None,
) -> UploadResult:
    """Validate and stream-upload a firmware ZIP to `md_upgrade` (`:8085`) with V1/V2 support & retries."""
    file_path = Path(zip_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Firmware ZIP not found: {file_path}")

    filename = remote_filename or file_path.name
    if not filename.startswith("V"):
        raise ValueError(
            "md_upgrade requires the uploaded filename to start with 'V' "
            f"(e.g. V26.10.28.05_lz110.zip), got {filename!r}"
        )
    if mode not in ("auto", "v1", "v2"):
        raise ValueError(f"Unsupported upload mode {mode!r}; expected 'auto', 'v1', or 'v2'")

    warnings: list[str] = []
    if verify_before_upload:
        report = verify_package(file_path, logical_filename=filename)
        warnings.extend(report.warnings)

    md5_hex, file_size = md5_file(file_path)
    sha256_hex = sha256_file(file_path)
    resolved = detect_scheme(host, port, timeout=min(timeout, 5.0)) if scheme == "auto" else scheme

    t0 = time.monotonic()
    active_mode = mode
    active_token = upload_token
    active_uid = upgrade_id

    if active_mode == "v2" and (not active_token or not active_uid):
        active_uid, active_token = _mint_v2_session(
            file_path=file_path,
            host=host,
            ucp_port=ucp_port,
            port=port,
            filename=filename,
            file_size=file_size,
            sha256_hex=sha256_hex,
            upgrade_id=active_uid,
            device_id=device_id,
            min_battery_pct=min_battery_pct,
            ignore_battery=ignore_battery,
        )

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            if active_mode in ("auto", "v1"):
                status, body = _upload_v1_multipart_once(
                    file_path,
                    host=host,
                    port=port,
                    scheme=resolved,
                    filename=filename,
                    md5_hex=md5_hex,
                    file_size=file_size,
                    upload_token=active_token,
                    bearer_token=bearer_token,
                    timeout=timeout,
                    chunk_size=chunk_size,
                    progress_cb=progress_cb,
                )
                # If `auto` hits 401/403 without an Upload-Token, mint a V2 session over Zenoh and switch to V2.
                if status in (401, 403) and active_mode == "auto" and not active_token:
                    active_uid, active_token = _mint_v2_session(
                        file_path=file_path,
                        host=host,
                        ucp_port=ucp_port,
                        port=port,
                        filename=filename,
                        file_size=file_size,
                        sha256_hex=sha256_hex,
                        upgrade_id=active_uid,
                        device_id=device_id,
                        min_battery_pct=min_battery_pct,
                        ignore_battery=ignore_battery,
                    )
                    active_mode = "v2"
                    status, body = _upload_v2_tus_once(
                        file_path,
                        host=host,
                        port=port,
                        scheme=resolved,
                        upgrade_id=active_uid,
                        upload_token=active_token,
                        bearer_token=bearer_token,
                        file_size=file_size,
                        timeout=timeout,
                        chunk_size=chunk_size,
                        progress_cb=progress_cb,
                    )
                else:
                    active_mode = "v1"
            else:
                if active_uid is None or active_token is None:
                    raise RuntimeError("V2 upload requires both upgrade_id and upload_token")
                status, body = _upload_v2_tus_once(
                    file_path,
                    host=host,
                    port=port,
                    scheme=resolved,
                    upgrade_id=active_uid,
                    upload_token=active_token,
                    bearer_token=bearer_token,
                    file_size=file_size,
                    timeout=timeout,
                    chunk_size=chunk_size,
                    progress_cb=progress_cb,
                )

            if status >= 500 and attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            if status >= 400:
                hint = ""
                if status in (401, 403) and not resolve_bearer_token(bearer_token):
                    hint = " (configure --bearer-token or $NEBI_HTTP_BEARER_TOKEN / ~/.config/pynebi/media_token)"
                raise RuntimeError(f"Upload failed with HTTP {status}{hint}: {body}")

            remote_verified = False
            if active_mode == "v2" and active_uid and active_token:
                wait_v2_task_ready(
                    host=host,
                    port=port,
                    scheme=resolved,
                    upgrade_id=active_uid,
                    upload_token=active_token,
                    bearer_token=bearer_token,
                    timeout=min(timeout, 120.0),
                )
                remote_verified = True
            else:
                found, size_err = _verify_file_in_remote_list(
                    host, port, resolved, filename, file_size, bearer_token=bearer_token
                )
                if size_err:
                    raise SafetyValidationError(size_err)
                remote_verified = found

            return UploadResult(
                mode=active_mode,
                scheme=resolved,
                host=host,
                port=port,
                filename=filename,
                size_bytes=file_size,
                md5=md5_hex,
                sha256=sha256_hex,
                status_code=status,
                response_body=body,
                elapsed_sec=time.monotonic() - t0,
                upgrade_id=active_uid,
                upload_token=active_token,
                remote_verified=remote_verified,
                warnings=warnings,
            )
        except SafetyValidationError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise

    assert last_exc is not None
    raise last_exc


async def trigger_zenoh_ota(
    host: str = "192.168.7.1",
    ucp_port: int = DEFAULT_UCP_PORT,
    ota_port: int = DEFAULT_OTA_PORT,
    *,
    scheme: str = "auto",
    upgrade_id: str | None = None,
    upload_token: str | None = None,
    bearer_token: str | None = None,
    min_battery_pct: int = MIN_SAFE_BATTERY_PCT,
    ignore_battery: bool = False,
    ignore_posture: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Run battery/posture safety gates and trigger OTA installation (`Install` for V2, `Start` for V1)."""
    safety = await preflight_and_create_zenoh_task(
        host=host,
        ucp_port=ucp_port,
        ota_port=ota_port,
        create_task=False,
        min_battery_pct=min_battery_pct,
        ignore_battery=ignore_battery,
        check_posture=True,
        ignore_posture=ignore_posture,
        timeout=timeout,
    )

    if upgrade_id and upload_token:
        resolved = detect_scheme(host, ota_port, timeout=5.0) if scheme == "auto" else scheme
        http_res = trigger_v2_http_install(
            host=host,
            port=ota_port,
            scheme=resolved,
            upgrade_id=upgrade_id,
            upload_token=upload_token,
            bearer_token=bearer_token,
            timeout=timeout,
        )
        return {"method": "v2_http_install", "upgrade_id": upgrade_id, "safety": safety, "response": http_res}

    client_cls = _import_nebi_client()
    cmd_code = OTA_CMD_INSTALL if upgrade_id else OTA_CMD_START
    req_kwargs: dict[str, Any] = {"control_cmd": cmd_code}
    if upgrade_id:
        req_kwargs["task"] = {"protocol_version": 1, "upgrade_id": upgrade_id}

    async with client_cls(host=host, ucp_port=ucp_port, ota_port=ota_port) as robot:
        rsp = await robot.bus.request(
            "/cmd/ota/upgrade_control_req",
            "/cmd/ota/upgrade_control_rsp",
            timeout=timeout,
            **req_kwargs,
        )
        return {
            "method": "zenoh_control_req",
            "control_cmd": cmd_code,
            "ret_code": int(rsp.data.ret_code),
            "stage": int(rsp.data.current_upgrade_stage),
            "task_state": str(rsp.data.task.state or ""),
            "error_code": str(rsp.data.task.error_code or ""),
            "safety": safety,
        }


def trigger_zenoh_ota_sync(**kwargs: Any) -> dict[str, Any]:
    """Synchronous wrapper around `trigger_zenoh_ota()`."""
    return asyncio.run(trigger_zenoh_ota(**kwargs))
