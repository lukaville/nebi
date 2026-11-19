# `nebi-patch` — Hardened Beni (LZ110/LZ100) Firmware Patcher, Verifier & Uploader

> [!WARNING]
> **Unofficial project, not affiliated with the robot's manufacturer.** Flashing modified firmware can brick the device
> or void its warranty. Use it only with a robot you own. Provided as is, without warranty ([Apache-2.0](LICENSE),
> see [NOTICE](NOTICE)).

Tooling to safely patch compatible Beni (LZ110/LZ100) OTA firmware archives from a patch directory, enforce
multi-layer anti-brick safety gates, recompute all inner/outer cryptographic digests, and stream-upload the verified
OTA package to the robot.

## Anti-Brick Safety Hardening

Because `S99ota_update_check.sh` runs `ota_tool -b` immediately at Linux boot (committing the new X5 A/B slot before
`/md` daemons prove health), a broken `/md` userspace can soft-brick Wi-Fi SoftAP, UCP, and OTA recovery.
`nebi-patch` enforces six layers of protection:

1. **Bootloader, AVB & Single-Slot MCU Locks**:
   - Blocks `x5/` overrides of `miniboot.img`, `uboot.img`, `vbmeta.img`, `boot.img`, `system.img`, `gpt.conf`, and
     `ota_process` unless `--allow-unsafe-x5-override` is explicitly passed.
   - Blocks `modules/` overrides of single-slot peripheral MCUs (`ESC`, `PMU`, `BAT`, `RC`) and `A350` unless
     `--allow-module-override` is passed, and validates ARM Cortex-M vector tables (`0x20xxxxxx` SRAM SP + odd Thumb
     `Reset_Handler`) when enabled.
2. **Overlay Binary, Script & Config Validation**:
   - Rejects macOS Mach-O, Windows PE, or non-`aarch64` ELF (`e_machine != 183`) binaries.
   - Rejects `#!` scripts containing CRLF (`\r\n`) line endings (which break Linux `execve` with `ENOENT`).
   - Parses and validates all `.json` and `.toml` config files in the overlay before touching any image.
   - Blocks deleting critical recovery daemons (`/bin/md_sys`, `/bin/md_upgrade`, `/bin/md_videotrans`,
     `/bin/md_netmanager`, `/bin/md_relay`, `/etc/md_upgrade_config.LZ110.toml`, `/bin/ota_tool`).
3. **Hardened `ext4` Mutation (`e2fsprogs`)**:
   - Runs `e2fsck -fn` before and after modifying `mondo.img` / `hbre.img`.
   - Checks free blocks and inodes via `dumpe2fs -h` upfront, enforcing a 3% / 32 MiB free-space reserve on
     `mondo.img` so runtime logs never hit `ENOSPC`.
   - Uses C-octal (`0100755` / `0100644`) inode modes and parses `debugfs` `stderr` strictly (since `debugfs` exits `0`
     even on errors).
   - Reads back and verifies exact byte size, SHA-256, and Unix permission bits of every written file inside the ext4
     image, plus confirms critical daemons remain intact and executable.
4. **Lossless Streaming `0300` Repack**:
   - Streams untouched X5 partitions directly from the base `0300` ZIP without `extractall()`, preserving vendor entry
     order, `create_system = 3` (Unix), and `0100755` permissions on `ota_process`.
5. **Full Pre-Publish & Pre-Upload Package Audit (`verify`)**:
   - Verifies outer ZIP CRCs, every module's `size` and `sha256:` against `mass.cfg`, inner `0300` `ota_process`
     `0755` permissions and ELF header, `data.json` `md5sum` and `md5_scope` for every X5 image, and untouched
     bootloader CRCs before atomic output publish **and** before uploading a single byte.
6. **Live Robot Pre-Flight Gates** (requires `nebi-patch[nebi]`):
   - Checks battery SoC (`>= 30%` unless charging), active OTA state (refuses if `INSTALLING` / `AWAITING_REBOOT`),
     and robot posture (refuses `--trigger` if standing/moving unless `--ignore-posture`).

## Requirements

- Python 3.10+ (standard library only on 3.11+, `tomli` on 3.10; install `nebi-patch[nebi]` for Zenoh V2
  `Upload-Token` minting, pre-flight checks, and `--trigger`)
- `e2fsprogs` (`brew install e2fsprogs` on macOS, `apt install e2fsprogs` on Debian/Ubuntu)

## Authentication Token (`:8085`)

`nebi-patch` does not ship any vendor access token. If your robot's `:8085` OTA HTTP server requires a Bearer token,
supply it via one of (highest priority first):

1. `--bearer-token <TOKEN>`
2. `$NEBI_HTTP_BEARER_TOKEN`
3. `$PYNEBI_MEDIA_TOKEN`
4. `~/.config/pynebi/media_token`

## Patch Directory Layout

```text
my_patch/
├── patch.toml        # Optional: [package] version/x5_version & [modes] octal overrides
├── mondo/            # Files overlaid onto mondo.img (/md on robot)
│   ├── bin/my_tool   # Validated: must be Linux aarch64 ELF or LF #! script (auto 0755)
│   └── etc/my.json   # Validated: JSON/TOML syntax checked before pack (auto 0644)
├── mondo.delete      # Optional: newline-separated paths to delete from mondo.img
├── hbre/             # Optional: files overlaid onto hbre.img (/usr/hobot on robot)
├── hbre.delete       # Optional: paths to delete from hbre.img
├── x5/               # Guarded: requires --allow-unsafe-x5-override for boot/AVB files
└── modules/          # Guarded: requires --allow-module-override (Cortex-M vector checked)
```

## CLI Usage

```bash
# 1. Inspect base firmware manifest & modules
nebi-patch inspect --base-zip /path/to/V26.10.28.04_lz110.zip

# 2. Verify all SHA-256 / MD5 checksums, permissions, and AVB/bootloader invariants
nebi-patch verify /path/to/V26.10.28.04_lz110.zip

# 3. Patch & verify locally (no robot connection required)
nebi-patch patch nebi-patch/patches/example_banner \
  --base-zip /path/to/V26.10.28.04_lz110.zip \
  -o /tmp/V26.10.28.05_lz110.zip

# 4. Patch, verify, and upload to robot (:8085 on SoftAP 192.168.7.1 or USB NCM 192.168.123.1)
nebi-patch apply nebi-patch/patches/example_banner \
  --base-zip /path/to/V26.10.28.04_lz110.zip \
  --host 192.168.7.1

# 5. Upload an existing verified ZIP and trigger installation (after battery & posture checks)
nebi-patch upload /tmp/V26.10.28.05_lz110.zip \
  --host 192.168.7.1 --mode auto --trigger

# 6. Check robot md_upgrade (:8085) health & stored packages
nebi-patch status --host 192.168.7.1
```
