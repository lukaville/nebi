"""Core hardened firmware patcher for Beni (`lz110`/`lz100`) firmware packages."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nebi_patch._toml import loads as toml_loads
from nebi_patch.ext4 import Ext4Image
from nebi_patch.packer import (
    PART_FILENAME_RE,
    STREAM_CHUNK_SIZE,
    bump_version,
    copy_zip_entry_streaming,
    parse_mass_cfg,
    render_mass_cfg,
    repack_x5_zip_streaming,
    sha256_file,
    update_x5_data_json,
    write_bytes_to_zip_unix,
    write_file_to_zip_streaming,
)
from nebi_patch.validator import (
    SafetyValidationError,
    validate_patch_inputs,
    validate_patched_mondo_image,
    verify_aarch64_elf_header,
    verify_package,
)


@dataclass
class PatchResult:
    """Result summary returned by `patch_firmware()`."""

    output_zip: Path
    package_version: str
    x5_version: str
    sha256: str
    size_bytes: int
    mondo_changes: dict[str, int] = field(default_factory=dict)
    hbre_changes: dict[str, int] = field(default_factory=dict)
    x5_overrides: list[str] = field(default_factory=list)
    module_overrides: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _load_delete_list(path: Path) -> list[str]:
    if not path.is_file():
        return []
    entries: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def _load_patch_config(patch_dir: Path) -> dict[str, Any]:
    cfg_path = patch_dir / "patch.toml"
    if not cfg_path.is_file():
        return {}
    return toml_loads(cfg_path.read_text(encoding="utf-8"))


def _parse_mode_overrides(patch_cfg: dict[str, Any]) -> dict[str, int]:
    modes: dict[str, int] = {}
    for path_key, mode_val in patch_cfg.get("modes", {}).items():
        if isinstance(mode_val, int):
            modes[path_key] = mode_val
        elif isinstance(mode_val, str):
            modes[path_key] = int(mode_val, 8)
        else:
            raise ValueError(f"Invalid mode for {path_key!r}: {mode_val!r}")
    return modes


def _has_files(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(p.is_file() and p.name != ".DS_Store" for p in directory.rglob("*"))


def _find_module_override(modules_dir: Path, part_code: str, orig_name: str) -> Path | None:
    if not modules_dir.is_dir():
        return None
    exact = modules_dir / orig_name
    if exact.is_file():
        return exact
    for candidate in sorted(modules_dir.iterdir()):
        if not candidate.is_file() or candidate.name == ".DS_Store":
            continue
        stem = candidate.stem.upper()
        if stem == part_code.upper() or candidate.name.lower().startswith(f"lz110_{part_code.lower()}_"):
            return candidate
    return None


def _extract_single_member(zf: zipfile.ZipFile, member_name: str, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member_name, "r") as rfp, dest_path.open("wb") as wfp:
        shutil.copyfileobj(rfp, wfp, length=STREAM_CHUNK_SIZE)


def _patch_mondo_image(
    x5_zf: zipfile.ZipFile,
    x5_members: set[str],
    x5_work: Path,
    *,
    overlay_dir: Path,
    delete_paths: list[str],
    mode_overrides: dict[str, int],
    verify_fsck: bool,
    min_ext4_free_bytes: int | None,
) -> tuple[Path, dict[str, int]]:
    if "mondo.img" not in x5_members:
        raise SafetyValidationError("0300 archive is missing mondo.img")
    mondo_path = x5_work / "mondo.img"
    _extract_single_member(x5_zf, "mondo.img", mondo_path)
    mondo_img = Ext4Image(mondo_path)
    if verify_fsck:
        mondo_img.fsck()
    had_system_daemons = mondo_img.path_exists("/bin/md_upgrade")
    changes = mondo_img.apply_overlay_and_deletes(
        overlay_dir=overlay_dir if _has_files(overlay_dir) else None,
        delete_paths=delete_paths,
        mode_overrides=mode_overrides,
        min_free_bytes=min_ext4_free_bytes,
        verify_readback=True,
    )
    if verify_fsck:
        mondo_img.fsck()
    validate_patched_mondo_image(mondo_img, had_system_daemons)
    return mondo_path, changes


def _patch_hbre_image(
    x5_zf: zipfile.ZipFile,
    x5_members: set[str],
    x5_work: Path,
    *,
    overlay_dir: Path,
    delete_paths: list[str],
    mode_overrides: dict[str, int],
    verify_fsck: bool,
    min_ext4_free_bytes: int | None,
) -> tuple[Path, dict[str, int]]:
    if "hbre.img" not in x5_members:
        raise SafetyValidationError("0300 archive is missing hbre.img")
    hbre_path = x5_work / "hbre.img"
    _extract_single_member(x5_zf, "hbre.img", hbre_path)
    hbre_img = Ext4Image(hbre_path)
    if verify_fsck:
        hbre_img.fsck()
    had_ota_tool = hbre_img.path_exists("/bin/ota_tool")
    changes = hbre_img.apply_overlay_and_deletes(
        overlay_dir=overlay_dir if _has_files(overlay_dir) else None,
        delete_paths=delete_paths,
        mode_overrides=mode_overrides,
        min_free_bytes=min_ext4_free_bytes,
        verify_readback=True,
    )
    if verify_fsck:
        hbre_img.fsck()
    if had_ota_tool:
        st = hbre_img.stat_path("/bin/ota_tool")
        if st is None or (st.mode & 0o111) == 0:
            raise SafetyValidationError("Post-patch check failed: /bin/ota_tool missing or non-executable in hbre.img!")
        verify_aarch64_elf_header(hbre_img.read_file_prefix("/bin/ota_tool", 64), "hbre.img:/bin/ota_tool")
    return hbre_path, changes


def _repack_x5_archive(
    src_zip: zipfile.ZipFile,
    x5_orig_name: str,
    new_x5_name: str,
    tmp_root: Path,
    *,
    need_mondo: bool,
    need_hbre: bool,
    need_x5_override: bool,
    mondo_overlay: Path,
    mondo_deletes: list[str],
    hbre_overlay: Path,
    hbre_deletes: list[str],
    x5_override_dir: Path,
    mode_overrides: dict[str, int],
    verify_fsck: bool,
    min_ext4_free_bytes: int | None,
) -> tuple[Path, dict[str, int], dict[str, int], list[str]]:
    x5_zip_local = tmp_root / "x5_orig.zip"
    _extract_single_member(src_zip, x5_orig_name, x5_zip_local)

    x5_work = tmp_root / "x5_work"
    x5_work.mkdir(parents=True, exist_ok=True)
    replacement_files: dict[str, Path] = {}
    mondo_changes: dict[str, int] = {}
    hbre_changes: dict[str, int] = {}
    x5_overrides: list[str] = []

    with zipfile.ZipFile(x5_zip_local, "r") as x5_zf:
        x5_members = set(x5_zf.namelist())
        if "data.json" not in x5_members:
            raise SafetyValidationError(f"0300 archive {x5_orig_name} is missing data.json")
        data_json_path = x5_work / "data.json"
        _extract_single_member(x5_zf, "data.json", data_json_path)

        if need_mondo:
            mondo_path, mondo_changes = _patch_mondo_image(
                x5_zf,
                x5_members,
                x5_work,
                overlay_dir=mondo_overlay,
                delete_paths=mondo_deletes,
                mode_overrides=mode_overrides,
                verify_fsck=verify_fsck,
                min_ext4_free_bytes=min_ext4_free_bytes,
            )
            replacement_files["mondo.img"] = mondo_path

        if need_hbre:
            hbre_path, hbre_changes = _patch_hbre_image(
                x5_zf,
                x5_members,
                x5_work,
                overlay_dir=hbre_overlay,
                delete_paths=hbre_deletes,
                mode_overrides=mode_overrides,
                verify_fsck=verify_fsck,
                min_ext4_free_bytes=min_ext4_free_bytes,
            )
            replacement_files["hbre.img"] = hbre_path

    if need_x5_override:
        for src_file in sorted(x5_override_dir.iterdir()):
            if src_file.is_file() and src_file.name != ".DS_Store":
                replacement_files[src_file.name] = src_file
                x5_overrides.append(src_file.name)

    if replacement_files:
        update_x5_data_json(data_json_path, replacement_files)
        replacement_files["data.json"] = data_json_path

    patched_x5_file = tmp_root / new_x5_name
    repack_x5_zip_streaming(x5_zip_local, patched_x5_file, replacement_files, compresslevel=1)
    x5_zip_local.unlink(missing_ok=True)
    shutil.rmtree(x5_work, ignore_errors=True)
    return patched_x5_file, mondo_changes, hbre_changes, x5_overrides


def _write_outer_zip(
    src_zip: zipfile.ZipFile,
    staging_zip: Path,
    *,
    entries: list[str],
    cfg_name: str,
    cfg_match: re.Match[str],
    mass_cfg: dict[str, Any],
    target_pkg_ver: str,
    x5_orig_name: str,
    new_x5_name: str,
    patched_x5_file: Path | None,
    modules_override_dir: Path,
) -> list[str]:
    module_overrides: list[str] = []
    fw_by_part: dict[str, dict[str, Any]] = {str(fw["part"]).upper(): fw for fw in mass_cfg.get("firmwares", [])}

    with zipfile.ZipFile(
        staging_zip,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
        allowZip64=True,
    ) as dst_zip:
        for entry_name in entries:
            if entry_name == cfg_name:
                continue

            if entry_name == x5_orig_name and patched_x5_file is not None:
                size, digest = write_file_to_zip_streaming(
                    patched_x5_file, dst_zip, new_x5_name, compress_type=zipfile.ZIP_STORED
                )
                fw_by_part["0300"]["size"] = size
                fw_by_part["0300"]["checksum"] = f"sha256:{digest}"
                continue

            match = PART_FILENAME_RE.match(entry_name)
            if match:
                part_code = match.group("part").upper()
                override_file = _find_module_override(modules_override_dir, part_code, entry_name)
                if override_file is not None:
                    size, digest = write_file_to_zip_streaming(
                        override_file, dst_zip, entry_name, compress_type=zipfile.ZIP_DEFLATED
                    )
                    if part_code in fw_by_part:
                        fw_by_part[part_code]["size"] = size
                        fw_by_part[part_code]["checksum"] = f"sha256:{digest}"
                    module_overrides.append(f"{part_code}:{override_file.name}")
                    continue

            copy_zip_entry_streaming(src_zip, entry_name, dst_zip, compress_type=zipfile.ZIP_STORED)

        now_utc = datetime.now(timezone.utc)
        date_str = cfg_match.group("date")
        mass_cfg["package"]["version"] = target_pkg_ver
        mass_cfg["package"]["package_id"] = f"upgrade_{date_str}_{target_pkg_ver.replace('.', '')}"
        mass_cfg["package"]["created_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        new_cfg_name = (
            f"{cfg_match.group('prefix')}_{cfg_match.group('part')}_V{target_pkg_ver}_{date_str}.{cfg_match.group('suffix')}"
        )
        write_bytes_to_zip_unix(
            dst_zip,
            new_cfg_name,
            render_mass_cfg(mass_cfg).encode("utf-8"),
            compress_type=zipfile.ZIP_DEFLATED,
        )

    return module_overrides


def patch_firmware(
    base_zip: str | Path,
    patch_dir: str | Path,
    output_zip: str | Path | None = None,
    *,
    bump_ver: bool = True,
    package_version: str | None = None,
    x5_version: str | None = None,
    work_dir: str | Path | None = None,
    verify_fsck: bool = True,
    allow_unsafe_x5_override: bool = False,
    allow_module_override: bool = False,
    min_ext4_free_bytes: int | None = None,
) -> PatchResult:
    """Apply `patch_dir` to `base_zip` with full anti-brick validation and produce an OTA ZIP."""
    base_zip_path = Path(base_zip)
    patch_path = Path(patch_dir)
    if not base_zip_path.is_file():
        raise FileNotFoundError(f"Base firmware ZIP not found: {base_zip_path}")
    if not patch_path.is_dir():
        raise NotADirectoryError(f"Patch directory not found: {patch_path}")

    patch_cfg = _load_patch_config(patch_path)
    pkg_cfg = patch_cfg.get("package", {})
    mode_overrides = _parse_mode_overrides(patch_cfg)

    mondo_overlay = patch_path / "mondo"
    mondo_deletes = _load_delete_list(patch_path / "mondo.delete")
    hbre_overlay = patch_path / "hbre"
    hbre_deletes = _load_delete_list(patch_path / "hbre.delete")
    x5_override_dir = patch_path / "x5"
    modules_override_dir = patch_path / "modules"

    # Gate 1: Validate all patch inputs BEFORE extracting or modifying any image.
    warnings = validate_patch_inputs(
        patch_path,
        mondo_deletes=mondo_deletes,
        hbre_deletes=hbre_deletes,
        allow_unsafe_x5_override=allow_unsafe_x5_override,
        allow_module_override=allow_module_override,
    )

    need_mondo = _has_files(mondo_overlay) or bool(mondo_deletes)
    need_hbre = _has_files(hbre_overlay) or bool(hbre_deletes)
    need_x5_override = _has_files(x5_override_dir)
    need_x5_repack = need_mondo or need_hbre or need_x5_override

    base_size = base_zip_path.stat().st_size
    tmp_parent = Path(work_dir) if work_dir else Path(tempfile.gettempdir())
    tmp_parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(tmp_parent).free < int(base_size * 2.5):
        raise RuntimeError(
            f"Insufficient free space in work directory {tmp_parent}: "
            f"need at least {int(base_size * 2.5)} bytes free."
        )

    tmp_ctx = tempfile.TemporaryDirectory(prefix="nebi_patch_", dir=str(tmp_parent))
    staging_zip: Path | None = None
    try:
        tmp_root = Path(tmp_ctx.name)

        with zipfile.ZipFile(base_zip_path, "r") as src_zip:
            entries = src_zip.namelist()
            cfg_entries = [n for n in entries if n.endswith(".mass.cfg.plain.unsign")]
            if len(cfg_entries) != 1:
                raise ValueError(f"Expected 1 .mass.cfg.plain.unsign in {base_zip_path}, found {cfg_entries}")
            cfg_name = cfg_entries[0]
            cfg_match = PART_FILENAME_RE.match(cfg_name)
            if not cfg_match:
                raise ValueError(f"Unexpected manifest filename format: {cfg_name}")

            mass_cfg = parse_mass_cfg(src_zip.read(cfg_name))
            orig_pkg_ver = str(mass_cfg["package"]["version"])

            x5_entries = [n for n in entries if "_0300_" in n and n.endswith(".mass.fw.plain.unsign")]
            if len(x5_entries) != 1:
                raise ValueError(f"Expected 1 X5 (0300) entry in {base_zip_path}, found {x5_entries}")
            x5_orig_name = x5_entries[0]
            x5_match = PART_FILENAME_RE.match(x5_orig_name)
            if not x5_match:
                raise ValueError(f"Unexpected X5 filename format: {x5_orig_name}")
            orig_x5_ver = x5_match.group("version")

            target_pkg_ver = (
                package_version or pkg_cfg.get("version") or (bump_version(orig_pkg_ver) if bump_ver else orig_pkg_ver)
            )
            target_x5_ver = (
                x5_version
                or pkg_cfg.get("x5_version")
                or (bump_version(orig_x5_ver) if (bump_ver and need_x5_repack) else orig_x5_ver)
            )

            if need_x5_repack and target_x5_ver == orig_x5_ver:
                warnings.append(
                    f"X5 (0300) image was modified but x5_version was kept at {orig_x5_ver}; "
                    "md_upgrade has `skip_if_firmware_version_matched = true` and may skip flashing X5!"
                )

            out_path = Path(output_zip) if output_zip else base_zip_path.parent / f"V{target_pkg_ver}_lz110.zip"
            if not out_path.name.startswith("V") or not out_path.name.lower().endswith(".zip"):
                raise SafetyValidationError(
                    "Output filename must start with 'V' and end with '.zip' "
                    f"(md_upgrade rejects non-V filenames), got {out_path.name!r}"
                )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(out_path.parent).free < int(base_size * 1.2):
                raise RuntimeError(
                    f"Insufficient free space in output directory {out_path.parent}: "
                    f"need at least {int(base_size * 1.2)} bytes free."
                )
            staging_zip = out_path.parent / f".{out_path.name}.{os.getpid()}.tmp"

            mondo_changes: dict[str, int] = {}
            hbre_changes: dict[str, int] = {}
            x5_overrides: list[str] = []
            patched_x5_file: Path | None = None
            new_x5_name = (
                f"{x5_match.group('prefix')}_0300_V{target_x5_ver}_{x5_match.group('date')}.{x5_match.group('suffix')}"
            )

            if need_x5_repack:
                patched_x5_file, mondo_changes, hbre_changes, x5_overrides = _repack_x5_archive(
                    src_zip,
                    x5_orig_name,
                    new_x5_name,
                    tmp_root,
                    need_mondo=need_mondo,
                    need_hbre=need_hbre,
                    need_x5_override=need_x5_override,
                    mondo_overlay=mondo_overlay,
                    mondo_deletes=mondo_deletes,
                    hbre_overlay=hbre_overlay,
                    hbre_deletes=hbre_deletes,
                    x5_override_dir=x5_override_dir,
                    mode_overrides=mode_overrides,
                    verify_fsck=verify_fsck,
                    min_ext4_free_bytes=min_ext4_free_bytes,
                )

            module_overrides = _write_outer_zip(
                src_zip,
                staging_zip,
                entries=entries,
                cfg_name=cfg_name,
                cfg_match=cfg_match,
                mass_cfg=mass_cfg,
                target_pkg_ver=target_pkg_ver,
                x5_orig_name=x5_orig_name,
                new_x5_name=new_x5_name,
                patched_x5_file=patched_x5_file,
                modules_override_dir=modules_override_dir,
            )

        # Gate 2: Full cryptographic, MD5, permission, and bootloader integrity audit BEFORE atomic publish.
        report = verify_package(
            staging_zip,
            base_zip_path=base_zip_path,
            allow_unsafe_x5_override=allow_unsafe_x5_override,
            logical_filename=out_path.name,
        )
        warnings.extend(report.warnings)

        staging_zip.replace(out_path)
        staging_zip = None

        return PatchResult(
            output_zip=out_path,
            package_version=target_pkg_ver,
            x5_version=target_x5_ver,
            sha256=sha256_file(out_path),
            size_bytes=out_path.stat().st_size,
            mondo_changes=mondo_changes,
            hbre_changes=hbre_changes,
            x5_overrides=x5_overrides,
            module_overrides=module_overrides,
            warnings=warnings,
        )
    except BaseException:
        if staging_zip is not None:
            staging_zip.unlink(missing_ok=True)
        raise
    finally:
        tmp_ctx.cleanup()
