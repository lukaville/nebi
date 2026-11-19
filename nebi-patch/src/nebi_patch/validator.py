"""Pre-pack and post-pack anti-brick safety validators for Beni (`lz110`/`lz100`) firmware packages."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from nebi_patch._toml import loads as toml_loads
from nebi_patch.ext4 import Ext4Image
from nebi_patch.packer import PART_FILENAME_RE, STREAM_CHUNK_SIZE, parse_mass_cfg

# X5 partitions/files whose accidental modification can brick bootloaders or trip AVB / anti-rollback.
PROTECTED_X5_FILES: frozenset[str] = frozenset(
    {
        "miniboot.img",
        "uboot.img",
        "vbmeta.img",
        "boot.img",
        "system.img",
        "gpt.conf",
        "ota_process",
    }
)

# Critical daemons/configs in mondo.img (/md). Because S99ota_update_check.sh runs `ota_tool -b`
# immediately at Linux boot, losing any of these soft-bricks SoftAP, UCP, and future OTA recovery.
CRITICAL_MONDO_BINARIES: tuple[str, ...] = (
    "/bin/md_sys",
    "/bin/md_upgrade",
    "/bin/md_videotrans",
    "/bin/md_netmanager",
    "/bin/md_relay",
)

CRITICAL_MONDO_PATHS: frozenset[str] = frozenset(
    {
        *CRITICAL_MONDO_BINARIES,
        "/etc/md_upgrade_config.LZ110.toml",
    }
)

CRITICAL_HBRE_PATHS: frozenset[str] = frozenset({"/bin/ota_tool"})

# Single-slot Cortex-M MCU parts with standard ARMv7-M / ARMv6-M vector tables (SP in 0x20xxxxxx SRAM).
CORTEX_M_SRAM_PARTS: frozenset[str] = frozenset(
    {
        "0200",
        "0201",
        "0202",
        "023F",
        "0500",
        "0702",
        "0712",
        "0722",
        "0732",
        "0A00",
    }
)

_MACHO_MAGICS: frozenset[bytes] = frozenset(
    {
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
)

_EXEC_DIR_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/")

EM_AARCH64 = 183


class SafetyValidationError(ValueError):
    """Raised when a patch input or produced firmware package fails an anti-brick safety gate."""


@dataclass
class PackageVerificationReport:
    """Summary returned by `verify_package()` when an OTA archive passes all integrity checks."""

    zip_path: Path
    package_version: str
    manifest_name: str
    x5_entry_name: str | None
    verified_modules: int
    verified_x5_partitions: list[str] = field(default_factory=list)
    ota_process_mode: int | None = None
    warnings: list[str] = field(default_factory=list)


def verify_aarch64_elf_header(hdr: bytes, label: str) -> None:
    """Ensure `hdr` belongs to a 64-bit little-endian ARM aarch64 ELF (`e_machine == 183`)."""
    if len(hdr) < 20 or hdr[:4] != b"\x7fELF":
        raise SafetyValidationError(f"{label}: expected ELF header (\\x7fELF), got {hdr[:8]!r}")
    ei_class = hdr[4]
    ei_data = hdr[5]
    if ei_class != 2 or ei_data != 1:
        raise SafetyValidationError(
            f"{label}: expected 64-bit little-endian ELF (class=2, data=1), got class={ei_class}, data={ei_data}"
        )
    e_machine = struct.unpack_from("<H", hdr, 18)[0]
    if e_machine != EM_AARCH64:
        raise SafetyValidationError(
            f"{label}: wrong CPU architecture in ELF (e_machine={e_machine}, expected {EM_AARCH64} ARM aarch64)"
        )


def _validate_overlay_file(fpath: Path, remote: str, tree_name: str) -> None:
    with fpath.open("rb") as fp:
        head = fp.read(256)

    if len(head) >= 4 and head[:4] in _MACHO_MAGICS:
        raise SafetyValidationError(
            f"{tree_name}{remote}: macOS Mach-O binary detected ({fpath}); "
            "Beni requires Linux ARM64 (aarch64) ELF binaries."
        )

    if head.startswith(b"MZ") and remote.startswith(("/bin/", "/sbin/", "/lib/", "/usr/")):
        raise SafetyValidationError(
            f"{tree_name}{remote}: Windows PE/MZ binary detected ({fpath}); "
            "Beni requires Linux ARM64 (aarch64) ELF binaries."
        )

    if head.startswith(b"\x7fELF"):
        verify_aarch64_elf_header(head, f"{tree_name}{remote}")

    if head.startswith(b"#!"):
        first_nl = head.find(b"\n")
        first_line = head[:first_nl] if first_nl != -1 else head
        if b"\r" in first_line:
            raise SafetyValidationError(
                f"{tree_name}{remote}: script has CRLF (\\r\\n) shebang line ({fpath}), "
                "which fails on Linux with ENOENT. Convert line endings to LF."
            )
        if b"\r\n" in fpath.read_bytes():
            raise SafetyValidationError(
                f"{tree_name}{remote}: script contains CRLF (\\r\\n) line endings ({fpath}); "
                "convert to Unix LF before flashing."
            )

    if remote.startswith(_EXEC_DIR_PREFIXES):
        if not head:
            raise SafetyValidationError(f"{tree_name}{remote}: empty 0-byte executable in {remote}")
        if not (head.startswith((b"\x7fELF", b"#!"))):
            raise SafetyValidationError(
                f"{tree_name}{remote}: file in executable directory is neither an aarch64 ELF "
                f"nor a `#!` script ({fpath})"
            )

    # Syntax-validate JSON and TOML configs so typos cannot crash-loop daemons at boot.
    lower_name = fpath.name.lower()
    if lower_name.endswith(".json"):
        try:
            json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SafetyValidationError(f"{tree_name}{remote}: invalid JSON syntax in {fpath}: {exc}") from exc
    elif lower_name.endswith(".toml"):
        try:
            toml_loads(fpath.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SafetyValidationError(f"{tree_name}{remote}: invalid TOML syntax in {fpath}: {exc}") from exc


def _validate_overlay_tree(
    overlay_dir: Path,
    delete_paths: list[str],
    critical_paths: frozenset[str],
    tree_name: str,
) -> list[str]:
    overlay_remote_paths: set[str] = set()

    if overlay_dir.is_dir():
        for root, _, filenames in os.walk(overlay_dir):
            root_path = Path(root)
            rel_dir = root_path.relative_to(overlay_dir)
            for fname in sorted(filenames):
                if fname == ".DS_Store":
                    continue
                fpath = root_path / fname
                if fpath.is_symlink():
                    raise SafetyValidationError(f"{tree_name} overlay contains unsupported symlink: {fpath}")
                rel_posix = (rel_dir / fname).as_posix() if rel_dir != Path(".") else fname
                remote = "/" + rel_posix.lstrip("/")
                overlay_remote_paths.add(remote)
                _validate_overlay_file(fpath, remote, tree_name)

    for raw_del in delete_paths:
        norm = "/" + raw_del.strip().lstrip("/")
        if norm in critical_paths and norm not in overlay_remote_paths:
            raise SafetyValidationError(
                f"Refusing to delete critical recovery file {norm} in {tree_name}.delete without a valid "
                f"replacement in {tree_name}/ (would soft-brick OTA/network after slot commit)."
            )

    return []


def validate_module_override_file(part_code: str, file_path: Path) -> None:
    """Sanity-check a raw peripheral module firmware file before allowing override."""
    code = part_code.upper()
    size = file_path.stat().st_size
    if size < 64:
        raise SafetyValidationError(f"Module override for {code} ({file_path}) is suspiciously small ({size} bytes)")
    with file_path.open("rb") as fp:
        hdr = fp.read(32)

    if code in CORTEX_M_SRAM_PARTS:
        sp, reset_handler = struct.unpack_from("<II", hdr, 0)
        if (sp & 0xFF000000) != 0x20000000 or (sp & 0x3) != 0:
            raise SafetyValidationError(
                f"Module {code} ({file_path.name}): invalid Cortex-M initial SP 0x{sp:08x} "
                "(expected 4-byte aligned 0x20xxxxxx SRAM address)"
            )
        if (reset_handler & 1) != 1 or reset_handler < 0x100:
            raise SafetyValidationError(
                f"Module {code} ({file_path.name}): invalid Cortex-M Reset_Handler 0x{reset_handler:08x} "
                "(expected Thumb bit0 == 1)"
            )
    elif code == "0301":
        if not zipfile.is_zipfile(file_path):
            raise SafetyValidationError(f"Module 0301 (A350) override ({file_path.name}) must be a valid ZIP archive")
    elif code == "0800" and not hdr.startswith(b"Rar!"):
        raise SafetyValidationError(f"Module 0800 override ({file_path.name}) does not have expected RAR header")


def _validate_x5_overrides(x5_dir: Path, *, allow_unsafe_x5_override: bool) -> list[str]:
    warnings: list[str] = []
    if not x5_dir.is_dir():
        return warnings
    for item in sorted(x5_dir.iterdir()):
        if item.name == ".DS_Store" or not item.is_file():
            continue
        if item.name == "0300":
            raise SafetyValidationError("Do not place '0300' inside x5/; place partition files only.")
        if item.name in PROTECTED_X5_FILES:
            if not allow_unsafe_x5_override:
                raise SafetyValidationError(
                    f"x5/{item.name} modifies a protected bootloader/AVB/partition file "
                    f"({sorted(PROTECTED_X5_FILES)}). Flashing mismatched bootloaders or AVB-protected "
                    "partitions (boot.img/system.img without re-signing vbmeta.img) can brick X5! "
                    "Pass --allow-unsafe-x5-override only if you have verified AVB & anti-rollback counters."
                )
            warnings.append(f"UNSAFE OVERRIDE ENABLED: x5/{item.name}")
        if item.name == "ota_process":
            with item.open("rb") as fp:
                verify_aarch64_elf_header(fp.read(64), "x5/ota_process")
    return warnings


def _validate_module_overrides(modules_dir: Path, *, allow_module_override: bool) -> list[str]:
    warnings: list[str] = []
    if not modules_dir.is_dir():
        return warnings
    mod_files = [p for p in sorted(modules_dir.iterdir()) if p.is_file() and p.name != ".DS_Store"]
    if mod_files and not allow_module_override:
        names = [p.name for p in mod_files]
        raise SafetyValidationError(
            f"modules/ contains peripheral firmware overrides {names}. "
            "Peripheral MCUs (ESC, PMU, BAT, RC) are single-slot with NO A/B rollback! "
            "Pass --allow-module-override to confirm intentional peripheral flashing."
        )
    for mf in mod_files:
        match = PART_FILENAME_RE.match(mf.name)
        part_code = match.group("part").upper() if match else mf.stem.upper()
        validate_module_override_file(part_code, mf)
        warnings.append(f"Peripheral module override enabled: {part_code} ({mf.name})")
    return warnings


def validate_patch_inputs(
    patch_dir: Path,
    *,
    mondo_deletes: list[str],
    hbre_deletes: list[str],
    allow_unsafe_x5_override: bool = False,
    allow_module_override: bool = False,
) -> list[str]:
    """Validate all files and directives in `patch_dir` before touching any firmware image."""
    warnings: list[str] = []
    warnings.extend(_validate_overlay_tree(patch_dir / "mondo", mondo_deletes, CRITICAL_MONDO_PATHS, "mondo"))
    warnings.extend(_validate_overlay_tree(patch_dir / "hbre", hbre_deletes, CRITICAL_HBRE_PATHS, "hbre"))
    warnings.extend(_validate_x5_overrides(patch_dir / "x5", allow_unsafe_x5_override=allow_unsafe_x5_override))
    warnings.extend(_validate_module_overrides(patch_dir / "modules", allow_module_override=allow_module_override))
    return warnings


def validate_patched_mondo_image(mondo_img: Ext4Image, had_system_daemons: bool) -> None:
    """Verify critical recovery daemons remain intact, executable, and valid AArch64 ELFs in `mondo.img`."""
    if not had_system_daemons:
        return
    for bin_path in CRITICAL_MONDO_BINARIES:
        st = mondo_img.stat_path(bin_path)
        if st is None:
            raise SafetyValidationError(f"Post-patch check failed: critical daemon {bin_path} is missing from mondo.img!")
        if st.size < 1024:
            raise SafetyValidationError(
                f"Post-patch check failed: critical daemon {bin_path} is truncated ({st.size} bytes)!"
            )
        if (st.mode & 0o111) == 0:
            raise SafetyValidationError(
                f"Post-patch check failed: critical daemon {bin_path} lost execute permission (mode={oct(st.mode)})!"
            )
        hdr = mondo_img.read_file_prefix(bin_path, 64)
        verify_aarch64_elf_header(hdr, f"mondo.img:{bin_path}")


def _stream_hash_zip_entry(
    zf: zipfile.ZipFile,
    name: str,
    algo: str = "sha256",
    scope_bytes: int | None = None,
) -> tuple[str, int]:
    digest = hashlib.new(algo)
    total = 0
    remaining = scope_bytes
    with zf.open(name, "r") as fp:
        while True:
            to_read = STREAM_CHUNK_SIZE if remaining is None else min(STREAM_CHUNK_SIZE, remaining)
            if remaining is not None and to_read <= 0:
                break
            chunk = fp.read(to_read)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest(), total


def _open_nested_zip(outer_zf: zipfile.ZipFile, entry_name: str, tmp_spool: IO[bytes]) -> zipfile.ZipFile:
    """Stream-copy `entry_name` into a spooled file so 600 MB+ `0300` inner ZIPs do not exhaust RAM."""
    tmp_spool.seek(0)
    tmp_spool.truncate(0)
    with outer_zf.open(entry_name, "r") as rfp:
        while chunk := rfp.read(STREAM_CHUNK_SIZE):
            tmp_spool.write(chunk)
    tmp_spool.seek(0)
    return zipfile.ZipFile(tmp_spool, "r")


def _collect_base_x5_crcs(base_zip_path: str | Path | None) -> dict[str, tuple[int, int]]:
    base_x5_crcs: dict[str, tuple[int, int]] = {}
    if base_zip_path is None or not Path(base_zip_path).is_file():
        return base_x5_crcs
    with zipfile.ZipFile(base_zip_path, "r") as bzf:
        bx5 = [n for n in bzf.namelist() if "_0300_" in n and n.endswith(".mass.fw.plain.unsign")]
        if not bx5:
            return base_x5_crcs
        with (
            tempfile.TemporaryFile() as spool,
            _open_nested_zip(bzf, bx5[0], spool) as bx5_zf,
        ):
            for zi in bx5_zf.infolist():
                base_x5_crcs[zi.filename] = (zi.CRC, zi.file_size)
    return base_x5_crcs


def _verify_outer_modules(
    zf: zipfile.ZipFile,
    entries: list[str],
    manifest_name: str,
    firmwares: list[dict[str, Any]],
) -> tuple[int, str | None]:
    fw_entries_by_part: dict[str, list[str]] = {}
    for name in entries:
        if name == manifest_name:
            continue
        match = PART_FILENAME_RE.match(name)
        if not match:
            raise SafetyValidationError(f"Invalid module filename inside outer ZIP (md_upgrade will reject): {name!r}")
        fw_entries_by_part.setdefault(match.group("part").upper(), []).append(name)

    verified_modules = 0
    x5_entry_name: str | None = None

    for fw in firmwares:
        part = str(fw["part"]).upper()
        matches = fw_entries_by_part.get(part, [])
        if len(matches) != 1:
            raise SafetyValidationError(f"Manifest part {part} expected 1 file in outer ZIP, found {matches}")
        entry_name = matches[0]
        if part == "0300":
            x5_entry_name = entry_name

        expected_size = int(fw["size"])
        expected_cksum = str(fw["checksum"])
        if not expected_cksum.startswith("sha256:"):
            raise SafetyValidationError(f"Part {part} checksum must start with 'sha256:', got {expected_cksum!r}")
        expected_hex = expected_cksum.split(":", 1)[1].lower()
        actual_hex, actual_size = _stream_hash_zip_entry(zf, entry_name, "sha256")
        if actual_size != expected_size:
            raise SafetyValidationError(
                f"Part {part} ({entry_name}) size mismatch: manifest={expected_size}, actual={actual_size}"
            )
        if actual_hex != expected_hex:
            raise SafetyValidationError(
                f"Part {part} ({entry_name}) SHA-256 mismatch: manifest={expected_hex}, actual={actual_hex}"
            )
        verified_modules += 1

    return verified_modules, x5_entry_name


def _verify_inner_x5_archive(
    outer_zf: zipfile.ZipFile,
    x5_entry_name: str,
    base_x5_crcs: dict[str, tuple[int, int]],
    *,
    allow_unsafe_x5_override: bool,
) -> tuple[list[str], int | None]:
    verified_x5_partitions: list[str] = []
    ota_process_mode: int | None = None

    with (
        tempfile.TemporaryFile() as spool,
        _open_nested_zip(outer_zf, x5_entry_name, spool) as x5_zf,
    ):
        bad_inner = x5_zf.testzip()
        if bad_inner is not None:
            raise SafetyValidationError(f"Corrupt inner 0300 ZIP entry CRC ({x5_entry_name}): {bad_inner}")
        x5_names = set(x5_zf.namelist())
        for req_meta in ("gpt.conf", "data.json"):
            if req_meta not in x5_names:
                raise SafetyValidationError(f"Inner 0300 ZIP ({x5_entry_name}) is missing required file {req_meta!r}")

        if "ota_process" in x5_names:
            op_info = x5_zf.getinfo("ota_process")
            unix_mode = (op_info.external_attr >> 16) & 0o7777
            ota_process_mode = unix_mode
            if op_info.create_system != 3 or (unix_mode & 0o111) != 0o111:
                raise SafetyValidationError(
                    f"Inner 0300 `ota_process` lacks Unix execute permissions "
                    f"(create_system={op_info.create_system}, mode={oct(unix_mode)}); "
                    "`ota_tool` execve() will fail with EACCES!"
                )
            with x5_zf.open("ota_process", "r") as op_fp:
                verify_aarch64_elf_header(op_fp.read(64), "0300/ota_process")

        if base_x5_crcs and not allow_unsafe_x5_override:
            for prot_name in PROTECTED_X5_FILES:
                if prot_name in base_x5_crcs and prot_name in x5_names:
                    cur_zi = x5_zf.getinfo(prot_name)
                    if (cur_zi.CRC, cur_zi.file_size) != base_x5_crcs[prot_name]:
                        raise SafetyValidationError(
                            f"Protected X5 file {prot_name!r} differs from base firmware "
                            "(CRC/size changed) without --allow-unsafe-x5-override!"
                        )

        data_json = json.loads(x5_zf.read("data.json").decode("utf-8"))
        part_info: dict[str, Any] = data_json.get("partition_info", {})
        update_parts = set(data_json.get("update_partition", []))

        for part_name, pmeta in part_info.items():
            imgname = pmeta.get("imgname")
            if not imgname:
                continue
            if imgname not in x5_names:
                if part_name in update_parts:
                    raise SafetyValidationError(
                        f"0300 data.json lists {part_name!r} ({imgname}) in update_partition, "
                        f"but {imgname} is missing from 0300 ZIP!"
                    )
                continue

            expected_md5 = str(pmeta.get("md5sum", {}).get(imgname, "")).lower()
            expected_scope = int(pmeta.get("md5_scope", {}).get(imgname, -1))
            zi = x5_zf.getinfo(imgname)
            if expected_scope <= 0 or expected_scope > zi.file_size:
                raise SafetyValidationError(
                    f"0300 data.json invalid md5_scope={expected_scope} for {imgname} (file_size={zi.file_size})"
                )
            actual_md5, hashed_len = _stream_hash_zip_entry(x5_zf, imgname, "md5", scope_bytes=expected_scope)
            if hashed_len != expected_scope or actual_md5 != expected_md5:
                raise SafetyValidationError(
                    f"0300 data.json MD5 mismatch for {imgname}: "
                    f"expected {expected_md5} (scope={expected_scope}), got {actual_md5}"
                )
            verified_x5_partitions.append(part_name)

    return verified_x5_partitions, ota_process_mode


def verify_package(
    zip_path: str | Path,
    *,
    base_zip_path: str | Path | None = None,
    allow_unsafe_x5_override: bool = False,
    logical_filename: str | None = None,
) -> PackageVerificationReport:
    """Perform a full structural, permission, MD5, and SHA-256 verification of an OTA `.zip` package."""
    pkg_path = Path(zip_path)
    if not pkg_path.is_file():
        raise FileNotFoundError(f"Firmware ZIP not found: {pkg_path}")

    check_name = logical_filename or pkg_path.name
    if not check_name.startswith("V") or not check_name.lower().endswith(".zip"):
        raise SafetyValidationError(
            "Outer firmware ZIP filename must start with 'V' and end with '.zip' "
            f"(e.g. V26.10.28.05_lz110.zip), got {check_name!r}"
        )

    warnings: list[str] = []
    base_x5_crcs = _collect_base_x5_crcs(base_zip_path)

    with zipfile.ZipFile(pkg_path, "r") as zf:
        bad_member = zf.testzip()
        if bad_member is not None:
            raise SafetyValidationError(f"Corrupt ZIP entry CRC in {pkg_path.name}: {bad_member}")

        entries = zf.namelist()
        cfg_entries = [n for n in entries if n.endswith(".mass.cfg.plain.unsign")]
        if len(cfg_entries) != 1:
            raise SafetyValidationError(
                f"Expected exactly 1 .mass.cfg.plain.unsign manifest in {pkg_path.name}, found {cfg_entries}"
            )
        manifest_name = cfg_entries[0]
        cfg = parse_mass_cfg(zf.read(manifest_name))
        pkg_ver = str(cfg.get("package", {}).get("version", ""))
        if not pkg_ver:
            raise SafetyValidationError(f"Missing [package].version in {manifest_name}")

        expected_prefix = f"V{pkg_ver}_"
        if not check_name.startswith(expected_prefix):
            warnings.append(
                f"Outer filename {check_name!r} does not start with {expected_prefix!r} "
                "(md_upgrade extracts version from filename)"
            )

        firmwares: list[dict[str, Any]] = cfg.get("firmwares", [])
        if not firmwares:
            raise SafetyValidationError(f"No [[firmwares]] entries in {manifest_name}")

        verified_modules, x5_entry_name = _verify_outer_modules(zf, entries, manifest_name, firmwares)

        verified_x5_partitions: list[str] = []
        ota_process_mode: int | None = None
        if x5_entry_name is not None:
            verified_x5_partitions, ota_process_mode = _verify_inner_x5_archive(
                zf,
                x5_entry_name,
                base_x5_crcs,
                allow_unsafe_x5_override=allow_unsafe_x5_override,
            )

    return PackageVerificationReport(
        zip_path=pkg_path,
        package_version=pkg_ver,
        manifest_name=manifest_name,
        x5_entry_name=x5_entry_name,
        verified_modules=verified_modules,
        verified_x5_partitions=verified_x5_partitions,
        ota_process_mode=ota_process_mode,
        warnings=warnings,
    )
