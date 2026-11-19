"""Hardened e2fsprogs (`debugfs`, `e2fsck`, `dumpe2fs`) wrapper for modifying ext4 images safely."""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SEARCH_DIRS = (
    "/opt/homebrew/opt/e2fsprogs/sbin",
    "/usr/local/opt/e2fsprogs/sbin",
    "/sbin",
    "/usr/sbin",
)

_DEBUGFS_BANNER_RE = re.compile(r"^debugfs\s+\d+\.\d+.*$")
_STAT_INODE_RE = re.compile(r"Inode:\s*(?P<inode>\d+)\s+Type:\s*(?P<type>\S+)\s+Mode:\s*(?P<mode>[0-7]+)")
_STAT_SIZE_RE = re.compile(r"\bSize:\s*(?P<size>\d+)\b")

_EXEC_DIR_PREFIXES = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/")
_MIN_FREE_INODES = 16


@dataclass(frozen=True)
class Ext4Stats:
    """Superblock capacity and free-space snapshot from `dumpe2fs -h`."""

    block_count: int
    free_blocks: int
    inode_count: int
    free_inodes: int
    block_size: int

    @property
    def total_bytes(self) -> int:
        return self.block_count * self.block_size

    @property
    def free_bytes(self) -> int:
        return self.free_blocks * self.block_size


@dataclass(frozen=True)
class Ext4InodeStat:
    """Inode metadata parsed from `debugfs -R 'stat ...'`."""

    inode: int
    file_type: str
    mode: int
    size: int


@dataclass(frozen=True)
class _PlannedWrite:
    local_file: Path
    remote_file: str
    perm: int
    sha256: str
    size: int
    existed: bool


def find_e2fs_tool(name: str) -> str:
    """Locate an e2fsprogs binary such as `debugfs`, `e2fsck`, `dumpe2fs`, or `mke2fs`."""
    found = shutil.which(name)
    if found:
        return found
    for directory in _SEARCH_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(f"Could not find '{name}'. Install e2fsprogs (e.g. `brew install e2fsprogs`).")


def _quote_debugfs_arg(arg: str) -> str:
    """Quote a path/argument for a `debugfs` command script."""
    if any(ch in arg for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"Illegal control character in ext4 path: {arg!r}")
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _extract_debugfs_errors(stderr: str) -> list[str]:
    errors: list[str] = []
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line or _DEBUGFS_BANNER_RE.match(line):
            continue
        errors.append(line)
    return errors


def _sha256_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as fp:
        while chunk := fp.read(1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _blocks_bytes(size: int, block_size: int) -> int:
    return ((size + block_size - 1) // block_size) * block_size


def _resolve_file_mode(local_file: Path, remote_file: str, mode_overrides: dict[str, int]) -> int:
    if remote_file in mode_overrides:
        return mode_overrides[remote_file]
    host_mode = local_file.stat().st_mode & 0o777
    if (host_mode & 0o111) or remote_file.startswith(_EXEC_DIR_PREFIXES) or remote_file.endswith(".sh"):
        return 0o755
    return 0o644


class Ext4Image:
    """In-place editor and strict verifier for an ext4 filesystem image via `debugfs`."""

    def __init__(self, image_path: str | Path) -> None:
        self.path = Path(image_path)
        if not self.path.is_file():
            raise FileNotFoundError(f"ext4 image not found: {self.path}")
        self._debugfs = find_e2fs_tool("debugfs")
        self._e2fsck = find_e2fs_tool("e2fsck")
        self._dumpe2fs = find_e2fs_tool("dumpe2fs")

    def get_fs_stats(self) -> Ext4Stats:
        """Read superblock free space and inode counts via `dumpe2fs -h`."""
        res = subprocess.run([self._dumpe2fs, "-h", str(self.path)], capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"dumpe2fs -h failed on {self.path.name} (exit {res.returncode}):\n"
                f"{res.stderr.strip() or res.stdout.strip()}"
            )
        kv: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                kv[key.strip()] = val.strip()
        try:
            return Ext4Stats(
                block_count=int(kv["Block count"]),
                free_blocks=int(kv["Free blocks"]),
                inode_count=int(kv["Inode count"]),
                free_inodes=int(kv["Free inodes"]),
                block_size=int(kv["Block size"]),
            )
        except KeyError as exc:
            raise RuntimeError(f"Unexpected dumpe2fs output on {self.path.name}; missing field {exc}") from exc

    def run_debugfs(self, commands: list[str], write: bool = False) -> str:
        """Execute a batch of `debugfs` commands via `-f <script>`, failing on any stderr error."""
        if not commands:
            return ""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".dbg", delete=False) as tf:
            tf.write("\n".join(commands) + "\n")
            script_path = Path(tf.name)
        try:
            cmd = [self._debugfs]
            if write:
                cmd.append("-w")
            cmd.extend(["-f", str(script_path), str(self.path)])
            res = subprocess.run(cmd, capture_output=True, text=True)
            err_lines = _extract_debugfs_errors(res.stderr)
            if res.returncode != 0 or err_lines:
                detail = "\n".join(err_lines) or res.stderr.strip() or res.stdout.strip()
                raise RuntimeError(f"debugfs error on {self.path.name} (exit {res.returncode}):\n{detail}")
            return res.stdout
        finally:
            script_path.unlink(missing_ok=True)

    def stat_path(self, remote_path: str) -> Ext4InodeStat | None:
        """Return inode metadata if `remote_path` exists inside the ext4 image, else None."""
        norm = "/" + remote_path.lstrip("/")
        res = subprocess.run(
            [self._debugfs, "-R", f"stat {_quote_debugfs_arg(norm)}", str(self.path)],
            capture_output=True,
            text=True,
        )
        err_lines = _extract_debugfs_errors(res.stderr)
        if res.returncode != 0 or any("File not found" in err for err in err_lines):
            return None
        if err_lines:
            raise RuntimeError(f"debugfs stat failed for {norm} in {self.path.name}: {err_lines}")
        m_inode = _STAT_INODE_RE.search(res.stdout)
        m_size = _STAT_SIZE_RE.search(res.stdout)
        if not m_inode or not m_size:
            raise RuntimeError(f"Could not parse debugfs stat output for {norm} in {self.path.name}:\n{res.stdout}")
        return Ext4InodeStat(
            inode=int(m_inode.group("inode")),
            file_type=m_inode.group("type"),
            mode=int(m_inode.group("mode"), 8),
            size=int(m_size.group("size")),
        )

    def path_exists(self, remote_path: str) -> bool:
        """Return True if `remote_path` exists inside the ext4 image."""
        return self.stat_path(remote_path) is not None

    def dump_file_to(self, remote_path: str, dest_path: Path) -> None:
        """Stream-dump a file from inside the ext4 image to `dest_path` on the host."""
        norm = "/" + remote_path.lstrip("/")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.unlink(missing_ok=True)
        res = subprocess.run(
            [
                self._debugfs,
                "-R",
                f"dump {_quote_debugfs_arg(norm)} {_quote_debugfs_arg(str(dest_path))}",
                str(self.path),
            ],
            capture_output=True,
            text=True,
        )
        err_lines = _extract_debugfs_errors(res.stderr)
        if res.returncode != 0 or err_lines or not dest_path.is_file():
            detail = "\n".join(err_lines) or res.stderr.strip()
            raise FileNotFoundError(f"Failed to dump {norm} from {self.path.name}: {detail or 'file not created'}")

    def read_file(self, remote_path: str) -> bytes:
        """Read a file's raw bytes from inside the ext4 image."""
        with tempfile.TemporaryDirectory(prefix="nebi_patch_read_") as tmp:
            out_file = Path(tmp) / "dump.bin"
            self.dump_file_to(remote_path, out_file)
            return out_file.read_bytes()

    def read_file_prefix(self, remote_path: str, max_bytes: int = 64) -> bytes:
        """Read up to `max_bytes` from the start of a file inside the ext4 image."""
        with tempfile.TemporaryDirectory(prefix="nebi_patch_hdr_") as tmp:
            out_file = Path(tmp) / "dump.bin"
            self.dump_file_to(remote_path, out_file)
            with out_file.open("rb") as fp:
                return fp.read(max_bytes)

    def list_dir(self, remote_path: str = "/") -> str:
        """Return `ls -l` output for a directory inside the ext4 image."""
        norm = "/" + remote_path.lstrip("/")
        return self.run_debugfs([f"ls -l {_quote_debugfs_arg(norm)}"], write=False)

    def _plan_deletes(self, delete_paths: list[str], block_size: int) -> tuple[list[str], int, int]:
        deleted_targets: list[str] = []
        reclaimed_bytes = 0
        reclaimed_inodes = 0
        for raw_path in delete_paths:
            norm = "/" + raw_path.strip().lstrip("/")
            if norm == "/":
                raise ValueError("Refusing to delete root directory '/' from ext4 image")
            st = self.stat_path(norm)
            if st is None:
                continue
            if st.file_type == "directory":
                raise ValueError(f"Refusing to `rm` directory {norm} via delete list; list individual files instead")
            deleted_targets.append(norm)
            reclaimed_bytes += _blocks_bytes(st.size, block_size)
            reclaimed_inodes += 1
        return deleted_targets, reclaimed_bytes, reclaimed_inodes

    def _plan_overlay(
        self,
        overlay_dir: Path,
        deleted_set: set[str],
        mode_overrides: dict[str, int],
        block_size: int,
    ) -> tuple[list[str], list[_PlannedWrite], int, int, int, int]:
        dirs_to_create: list[str] = []
        files_to_write: list[_PlannedWrite] = []
        created_dirs: set[str] = {"/"}
        required_bytes = 0
        required_inodes = 0
        reclaimed_bytes = 0
        reclaimed_inodes = 0

        for root, dirnames, filenames in os.walk(overlay_dir):
            dirnames.sort()
            filenames.sort()
            root_path = Path(root)
            rel_dir = root_path.relative_to(overlay_dir)

            if rel_dir != Path("."):
                cur = ""
                for part in rel_dir.parts:
                    cur = posixpath.join(cur, part)
                    remote_dir = "/" + cur
                    if remote_dir in created_dirs:
                        continue
                    created_dirs.add(remote_dir)
                    existing_dir = self.stat_path(remote_dir)
                    if existing_dir is None:
                        dirs_to_create.append(remote_dir)
                        required_inodes += 1
                        required_bytes += block_size
                    elif existing_dir.file_type != "directory":
                        raise ValueError(
                            f"Cannot create directory {remote_dir} in {self.path.name}: "
                            f"non-directory ({existing_dir.file_type}) already exists at that path"
                        )

            for fname in filenames:
                if fname == ".DS_Store":
                    continue
                local_file = root_path / fname
                if local_file.is_symlink():
                    raise ValueError(f"Symlinks in patch overlay are not supported: {local_file}")
                rel_file = (rel_dir / fname).as_posix() if rel_dir != Path(".") else fname
                remote_file = "/" + rel_file.lstrip("/")

                perm = _resolve_file_mode(local_file, remote_file, mode_overrides)
                digest, fsize = _sha256_path(local_file)

                # Account for ext4 indirect/extent metadata overhead (~1% + 1 block).
                required_bytes += _blocks_bytes(fsize, block_size) + block_size + (fsize // 100)
                required_inodes += 1

                existing_file = self.stat_path(remote_file)
                existed = existing_file is not None
                if existing_file is not None and remote_file not in deleted_set:
                    if existing_file.file_type == "directory":
                        raise ValueError(
                            f"Cannot overwrite directory {remote_file} in {self.path.name} with a regular file"
                        )
                    reclaimed_bytes += _blocks_bytes(existing_file.size, block_size)
                    reclaimed_inodes += 1

                files_to_write.append(
                    _PlannedWrite(
                        local_file=local_file,
                        remote_file=remote_file,
                        perm=perm,
                        sha256=digest,
                        size=fsize,
                        existed=existed,
                    )
                )

        return dirs_to_create, files_to_write, required_bytes, required_inodes, reclaimed_bytes, reclaimed_inodes

    def _build_mutation_commands(
        self,
        deleted_targets: list[str],
        dirs_to_create: list[str],
        files_to_write: list[_PlannedWrite],
    ) -> list[str]:
        commands: list[str] = [f"rm {_quote_debugfs_arg(path)}" for path in deleted_targets]
        deleted_set = set(deleted_targets)

        for directory in dirs_to_create:
            quoted = _quote_debugfs_arg(directory)
            commands.extend(
                [
                    f"mkdir {quoted}",
                    f"set_inode_field {quoted} mode 0{0o040755:o}",
                    f"set_inode_field {quoted} uid 0",
                    f"set_inode_field {quoted} gid 0",
                ]
            )

        for item in files_to_write:
            quoted_remote = _quote_debugfs_arg(item.remote_file)
            quoted_local = _quote_debugfs_arg(str(item.local_file.resolve()))
            ext4_mode = 0o100000 | item.perm
            if item.existed and item.remote_file not in deleted_set:
                commands.append(f"rm {quoted_remote}")
            commands.extend(
                [
                    f"write {quoted_local} {quoted_remote}",
                    f"set_inode_field {quoted_remote} mode 0{ext4_mode:o}",
                    f"set_inode_field {quoted_remote} uid 0",
                    f"set_inode_field {quoted_remote} gid 0",
                ]
            )
        return commands

    def _verify_readback(self, deleted_targets: list[str], files_to_write: list[_PlannedWrite]) -> None:
        overwritten = {item.remote_file for item in files_to_write}
        for del_path in deleted_targets:
            if del_path not in overwritten and self.path_exists(del_path):
                raise RuntimeError(
                    f"Post-write verification failed on {self.path.name}: deleted path {del_path} still exists!"
                )

        with tempfile.TemporaryDirectory(prefix="nebi_patch_verify_") as tmp:
            tmp_dump = Path(tmp) / "readback.bin"
            for item in files_to_write:
                st = self.stat_path(item.remote_file)
                if st is None:
                    raise RuntimeError(
                        f"Post-write verification failed on {self.path.name}: written file {item.remote_file} is missing!"
                    )
                if st.size != item.size:
                    raise RuntimeError(
                        f"Post-write size mismatch for {item.remote_file} in {self.path.name}: "
                        f"expected {item.size} bytes, got {st.size} bytes"
                    )
                actual_perm = st.mode & 0o7777
                if actual_perm != item.perm:
                    raise RuntimeError(
                        f"Post-write permission mismatch for {item.remote_file} in {self.path.name}: "
                        f"expected {oct(item.perm)}, got {oct(actual_perm)}"
                    )
                self.dump_file_to(item.remote_file, tmp_dump)
                actual_sha256, actual_size = _sha256_path(tmp_dump)
                if actual_size != item.size or actual_sha256 != item.sha256:
                    raise RuntimeError(
                        f"Post-write SHA-256 readback mismatch for {item.remote_file} in {self.path.name}: "
                        f"expected {item.sha256}, got {actual_sha256}"
                    )

    def apply_overlay_and_deletes(
        self,
        overlay_dir: Path | None = None,
        delete_paths: list[str] | None = None,
        mode_overrides: dict[str, int] | None = None,
        *,
        min_free_bytes: int | None = None,
        verify_readback: bool = True,
    ) -> dict[str, int]:
        """Apply file deletions and directory overlay with pre-space check and read-back verification.

        1. Checks free blocks and free inodes via `dumpe2fs -h` before touching the image.
        2. Runs all mutations via `debugfs -w`, failing immediately on any `debugfs` stderr message.
        3. Verifies every deleted path is gone and reads back every written file to confirm its
           exact byte length, SHA-256 digest, and permission bits.
        """
        stats = self.get_fs_stats()
        default_reserve = min(32 * 1024 * 1024, int(stats.total_bytes * 0.03))
        reserve_bytes = default_reserve if min_free_bytes is None else max(0, min_free_bytes)

        mode_overrides_norm = {("/" + k.lstrip("/")): (v & 0o7777) for k, v in (mode_overrides or {}).items()}
        deleted_targets, reclaimed_bytes, reclaimed_inodes = self._plan_deletes(delete_paths or [], stats.block_size)

        dirs_to_create: list[str] = []
        files_to_write: list[_PlannedWrite] = []
        required_bytes = 0
        required_inodes = 0

        if overlay_dir and overlay_dir.is_dir():
            (
                dirs_to_create,
                files_to_write,
                required_bytes,
                required_inodes,
                ov_reclaimed_bytes,
                ov_reclaimed_inodes,
            ) = self._plan_overlay(overlay_dir, set(deleted_targets), mode_overrides_norm, stats.block_size)
            reclaimed_bytes += ov_reclaimed_bytes
            reclaimed_inodes += ov_reclaimed_inodes

        net_bytes_needed = max(0, required_bytes - reclaimed_bytes)
        net_inodes_needed = max(0, required_inodes - reclaimed_inodes)
        projected_free_bytes = stats.free_bytes - net_bytes_needed
        projected_free_inodes = stats.free_inodes - net_inodes_needed

        if projected_free_bytes < reserve_bytes:
            raise RuntimeError(
                f"Insufficient space in {self.path.name}: overlay needs ~{net_bytes_needed} net bytes, "
                f"free is {stats.free_bytes} bytes, leaving {projected_free_bytes} bytes "
                f"(safety reserve requires >= {reserve_bytes} bytes free)."
            )
        if projected_free_inodes < _MIN_FREE_INODES:
            raise RuntimeError(
                f"Insufficient free inodes in {self.path.name}: need {net_inodes_needed}, have {stats.free_inodes}."
            )

        commands = self._build_mutation_commands(deleted_targets, dirs_to_create, files_to_write)
        if commands:
            self.run_debugfs(commands, write=True)

        if verify_readback and (deleted_targets or files_to_write):
            self._verify_readback(deleted_targets, files_to_write)

        return {
            "deleted": len(deleted_targets),
            "written": len(files_to_write),
            "dirs_created": len(dirs_to_create),
        }

    def fsck(self) -> str:
        """Run `e2fsck -fn` to verify filesystem consistency without modifying the image."""
        res = subprocess.run([self._e2fsck, "-fn", str(self.path)], capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"e2fsck -fn failed on {self.path.name} (exit {res.returncode}):\n{res.stdout}\n{res.stderr}")
        return res.stdout.strip()
