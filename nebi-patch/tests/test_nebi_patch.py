"""Comprehensive safety, packaging, ext4, CLI, and V1/V2 upload tests for `nebi-patch`."""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import struct
import subprocess
import threading
import zipfile
from email.parser import BytesParser
from email.policy import default as email_default
from pathlib import Path

import pytest

from nebi_patch.cli import main as cli_main
from nebi_patch.ext4 import Ext4Image, find_e2fs_tool
from nebi_patch.packer import bump_version, parse_mass_cfg, render_mass_cfg
from nebi_patch.patcher import patch_firmware
from nebi_patch.uploader import (
    DEFAULT_OTA_PORT,
    OTA_CMD_CANCEL,
    OTA_CMD_INSTALL,
    OTA_CMD_QUERY,
    OTA_CMD_START,
    check_robot_ota,
    resolve_bearer_token,
    trigger_v2_http_install,
    upload_firmware,
)
from nebi_patch.validator import SafetyValidationError, validate_patch_inputs, verify_package


def _has_e2fsprogs() -> bool:
    for tool in ("mke2fs", "debugfs", "e2fsck", "dumpe2fs"):
        try:
            find_e2fs_tool(tool)
        except FileNotFoundError:
            return False
    return True


requires_e2fsprogs = pytest.mark.skipif(not _has_e2fsprogs(), reason="e2fsprogs not installed")


def _fake_aarch64_elf(size: int = 2048) -> bytes:
    """Construct a minimal 64-bit little-endian ARM aarch64 (`e_machine=183`) ELF stub."""
    hdr = bytearray(64)
    hdr[0:4] = b"\x7fELF"
    hdr[4] = 2  # ELFCLASS64
    hdr[5] = 1  # ELFDATA2LSB
    hdr[6] = 1  # EV_CURRENT
    struct.pack_into("<H", hdr, 16, 2)    # ET_EXEC
    struct.pack_into("<H", hdr, 18, 183)  # EM_AARCH64
    struct.pack_into("<I", hdr, 20, 1)
    if size > len(hdr):
        hdr.extend(b"\x00" * (size - len(hdr)))
    return bytes(hdr)


def _fake_x86_64_elf() -> bytes:
    hdr = bytearray(_fake_aarch64_elf(128))
    struct.pack_into("<H", hdr, 18, 62)  # EM_X86_64
    return bytes(hdr)


def _create_empty_ext4(path: Path, size_mb: int = 4) -> None:
    mke2fs = find_e2fs_tool("mke2fs")
    res = subprocess.run(
        [mke2fs, "-t", "ext4", "-q", "-F", str(path), f"{size_mb}M"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"mke2fs failed: {res.stderr}")


def _build_synthetic_firmware(work: Path, *, with_ext4: bool = True) -> Path:
    """Create a structurally authentic `V26.10.28.04_lz110.zip` including critical daemons & bootloaders."""
    work.mkdir(parents=True, exist_ok=True)
    if with_ext4:
        mondo_img = work / "mondo.img"
        _create_empty_ext4(mondo_img, size_mb=6)
        seed_dir = work / "seed_mondo"
        (seed_dir / "bin").mkdir(parents=True)
        (seed_dir / "etc").mkdir(parents=True)
        for daemon in ("md_sys", "md_upgrade", "md_videotrans", "md_netmanager", "md_relay"):
            dp = seed_dir / "bin" / daemon
            dp.write_bytes(_fake_aarch64_elf(2048))
            os.chmod(dp, 0o755)
        (seed_dir / "etc" / "md_upgrade_config.LZ110.toml").write_text("[upgrade]\nschema = 1\n", encoding="utf-8")
        (seed_dir / "etc" / "existing.txt").write_text("original\n", encoding="utf-8")
        (seed_dir / "etc" / "remove_me.txt").write_text("bye\n", encoding="utf-8")
        Ext4Image(mondo_img).apply_overlay_and_deletes(overlay_dir=seed_dir)

        hbre_img = work / "hbre.img"
        _create_empty_ext4(hbre_img, size_mb=3)
        seed_hbre = work / "seed_hbre"
        (seed_hbre / "bin").mkdir(parents=True)
        ota_tool_p = seed_hbre / "bin" / "ota_tool"
        ota_tool_p.write_bytes(_fake_aarch64_elf(2048))
        os.chmod(ota_tool_p, 0o755)
        Ext4Image(hbre_img).apply_overlay_and_deletes(overlay_dir=seed_hbre)

        mondo_bytes = mondo_img.read_bytes()
        hbre_bytes = hbre_img.read_bytes()
    else:
        mondo_bytes = b"SYNTHETIC_MONDO_IMG" * 64
        hbre_bytes = b"SYNTHETIC_HBRE_IMG" * 64

    miniboot_bytes = b"MINIBOOT_STAGE1_IMAGE_V1" * 16
    vbmeta_bytes = b"AVB0_VBMETA_DESCRIPTORS!" * 16
    ota_proc_bytes = _fake_aarch64_elf(2048)

    data_json = {
        "sys_version": "LNX6.1.83_PL5.1_V1.1.1_20261028-1939",
        "update_partition": ["miniboot", "vbmeta", "hbre", "mondo"],
        "partition_info": {
            "miniboot": {
                "md5sum": {"miniboot.img": hashlib.md5(miniboot_bytes).hexdigest()},
                "md5_scope": {"miniboot.img": len(miniboot_bytes)},
                "medium": "emmc",
                "part_type": "BAK",
                "have_anti_ver": True,
                "imgname": "miniboot.img",
            },
            "vbmeta": {
                "md5sum": {"vbmeta.img": hashlib.md5(vbmeta_bytes).hexdigest()},
                "md5_scope": {"vbmeta.img": len(vbmeta_bytes)},
                "medium": "emmc",
                "part_type": "AB",
                "have_anti_ver": True,
                "imgname": "vbmeta.img",
            },
            "hbre": {
                "md5sum": {"hbre.img": hashlib.md5(hbre_bytes).hexdigest()},
                "md5_scope": {"hbre.img": len(hbre_bytes)},
                "medium": "emmc",
                "part_type": "AB",
                "imgname": "hbre.img",
            },
            "mondo": {
                "md5sum": {"mondo.img": hashlib.md5(mondo_bytes).hexdigest()},
                "md5_scope": {"mondo.img": len(mondo_bytes)},
                "medium": "emmc",
                "part_type": "AB",
                "imgname": "mondo.img",
            },
        },
    }

    x5_zip_path = work / "lz110_0300_V26.10.27.45_20261028.mass.fw.plain.unsign"
    with zipfile.ZipFile(x5_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("gpt.conf", "mondo_a:1:2:2\n")
        zf.writestr("data.json", json.dumps(data_json, indent=4))
        zf.writestr("miniboot.img", miniboot_bytes)
        zf.writestr("vbmeta.img", vbmeta_bytes)
        zf.writestr("hbre.img", hbre_bytes)
        zf.writestr("mondo.img", mondo_bytes)
        op_zi = zipfile.ZipInfo("ota_process", date_time=(2026, 10, 28, 12, 0, 0))
        op_zi.compress_type = zipfile.ZIP_DEFLATED
        op_zi.create_system = 3
        op_zi.external_attr = 0o100755 << 16
        zf.writestr(op_zi, ota_proc_bytes)

    # Authentic Cortex-M vector table for ESC 0200 (SP=0x20002408, Reset=0x0000319d Thumb)
    esc_bytes = struct.pack("<II", 0x20002408, 0x0000319D) + (b"\x00" * 256)
    x5_bytes = x5_zip_path.read_bytes()
    mass_cfg = (
        "[package]\n"
        "version = '26.10.28.04'\n"
        "package_id = 'upgrade_20261028_26102804'\n"
        "created_at = '2026-10-28T12:01:24Z'\n\n"
        "[[firmwares]]\n"
        "part = '0200'\n"
        "device_type = 'ESC'\n"
        f"size = {len(esc_bytes)}\n"
        f"checksum = 'sha256:{hashlib.sha256(esc_bytes).hexdigest()}'\n"
        "priority = 0\n"
        "required = true\n\n"
        "[[firmwares]]\n"
        "part = '0300'\n"
        "device_type = 'X5'\n"
        f"size = {len(x5_bytes)}\n"
        f"checksum = 'sha256:{hashlib.sha256(x5_bytes).hexdigest()}'\n"
        "priority = 0\n"
        "required = true\n\n"
        "[strategy]\n"
        "max_concurrent = 2\n"
        "global_timeout = 3600\n"
    )

    outer_zip = work / "V26.10.28.04_lz110.zip"
    with zipfile.ZipFile(outer_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("lz110_0200_V26.09.12.77_20261028.mass.fw.plain.unsign", esc_bytes)
        zf.write(x5_zip_path, x5_zip_path.name)
        zf.writestr("lz110_0000_V26.10.28.04_20261028.mass.cfg.plain.unsign", mass_cfg)
    return outer_zip


def test_protocol_constants() -> None:
    assert DEFAULT_OTA_PORT == 8085
    assert OTA_CMD_START == 0
    assert OTA_CMD_CANCEL == 1
    assert OTA_CMD_QUERY == 3
    assert OTA_CMD_INSTALL == 4


def test_bump_version_and_mass_cfg_roundtrip() -> None:
    assert bump_version("26.10.28.04") == "26.10.28.05"
    assert bump_version("V26.10.28.09") == "26.10.28.10"
    with pytest.raises(ValueError):
        bump_version("26.10.28.dev")

    cfg = {
        "package": {"version": "26.10.28.05", "package_id": "upgrade_20261028_26102805"},
        "firmwares": [{"part": "0300", "size": 1234, "required": True, "checksum": "sha256:abcd"}],
        "strategy": {"max_concurrent": 2},
    }
    rendered = render_mass_cfg(cfg)
    assert parse_mass_cfg(rendered.encode("utf-8")) == cfg


def test_bearer_token_resolution_has_no_hardcoded_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NEBI_HTTP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("PYNEBI_MEDIA_TOKEN", raising=False)
    monkeypatch.setattr("nebi_patch.uploader._media_token_file", lambda: tmp_path / "missing_token")
    assert resolve_bearer_token(None) == ""

    tok_file = tmp_path / "media_token"
    tok_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setattr("nebi_patch.uploader._media_token_file", lambda: tok_file)
    assert resolve_bearer_token(None) == "from-file"

    monkeypatch.setenv("PYNEBI_MEDIA_TOKEN", "from-pynebi-env")
    assert resolve_bearer_token(None) == "from-pynebi-env"

    monkeypatch.setenv("NEBI_HTTP_BEARER_TOKEN", "from-nebi-env")
    assert resolve_bearer_token(None) == "from-nebi-env"

    assert resolve_bearer_token("explicit-arg") == "explicit-arg"
    assert resolve_bearer_token("") == ""


def test_validate_patch_inputs_blocks_wrong_arch_crlf_broken_config_and_critical_delete(tmp_path: Path) -> None:
    # 1. macOS Mach-O 64-bit magic
    p_macho = tmp_path / "p_macho"
    (p_macho / "mondo" / "bin").mkdir(parents=True)
    (p_macho / "mondo" / "bin" / "tool").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
    with pytest.raises(SafetyValidationError, match="Mach-O"):
        validate_patch_inputs(p_macho, mondo_deletes=[], hbre_deletes=[])

    # 2. x86_64 ELF instead of ARM64
    p_x86 = tmp_path / "p_x86"
    (p_x86 / "mondo" / "bin").mkdir(parents=True)
    (p_x86 / "mondo" / "bin" / "tool").write_bytes(_fake_x86_64_elf())
    with pytest.raises(SafetyValidationError, match="wrong CPU architecture"):
        validate_patch_inputs(p_x86, mondo_deletes=[], hbre_deletes=[])

    # 3. CRLF shebang script
    p_crlf = tmp_path / "p_crlf"
    (p_crlf / "mondo" / "bin").mkdir(parents=True)
    (p_crlf / "mondo" / "bin" / "run.sh").write_bytes(b"#!/bin/sh\r\necho broken\r\n")
    with pytest.raises(SafetyValidationError, match="CRLF"):
        validate_patch_inputs(p_crlf, mondo_deletes=[], hbre_deletes=[])

    # 4. Syntax error in TOML config
    p_toml = tmp_path / "p_toml"
    (p_toml / "mondo" / "etc").mkdir(parents=True)
    (p_toml / "mondo" / "etc" / "broken.toml").write_text("[bad\nkey = ", encoding="utf-8")
    with pytest.raises(SafetyValidationError, match="invalid TOML"):
        validate_patch_inputs(p_toml, mondo_deletes=[], hbre_deletes=[])

    # 5. Syntax error in JSON config
    p_json = tmp_path / "p_json"
    (p_json / "mondo" / "etc").mkdir(parents=True)
    (p_json / "mondo" / "etc" / "broken.json").write_text("{bad json", encoding="utf-8")
    with pytest.raises(SafetyValidationError, match="invalid JSON"):
        validate_patch_inputs(p_json, mondo_deletes=[], hbre_deletes=[])

    # 6. Deleting /bin/md_upgrade without replacement
    p_del = tmp_path / "p_del"
    p_del.mkdir(parents=True)
    with pytest.raises(SafetyValidationError, match="critical recovery file"):
        validate_patch_inputs(p_del, mondo_deletes=["/bin/md_upgrade"], hbre_deletes=[])


def test_blocks_bootloader_and_avb_overrides_by_default(tmp_path: Path) -> None:
    base_zip = _build_synthetic_firmware(tmp_path / "base", with_ext4=False)
    for dangerous_file in ("miniboot.img", "vbmeta.img", "boot.img", "system.img", "gpt.conf", "ota_process"):
        pdir = tmp_path / f"bad_{dangerous_file}"
        (pdir / "x5").mkdir(parents=True)
        (pdir / "x5" / dangerous_file).write_bytes(b"TAMPERED" * 32)
        with pytest.raises(SafetyValidationError):
            patch_firmware(base_zip, pdir, tmp_path / "out" / "V26.10.28.05_lz110.zip")


def test_blocks_mcu_override_without_flag_and_validates_cortex_m_vectors(tmp_path: Path) -> None:
    base_zip = _build_synthetic_firmware(tmp_path / "base", with_ext4=False)
    p_mcu = tmp_path / "p_mcu"
    (p_mcu / "modules").mkdir(parents=True)
    bad_esc = p_mcu / "modules" / "0200.bin"
    # Bad SP (0x08000000 instead of 0x20xxxxxx SRAM)
    bad_esc.write_bytes(struct.pack("<II", 0x08000000, 0x0000319D) + b"\x00" * 128)

    with pytest.raises(SafetyValidationError, match="allow-module-override"):
        patch_firmware(base_zip, p_mcu)

    with pytest.raises(SafetyValidationError, match="Cortex-M initial SP"):
        patch_firmware(base_zip, p_mcu, allow_module_override=True)

    # Valid Cortex-M vector table succeeds with --allow-module-override
    bad_esc.write_bytes(struct.pack("<II", 0x20002408, 0x0000319D) + b"\xAB" * 128)
    out_zip = tmp_path / "out" / "V26.10.28.05_lz110.zip"
    res = patch_firmware(base_zip, p_mcu, out_zip, allow_module_override=True)
    assert res.module_overrides == ["0200:0200.bin"]


def test_cli_inspect_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base_zip = _build_synthetic_firmware(tmp_path / "base", with_ext4=False)
    assert cli_main(["inspect", "--base-zip", str(base_zip)]) == 0
    out = capsys.readouterr().out
    assert "26.10.28.04" in out
    assert "0300" in out

    assert cli_main(["verify", str(base_zip)]) == 0
    out = capsys.readouterr().out
    assert "Verified package OK" in out


@requires_e2fsprogs
def test_patch_roundtrip_permissions_order_and_checksums(tmp_path: Path) -> None:
    base_zip = _build_synthetic_firmware(tmp_path / "base", with_ext4=True)

    patch_dir = tmp_path / "my_patch"
    (patch_dir / "mondo" / "bin").mkdir(parents=True)
    (patch_dir / "mondo" / "etc").mkdir(parents=True)
    script = patch_dir / "mondo" / "bin" / "hello.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    os.chmod(script, 0o755)
    (patch_dir / "mondo" / "etc" / "existing.txt").write_text("patched!\n", encoding="utf-8")
    (patch_dir / "mondo" / "etc" / "valid.json").write_text('{"enabled": true}\n', encoding="utf-8")
    (patch_dir / "mondo.delete").write_text("/etc/remove_me.txt\n# comment\n", encoding="utf-8")

    out_zip = tmp_path / "out" / "V26.10.28.05_lz110.zip"
    res = patch_firmware(base_zip, patch_dir, out_zip)

    assert res.package_version == "26.10.28.05"
    assert res.x5_version == "26.10.27.46"
    assert res.mondo_changes["written"] == 3
    assert res.mondo_changes["deleted"] == 1

    report = verify_package(out_zip, base_zip_path=base_zip)
    assert report.verified_modules == 2
    assert report.ota_process_mode == 0o755

    with zipfile.ZipFile(out_zip, "r") as zf:
        x5_bytes = zf.read("lz110_0300_V26.10.27.46_20261028.mass.fw.plain.unsign")
        with zipfile.ZipFile(io.BytesIO(x5_bytes), "r") as x5_zf:
            assert x5_zf.namelist() == [
                "gpt.conf",
                "data.json",
                "miniboot.img",
                "vbmeta.img",
                "hbre.img",
                "mondo.img",
                "ota_process",
            ]
            op_info = x5_zf.getinfo("ota_process")
            assert op_info.create_system == 3
            assert (op_info.external_attr >> 16) & 0o7777 == 0o755

            extracted_mondo = tmp_path / "verify_mondo.img"
            extracted_mondo.write_bytes(x5_zf.read("mondo.img"))
            img = Ext4Image(extracted_mondo)
            img.fsck()
            assert img.read_file("/etc/existing.txt") == b"patched!\n"
            assert img.read_file("/bin/hello.sh") == b"#!/bin/sh\necho hi\n"
            assert not img.path_exists("/etc/remove_me.txt")


@requires_e2fsprogs
def test_ext4_enospc_caught_cleanly_without_silent_delete(tmp_path: Path) -> None:
    img_path = tmp_path / "small.img"
    _create_empty_ext4(img_path, size_mb=2)
    img = Ext4Image(img_path)

    seed = tmp_path / "seed"
    (seed / "bin").mkdir(parents=True)
    (seed / "bin" / "keep_me").write_bytes(_fake_aarch64_elf(4096))
    img.apply_overlay_and_deletes(overlay_dir=seed)

    over = tmp_path / "over"
    (over / "bin").mkdir(parents=True)
    (over / "bin" / "keep_me").write_bytes(_fake_aarch64_elf(3 * 1024 * 1024))

    with pytest.raises(RuntimeError, match="Insufficient space"):
        img.apply_overlay_and_deletes(overlay_dir=over)

    st = img.stat_path("/bin/keep_me")
    assert st is not None
    assert st.size == 4096


def test_verify_package_blocks_upload_on_corrupted_inner_checksum(tmp_path: Path) -> None:
    base_zip = _build_synthetic_firmware(tmp_path / "base", with_ext4=False)

    bad_zip = tmp_path / "V26.10.28.05_lz110.zip"
    with zipfile.ZipFile(base_zip, "r") as src, zipfile.ZipFile(bad_zip, "w") as dst:
        for item in src.infolist():
            raw = src.read(item.filename)
            if item.filename.endswith(".mass.cfg.plain.unsign"):
                text = raw.decode("utf-8").replace("sha256:", "sha256:0000")
                dst.writestr(item.filename, text.encode("utf-8"))
            else:
                dst.writestr(item, raw)

    with pytest.raises(SafetyValidationError, match="SHA-256 mismatch"):
        verify_package(bad_zip)

    # Upload refuses locally BEFORE opening any network socket.
    with pytest.raises(SafetyValidationError, match="SHA-256 mismatch"):
        upload_firmware(bad_zip, host="127.0.0.1", port=1)


def test_streaming_upload_v1_v2_and_auto_fallback_against_mock_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received_v1: dict[str, object] = {}
    received_v2: dict[str, object] = {}
    state = {"v2_installed": False, "reject_v1": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, code: int, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, b'{"status":"ok"}')
                return
            if self.path == "/list":
                files = []
                if "filename" in received_v1 and isinstance(received_v1.get("file_bytes"), bytes):
                    files.append({"name": received_v1["filename"], "size": len(received_v1["file_bytes"])})
                self._send_json(200, json.dumps({"files": files}).encode("utf-8"))
                return
            if self.path == "/v2/upgrades/test-uid-123":
                if self.headers.get("Upload-Token") != "tok-secret-99":
                    self._send_json(403, b'{"error":"forbidden"}')
                    return
                self._send_json(200, json.dumps({"upgradeId": "test-uid-123", "state": "READY"}).encode("utf-8"))
                return
            self._send_json(404, b'{"error":"not_found"}')

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            if self.path == "/upload":
                if state["reject_v1"]:
                    self._send_json(403, b'{"error":"legacy_auth_required"}')
                    return
                ctype = self.headers.get("Content-Type", "")
                msg = BytesParser(policy=email_default).parsebytes(
                    f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
                )
                parts: dict[str, object] = {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name == "file":
                        parts["filename"] = part.get_filename()
                        parts["file_bytes"] = part.get_payload(decode=True)
                    elif isinstance(name, str):
                        payload_bytes = part.get_payload(decode=True)
                        assert isinstance(payload_bytes, bytes)
                        parts[name] = payload_bytes.decode("utf-8")
                received_v1.update(parts)
                self._send_json(200, json.dumps({"ok": True, "filename": parts.get("filename")}).encode("utf-8"))
                return
            if self.path == "/v2/upgrades/test-uid-123/install":
                if self.headers.get("Upload-Token") != "tok-secret-99":
                    self._send_json(403, b'{"error":"forbidden"}')
                    return
                state["v2_installed"] = True
                self._send_json(200, b'{"state":"INSTALLING"}')
                return
            self._send_json(404, b'{"error":"not_found"}')

        def do_PATCH(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b""
            if self.path == "/v2/upgrades/test-uid-123/upload":
                if self.headers.get("Upload-Token") != "tok-secret-99":
                    self._send_json(403, b'{"error":"forbidden"}')
                    return
                received_v2["tus"] = self.headers.get("Tus-Resumable")
                received_v2["offset"] = self.headers.get("Upload-Offset")
                received_v2["upload_len"] = self.headers.get("Upload-Length")
                received_v2["auth"] = self.headers.get("Authorization")
                received_v2["bytes"] = raw
                self._send_json(200, b'{"state":"VERIFYING"}')
                return
            self._send_json(404, b'{"error":"not_found"}')

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status = check_robot_ota(host="127.0.0.1", port=port, bearer_token="test-bearer")
        assert status["scheme"] == "http"
        assert status["health"] == {"status": "ok"}

        pkg = _build_synthetic_firmware(tmp_path / "fw", with_ext4=False)
        payload = pkg.read_bytes()

        # 1. V1 multipart upload + local verify + remote /list size verification
        up1 = upload_firmware(pkg, host="127.0.0.1", port=port, mode="v1", bearer_token="test-bearer", chunk_size=16384)
        assert up1.status_code == 200
        assert up1.remote_verified
        assert received_v1["filename"] == "V26.10.28.04_lz110.zip"
        assert received_v1["md5"] == hashlib.md5(payload).hexdigest()
        assert received_v1["size"] == str(len(payload))
        assert received_v1["file_bytes"] == payload

        # 2. V2 TUS PATCH upload + Upload-Token + poll READY + HTTP install trigger
        up2 = upload_firmware(
            pkg,
            host="127.0.0.1",
            port=port,
            mode="v2",
            upgrade_id="test-uid-123",
            upload_token="tok-secret-99",
            bearer_token="test-bearer",
            chunk_size=16384,
        )
        assert up2.status_code == 200
        assert up2.mode == "v2"
        assert up2.remote_verified
        assert received_v2["tus"] == "1.0.0"
        assert received_v2["offset"] == "0"
        assert received_v2["upload_len"] == str(len(payload))
        assert received_v2["auth"] == "Bearer test-bearer"
        assert received_v2["bytes"] == payload

        inst = trigger_v2_http_install(
            host="127.0.0.1",
            port=port,
            scheme="http",
            upgrade_id="test-uid-123",
            upload_token="tok-secret-99",
            bearer_token="test-bearer",
        )
        assert state["v2_installed"]
        assert inst["state"] == "INSTALLING"

        # 3. `mode="auto"` falls back cleanly from V1 403 -> V2 PATCH forwarding bearer_token & device_id
        state["reject_v1"] = True
        received_v2.clear()
        monkeypatch.setattr("nebi_patch.uploader._mint_v2_session", lambda **_kw: ("test-uid-123", "tok-secret-99"))
        up_auto = upload_firmware(
            pkg,
            host="127.0.0.1",
            port=port,
            mode="auto",
            device_id="TEST-SN-001",
            bearer_token="auto-bearer",
            chunk_size=16384,
        )
        assert up_auto.mode == "v2"
        assert up_auto.remote_verified
        assert received_v2["auth"] == "Bearer auto-bearer"
    finally:
        server.shutdown()
        server.server_close()
