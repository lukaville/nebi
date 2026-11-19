"""Command-line interface for `nebi-patch`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

from nebi_patch.packer import PART_FILENAME_RE, parse_mass_cfg
from nebi_patch.patcher import patch_firmware
from nebi_patch.uploader import (
    DEFAULT_OTA_PORT,
    DEFAULT_UCP_PORT,
    MIN_SAFE_BATTERY_PCT,
    check_robot_ota,
    trigger_zenoh_ota_sync,
    upload_firmware,
)
from nebi_patch.validator import SafetyValidationError, verify_package

_BASE_FIRMWARE_ENV = "NEBI_BASE_FIRMWARE"


def _resolve_base_zip(arg_val: str | None) -> Path:
    raw = arg_val or os.environ.get(_BASE_FIRMWARE_ENV, "").strip()
    if not raw:
        raise ValueError(f"Base firmware ZIP is required: pass --base-zip <path> or set ${_BASE_FIRMWARE_ENV}.")
    return Path(raw)


def _print_progress() -> Callable[[int, int], None]:
    last_print = 0.0

    def cb(sent: int, total: int) -> None:
        nonlocal last_print
        now = time.monotonic()
        if sent < total and (now - last_print) < 0.25:
            return
        last_print = now
        pct = (sent / total * 100.0) if total else 100.0
        mb_sent = sent / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        sys.stderr.write(f"\rUploading: {mb_sent:7.1f} / {mb_total:7.1f} MiB ({pct:5.1f}%)")
        if sent >= total:
            sys.stderr.write("\n")
        sys.stderr.flush()

    return cb


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)


def _cmd_inspect(args: argparse.Namespace) -> int:
    zip_path = _resolve_base_zip(args.base_zip)
    if not zip_path.is_file():
        print(f"Error: file not found: {zip_path}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(zip_path, "r") as zf:
        cfg_names = [n for n in zf.namelist() if n.endswith(".mass.cfg.plain.unsign")]
        if not cfg_names:
            print("No .mass.cfg.plain.unsign found in archive", file=sys.stderr)
            return 1
        cfg = parse_mass_cfg(zf.read(cfg_names[0]))
        print(f"Package: {zip_path}")
        print(f"  version:    {cfg['package'].get('version')}")
        print(f"  package_id: {cfg['package'].get('package_id')}")
        print(f"  created_at: {cfg['package'].get('created_at')}")
        print("Modules:")
        for fw in cfg.get("firmwares", []):
            print(f"  {fw['part']:4s}  {fw['device_type']:8s}  {fw['size']:10d} B  {fw['checksum']}")
        x5 = [n for n in zf.namelist() if "_0300_" in n]
        if x5:
            match = PART_FILENAME_RE.match(x5[0])
            print(f"X5 (0300): {x5[0]} (version={match.group('version') if match else '?'})")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    base_ref = args.base_zip if args.base_zip and Path(args.base_zip).is_file() else None
    report = verify_package(
        args.firmware_zip,
        base_zip_path=base_ref,
        allow_unsafe_x5_override=args.allow_unsafe_x5_override,
    )
    print(f"Verified package OK: {report.zip_path}")
    print(f"  Package version:        {report.package_version}")
    print(f"  Manifest:               {report.manifest_name}")
    print(f"  Verified modules:       {report.verified_modules}")
    if report.x5_entry_name:
        print(f"  X5 (0300) archive:      {report.x5_entry_name}")
        print(f"  Verified X5 partitions: {', '.join(report.verified_x5_partitions)}")
        if report.ota_process_mode is not None:
            print(f"  ota_process Unix mode:  {oct(report.ota_process_mode)}")
    _print_warnings(report.warnings)
    return 0


def _cmd_patch(args: argparse.Namespace) -> int:
    res = patch_firmware(
        base_zip=_resolve_base_zip(args.base_zip),
        patch_dir=args.patch_dir,
        output_zip=args.output,
        bump_ver=not args.no_bump_version,
        package_version=args.package_version,
        x5_version=args.x5_version,
        work_dir=args.work_dir,
        verify_fsck=not args.no_fsck,
        allow_unsafe_x5_override=args.allow_unsafe_x5_override,
        allow_module_override=args.allow_module_override,
    )
    print(f"Patched firmware written & verified: {res.output_zip}")
    print(f"  Package version: {res.package_version}")
    print(f"  X5 version:      {res.x5_version}")
    print(f"  Size:            {res.size_bytes} bytes")
    print(f"  SHA-256:         {res.sha256}")
    if res.mondo_changes:
        print(f"  mondo.img:       {res.mondo_changes}")
    if res.hbre_changes:
        print(f"  hbre.img:        {res.hbre_changes}")
    if res.x5_overrides:
        print(f"  x5 overrides:    {res.x5_overrides}")
    if res.module_overrides:
        print(f"  module overrides:{res.module_overrides}")
    _print_warnings(res.warnings)
    return 0


def _run_upload_and_optional_trigger(zip_path: str | Path, args: argparse.Namespace) -> int:
    up = upload_firmware(
        zip_path,
        host=args.host,
        port=args.port,
        ucp_port=args.ucp_port,
        scheme=args.scheme,
        mode=args.mode,
        upload_token=args.upload_token,
        upgrade_id=args.upgrade_id,
        device_id=args.device_id,
        bearer_token=args.bearer_token,
        verify_before_upload=not args.skip_verify,
        min_battery_pct=args.min_battery,
        ignore_battery=args.ignore_battery,
        progress_cb=_print_progress(),
    )
    print(
        f"Upload complete ({up.elapsed_sec:.1f}s) [mode={up.mode}]: "
        f"{up.scheme}://{up.host}:{up.port} -> HTTP {up.status_code}"
    )
    print(f"  Filename:        {up.filename}")
    print(f"  MD5:             {up.md5}")
    print(f"  SHA-256:         {up.sha256}")
    print(f"  Remote verified: {up.remote_verified}")
    if up.upgrade_id:
        print(f"  Upgrade ID:      {up.upgrade_id}")
    if up.response_body.strip():
        print(f"  Response:        {up.response_body.strip()}")
    _print_warnings(up.warnings)

    if args.trigger:
        trig = trigger_zenoh_ota_sync(
            host=args.host,
            ucp_port=args.ucp_port,
            ota_port=args.port,
            scheme=up.scheme,
            upgrade_id=up.upgrade_id,
            upload_token=up.upload_token,
            bearer_token=args.bearer_token,
            min_battery_pct=args.min_battery,
            ignore_battery=args.ignore_battery,
            ignore_posture=args.ignore_posture,
        )
        print(f"OTA install trigger response: {json.dumps(trig, indent=2)}")
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    return _run_upload_and_optional_trigger(args.firmware_zip, args)


def _cmd_patch_and_upload(args: argparse.Namespace) -> int:
    res = patch_firmware(
        base_zip=_resolve_base_zip(args.base_zip),
        patch_dir=args.patch_dir,
        output_zip=args.output,
        bump_ver=not args.no_bump_version,
        package_version=args.package_version,
        x5_version=args.x5_version,
        work_dir=args.work_dir,
        verify_fsck=not args.no_fsck,
        allow_unsafe_x5_override=args.allow_unsafe_x5_override,
        allow_module_override=args.allow_module_override,
    )
    print(f"Patched & verified firmware: {res.output_zip} (ver={res.package_version}, x5={res.x5_version})")
    _print_warnings(res.warnings)
    if args.dry_run:
        print("Dry-run enabled; skipping upload.")
        return 0
    return _run_upload_and_optional_trigger(res.output_zip, args)


def _cmd_status(args: argparse.Namespace) -> int:
    info = check_robot_ota(
        host=args.host,
        port=args.port,
        scheme=args.scheme,
        bearer_token=args.bearer_token,
    )
    print(json.dumps(info, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the `nebi-patch` argument parser."""
    parser = argparse.ArgumentParser(
        prog="nebi-patch",
        description="Safely patch Beni (LZ110/LZ100) firmware packages, verify integrity, and upload to the robot.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="Inspect a firmware ZIP manifest and modules")
    p_inspect.add_argument(
        "--base-zip",
        default=None,
        help=f"Path to firmware ZIP (or set ${_BASE_FIRMWARE_ENV})",
    )
    p_inspect.set_defaults(func=_cmd_inspect)

    p_verify = sub.add_parser("verify", help="Verify all SHA-256/MD5 checksums, permissions, and safety invariants")
    p_verify.add_argument("firmware_zip", help="Path to OTA firmware ZIP to verify")
    p_verify.add_argument(
        "--base-zip",
        default=None,
        help="Optional base firmware ZIP to confirm bootloader/AVB partitions were not altered",
    )
    p_verify.add_argument(
        "--allow-unsafe-x5-override",
        action="store_true",
        help="Allow modified bootloader/AVB partitions relative to --base-zip",
    )
    p_verify.set_defaults(func=_cmd_verify)

    def add_patch_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("patch_dir", help="Directory containing firmware patches (mondo/, mondo.delete, patch.toml, ...)")
        p.add_argument(
            "--base-zip",
            default=None,
            help=f"Base firmware ZIP path (or set ${_BASE_FIRMWARE_ENV})",
        )
        p.add_argument("-o", "--output", default=None, help="Output patched ZIP path (must start with 'V')")
        p.add_argument("--no-bump-version", action="store_true", help="Keep original package & X5 version numbers")
        p.add_argument("--package-version", default=None, help="Explicit outer package version (e.g. 26.10.28.05)")
        p.add_argument("--x5-version", default=None, help="Explicit X5 (0300) version (e.g. 26.10.27.46)")
        p.add_argument("--work-dir", default=None, help="Temporary working directory (needs ~2.5 GiB free space)")
        p.add_argument("--no-fsck", action="store_true", help="Skip e2fsck -fn verification on ext4 images")
        p.add_argument(
            "--allow-unsafe-x5-override",
            action="store_true",
            help="DANGEROUS: allow overriding bootloader/AVB files (miniboot/uboot/vbmeta/boot/system/gpt.conf/ota_process)",
        )
        p.add_argument(
            "--allow-module-override",
            action="store_true",
            help="Allow overriding single-slot peripheral MCU/A350 firmwares in modules/",
        )

    def add_upload_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default="192.168.7.1", help="Robot IP (192.168.7.1 SoftAP or 192.168.123.1 USB NCM)")
        p.add_argument(
            "--port",
            type=int,
            default=DEFAULT_OTA_PORT,
            help=f"md_upgrade HTTP/HTTPS port (default: {DEFAULT_OTA_PORT})",
        )
        p.add_argument(
            "--ucp-port",
            type=int,
            default=DEFAULT_UCP_PORT,
            help=f"Robot UCP/Zenoh port for V2 token & pre-flight checks (default: {DEFAULT_UCP_PORT})",
        )
        p.add_argument("--scheme", choices=["auto", "http", "https"], default="auto", help="Transport scheme")
        p.add_argument(
            "--mode",
            choices=["auto", "v2", "v1"],
            default="auto",
            help="Upload protocol: 'v2' (Zenoh Upload-Token + /v2/upgrades/{id}/upload), 'v1' (POST /upload), or 'auto'",
        )
        p.add_argument("--upload-token", default=None, help="Explicit Upload-Token header if pre-created")
        p.add_argument("--upgrade-id", default=None, help="Explicit V2 upgrade UUID if pre-created")
        p.add_argument("--device-id", default=None, help="Explicit robot serial number (fallback if Zenoh sn_req times out)")
        p.add_argument(
            "--bearer-token",
            default=None,
            help="HTTP Bearer token for :8085 (default: $NEBI_HTTP_BEARER_TOKEN, $PYNEBI_MEDIA_TOKEN, or ~/.config/pynebi/media_token)",
        )
        p.add_argument("--skip-verify", action="store_true", help="Skip local full-package checksum verification before upload")
        p.add_argument(
            "--min-battery",
            type=int,
            default=MIN_SAFE_BATTERY_PCT,
            help=f"Minimum battery SoC %% required when not charging (default: {MIN_SAFE_BATTERY_PCT})",
        )
        p.add_argument("--ignore-battery", action="store_true", help="Bypass minimum battery SoC gate")
        p.add_argument("--ignore-posture", action="store_true", help="Bypass folded/sitting posture check on --trigger")
        p.add_argument("--trigger", action="store_true", help="Trigger OTA installation after upload & verification")

    p_patch = sub.add_parser("patch", help="Apply a patch directory to produce a verified firmware ZIP")
    add_patch_args(p_patch)
    p_patch.set_defaults(func=_cmd_patch)

    p_upload = sub.add_parser("upload", help="Verify and upload an existing firmware ZIP to the robot")
    p_upload.add_argument("firmware_zip", help="Patched firmware ZIP (filename must start with 'V')")
    add_upload_args(p_upload)
    p_upload.set_defaults(func=_cmd_upload)

    p_apply = sub.add_parser("apply", help="Patch, verify, and upload firmware to the robot")
    add_patch_args(p_apply)
    add_upload_args(p_apply)
    p_apply.add_argument("--dry-run", action="store_true", help="Patch and verify locally, but do not upload")
    p_apply.set_defaults(func=_cmd_patch_and_upload)

    p_status = sub.add_parser("status", help="Check robot md_upgrade /health and /list endpoints")
    p_status.add_argument("--host", default="192.168.7.1", help="Robot IP")
    p_status.add_argument("--port", type=int, default=DEFAULT_OTA_PORT, help=f"md_upgrade port (default: {DEFAULT_OTA_PORT})")
    p_status.add_argument("--scheme", choices=["auto", "http", "https"], default="auto")
    p_status.add_argument("--bearer-token", default=None, help="HTTP Bearer token for :8085")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `nebi-patch`."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SafetyValidationError as exc:
        print(f"SAFETY GATE BLOCKED OPERATION:\n  {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
