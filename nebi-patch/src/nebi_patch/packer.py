"""Firmware manifest, checksum, version, and Unix-preserving ZIP packaging utilities."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nebi_patch._toml import loads as toml_loads
from nebi_patch._toml import render_mass_cfg

PART_FILENAME_RE = re.compile(
    r"^(?P<prefix>[a-zA-Z0-9]+)_(?P<part>[0-9A-Fa-f]{4})_V(?P<version>\d+\.\d+\.\d+\.\d+)_(?P<date>\d+)\.(?P<suffix>.+)$"
)

STREAM_CHUNK_SIZE = 4 * 1024 * 1024
_ZIP64_THRESHOLD = (1 << 31) - 4096
_UNIX_SYSTEM = 3
_REG_FILE_MASK = 0o100000


def _unix_zip_info(
    filename: str,
    *,
    compress_type: int,
    unix_mode: int,
    date_time: tuple[int, int, int, int, int, int] | None = None,
) -> zipfile.ZipInfo:
    if date_time is None:
        now = datetime.now(timezone.utc)
        date_time = (now.year, now.month, now.day, now.hour, now.minute, now.second)
    zinfo = zipfile.ZipInfo(filename=filename, date_time=date_time)
    zinfo.compress_type = compress_type
    zinfo.create_system = _UNIX_SYSTEM
    zinfo.external_attr = (_REG_FILE_MASK | (unix_mode & 0o7777)) << 16
    return zinfo


def sha256_file(path: Path, chunk_size: int = STREAM_CHUNK_SIZE) -> str:
    """Compute the lowercase hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path: Path, chunk_size: int = STREAM_CHUNK_SIZE) -> tuple[str, int]:
    """Compute `(md5_hex, byte_length)` of a file in a single streaming pass."""
    digest = hashlib.md5()
    total = 0
    with path.open("rb") as fp:
        while chunk := fp.read(chunk_size):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def bump_version(ver: str) -> str:
    """Increment the final numeric segment of a `YY.MM.DD.ID` version string."""
    parts = ver.lstrip("Vv").split(".")
    if not parts or not parts[-1].isdigit():
        raise ValueError(f"Cannot auto-bump non-numeric version: {ver!r}")
    width = len(parts[-1])
    parts[-1] = f"{int(parts[-1]) + 1:0{width}d}"
    return ".".join(parts)


def parse_mass_cfg(raw_toml: bytes) -> dict[str, Any]:
    """Parse a `.mass.cfg.plain.unsign` TOML manifest."""
    return toml_loads(raw_toml.decode("utf-8"))


def update_x5_data_json(
    data_json_path: Path,
    replacement_files: dict[str, Path],
) -> list[str]:
    """Recompute `md5sum` and `md5_scope` in `data.json` for modified partition images."""
    data = json.loads(data_json_path.read_text(encoding="utf-8"))
    part_info: dict[str, Any] = data.get("partition_info", {})
    updated: list[str] = []

    for info in part_info.values():
        imgname = info.get("imgname")
        if not imgname or imgname not in replacement_files:
            continue
        img_path = replacement_files[imgname]
        if not img_path.is_file():
            raise FileNotFoundError(f"Missing X5 partition image {imgname} at {img_path}")
        digest, size = md5_file(img_path)
        info.setdefault("md5sum", {})[imgname] = digest
        info.setdefault("md5_scope", {})[imgname] = size
        updated.append(imgname)

    data_json_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    return updated


def copy_zip_entry_streaming(
    src_zip: zipfile.ZipFile,
    src_name: str,
    dest_zip: zipfile.ZipFile,
    dest_name: str | None = None,
    compress_type: int | None = zipfile.ZIP_STORED,
    force_unix_mode: int | None = None,
) -> tuple[int, str]:
    """Stream-copy an entry from `src_zip` into `dest_zip`, preserving Unix permissions."""
    target_name = dest_name or src_name
    src_info = src_zip.getinfo(src_name)
    resolved_compress = src_info.compress_type if compress_type is None else compress_type

    if force_unix_mode is not None:
        zinfo = _unix_zip_info(
            target_name,
            compress_type=resolved_compress,
            unix_mode=force_unix_mode,
            date_time=src_info.date_time,
        )
    elif src_info.create_system == _UNIX_SYSTEM and src_info.external_attr:
        zinfo = zipfile.ZipInfo(filename=target_name, date_time=src_info.date_time)
        zinfo.compress_type = resolved_compress
        zinfo.create_system = _UNIX_SYSTEM
        zinfo.external_attr = src_info.external_attr
    else:
        default_mode = 0o755 if target_name == "ota_process" else 0o644
        zinfo = _unix_zip_info(
            target_name,
            compress_type=resolved_compress,
            unix_mode=default_mode,
            date_time=src_info.date_time,
        )

    digest = hashlib.sha256()
    total = 0
    force_z64 = src_info.file_size >= _ZIP64_THRESHOLD
    with src_zip.open(src_name, "r") as rfp, dest_zip.open(zinfo, "w", force_zip64=force_z64) as wfp:
        while chunk := rfp.read(STREAM_CHUNK_SIZE):
            digest.update(chunk)
            total += len(chunk)
            wfp.write(chunk)
    return total, digest.hexdigest()


def write_file_to_zip_streaming(
    src_path: Path,
    dest_zip: zipfile.ZipFile,
    arcname: str,
    compress_type: int = zipfile.ZIP_STORED,
    unix_mode: int = 0o644,
) -> tuple[int, str]:
    """Stream-write `src_path` into `dest_zip` with explicit Unix mode and return `(size, sha256)`."""
    file_size = src_path.stat().st_size
    zinfo = _unix_zip_info(arcname, compress_type=compress_type, unix_mode=unix_mode)
    digest = hashlib.sha256()
    total = 0
    force_z64 = file_size >= _ZIP64_THRESHOLD
    with src_path.open("rb") as rfp, dest_zip.open(zinfo, "w", force_zip64=force_z64) as wfp:
        while chunk := rfp.read(STREAM_CHUNK_SIZE):
            digest.update(chunk)
            total += len(chunk)
            wfp.write(chunk)
    return total, digest.hexdigest()


def write_bytes_to_zip_unix(
    dest_zip: zipfile.ZipFile,
    arcname: str,
    payload: bytes,
    compress_type: int = zipfile.ZIP_DEFLATED,
    unix_mode: int = 0o644,
) -> None:
    """Write small in-memory bytes (`mass.cfg`, `data.json`) with clean Unix permissions."""
    zinfo = _unix_zip_info(arcname, compress_type=compress_type, unix_mode=unix_mode)
    dest_zip.writestr(zinfo, payload)


def repack_x5_zip_streaming(
    base_x5_zip: Path,
    dest_x5_zip: Path,
    replacement_files: dict[str, Path],
    compresslevel: int = 1,
) -> None:
    """Rebuild the `0300` X5 inner ZIP preserving vendor entry order and `ota_process` `0755` mode."""
    written_names: set[str] = set()
    with (
        zipfile.ZipFile(base_x5_zip, "r") as src_zf,
        zipfile.ZipFile(
            dest_x5_zip,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=compresslevel,
            allowZip64=True,
        ) as dst_zf,
    ):
        for src_info in src_zf.infolist():
            name = src_info.filename
            written_names.add(name)
            if name in replacement_files:
                mode = 0o755 if name == "ota_process" else 0o644
                write_file_to_zip_streaming(
                    replacement_files[name],
                    dst_zf,
                    name,
                    compress_type=zipfile.ZIP_DEFLATED,
                    unix_mode=mode,
                )
            else:
                forced_mode = 0o755 if name == "ota_process" else None
                copy_zip_entry_streaming(
                    src_zf,
                    name,
                    dst_zf,
                    compress_type=zipfile.ZIP_DEFLATED,
                    force_unix_mode=forced_mode,
                )

        for name, path in sorted(replacement_files.items()):
            if name not in written_names:
                mode = 0o755 if name == "ota_process" else 0o644
                write_file_to_zip_streaming(
                    path,
                    dst_zf,
                    name,
                    compress_type=zipfile.ZIP_DEFLATED,
                    unix_mode=mode,
                )


def ensure_clean_dir(path: Path) -> Path:
    """Delete and recreate `path` as an empty directory."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = [
    "PART_FILENAME_RE",
    "STREAM_CHUNK_SIZE",
    "bump_version",
    "copy_zip_entry_streaming",
    "ensure_clean_dir",
    "md5_file",
    "parse_mass_cfg",
    "render_mass_cfg",
    "repack_x5_zip_streaming",
    "sha256_file",
    "update_x5_data_json",
    "write_bytes_to_zip_unix",
    "write_file_to_zip_streaming",
]
