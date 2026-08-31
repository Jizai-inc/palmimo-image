#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Flash the official Palmimo .img onto an SD card and inject the identity
file (identity file spec v2). Manufacturing / dev tool.

Contract (see doc/design.md, "Phase 3 への接続"): this CLI
does exactly two things to the SD card — flash the official, unmodified
.img (or .img.xz), then write ``palmimo-identity.json`` to the boot (FAT)
partition. It touches NOTHING else; all individualization happens in
``palmimo-firstboot.service`` on first boot. Runs on macOS (the primary,
supported platform — the boot partition is FAT so macOS can write it
without extra tooling); Linux is best-effort only.

Usage:
    uv run tools/provision_sd.py --image palmimo-1.2.0.img.xz --device-id 405
    uv run tools/provision_sd.py --image palmimo-1.2.0.img.xz --device-id 405 \\
        --password s3cr3tSticker1
    uv run tools/provision_sd.py --image palmimo-1.2.0.img.xz --identity ./palmimo-identity.json
    uv run tools/provision_sd.py --image palmimo-1.2.0.img.xz --device-id 405 --device /dev/disk4
    uv run tools/provision_sd.py --image palmimo-1.2.0.img.xz --device-id 405 --dry-run

Safety model: nothing writes to a disk before the operator has both (a)
picked it from a list of external, non-internal, non-mounted-system
candidates (or named it explicitly with --device) and (b) typed the exact
disk identifier (e.g. "disk4") back as confirmation — a bare y/N is not
accepted. Any failing step aborts loudly; a partially-flashed card is
invalid and the message says so instead of silently continuing.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import lzma
import os
import plistlib
import re
import secrets
import shutil
import string
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Identity file spec v2 — kept textually identical to the patterns in
# tools/make_identity.py and
# files/usr/local/lib/palmimo/firstboot.sh. All three copies are
# pinned equal by tests/test_image_contracts.py and
# tests/test_provision_sd.py — do not edit one without the others.
# ---------------------------------------------------------------------------
DEVICE_ID_PATTERN = r"^[a-z0-9-]{1,32}$"
DEVICE_ID_REGEX = re.compile(DEVICE_ID_PATTERN)

PASSWORD_PATTERN = r"^[A-Za-z0-9]{8,63}$"
PASSWORD_REGEX = re.compile(PASSWORD_PATTERN)

IDENTITY_FILENAME = "palmimo-identity.json"

# Generated passwords: alphanumeric only (same alphabet PASSWORD_REGEX
# requires), long enough to be well clear of the 8-char floor.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
GENERATED_PASSWORD_LENGTH = 20

# Files/dirs whose presence on the target FAT volume identifies it as a
# Raspberry Pi boot partition (not, say, an unrelated USB stick the operator
# picked by mistake). Any one is enough.
PI_BOOT_MARKERS = ("config.txt", "cmdline.txt", "bootcode.bin", "start4.elf")

FLASH_BLOCK_SIZE = 4 * 1024 * 1024  # 4 MiB, matches dd bs=4m
FLASH_PROGRESS_INTERVAL = 256 * 1024 * 1024  # print progress every 256 MiB


class ProvisionError(Exception):
    """A user-facing, fatal provisioning error. Caught in main() and
    printed without a traceback."""


class ImageTooLargeError(Exception):
    """Raised by flash_image when writing the next chunk would exceed
    target_size_bytes. Caught in main() and turned into a ProvisionError
    with the same "card is now invalid, rerun" message as any other
    mid-flash failure."""


# ---------------------------------------------------------------------------
# Identity: validation, generation, rendering
# ---------------------------------------------------------------------------


def validate_device_id(device_id: str) -> None:
    if not DEVICE_ID_REGEX.match(device_id):
        raise ProvisionError(f"--device-id {device_id!r} does not match {DEVICE_ID_PATTERN!r}")


def validate_password(password: str) -> None:
    if not PASSWORD_REGEX.match(password):
        raise ProvisionError(
            f"--password does not match {PASSWORD_PATTERN!r} "
            "(alphanumeric, 8-63 chars — the manufacturing sticker alphabet)"
        )


def generate_password(length: int = GENERATED_PASSWORD_LENGTH) -> str:
    if length < 8:
        raise ValueError("generated password must be at least 8 chars to satisfy PASSWORD_REGEX")
    password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
    # Belt-and-suspenders: the alphabet/length already guarantee this, but a
    # generated value that failed the regex would be a silent contract
    # break, so assert loudly instead.
    assert PASSWORD_REGEX.match(password), f"generated password failed its own regex: {password!r}"
    return password


def render_identity_json(device_id: str, password: str) -> str:
    """Byte-identical shape to make_identity.py's output: a 2-space-indented
    JSON object with device_id and initial_password, plus a trailing
    newline."""
    identity = {"device_id": device_id, "initial_password": password}
    return json.dumps(identity, indent=2) + "\n"


@dataclasses.dataclass(frozen=True)
class Identity:
    device_id: str
    password: str
    generated: bool  # True if the password was generated here (needs printing for the sticker)


def load_identity_file(path: Path) -> Identity:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisionError(f"--identity {path}: could not read/parse as JSON ({exc})") from exc
    device_id = data.get("device_id")
    password = data.get("initial_password")
    if not isinstance(device_id, str) or not isinstance(password, str):
        raise ProvisionError(
            f"--identity {path}: expected a JSON object with string 'device_id' and 'initial_password'"
        )
    validate_device_id(device_id)
    validate_password(password)
    return Identity(device_id=device_id, password=password, generated=False)


def resolve_identity(args: argparse.Namespace) -> Identity:
    """Build the Identity to inject from parsed args. Pure aside from
    reading --identity off disk and (maybe) generating a password."""
    if args.identity is not None:
        return load_identity_file(args.identity)
    validate_device_id(args.device_id)
    if args.password is not None:
        validate_password(args.password)
        return Identity(device_id=args.device_id, password=args.password, generated=False)
    return Identity(device_id=args.device_id, password=generate_password(), generated=True)


# ---------------------------------------------------------------------------
# Image format detection
# ---------------------------------------------------------------------------

_XZ_MAGIC = b"\xfd7zXZ\x00"


def detect_image_format(image_path: Path) -> str:
    """Return "xz" or "raw". Raises ProvisionError for a missing file or an
    unrecognized image."""
    if not image_path.is_file():
        raise ProvisionError(f"--image {image_path}: not a file")
    name = image_path.name
    if name.endswith(".img.xz") or name.endswith(".xz"):
        return "xz"
    if name.endswith(".img"):
        return "raw"
    # Extension didn't tell us — sniff the magic bytes rather than refuse.
    with image_path.open("rb") as f:
        head = f.read(len(_XZ_MAGIC))
    if head == _XZ_MAGIC:
        return "xz"
    raise ProvisionError(
        f"--image {image_path}: unrecognized image (expected a .img or .img.xz filename, "
        "and the content isn't xz-compressed either)"
    )


# ---------------------------------------------------------------------------
# Disk discovery (macOS diskutil) and safety filtering
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PartitionInfo:
    identifier: str
    content: str
    volume_name: str
    mount_point: str


@dataclasses.dataclass(frozen=True)
class DiskCandidate:
    identifier: str  # e.g. "disk4"
    size_bytes: int
    content: str
    partitions: tuple[PartitionInfo, ...]

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1_000_000_000


def parse_disk_list_plist(plist_bytes: bytes) -> list[DiskCandidate]:
    """Parse the output of `diskutil list -plist external physical` (or any
    `diskutil list -plist` invocation) into whole-disk candidates."""
    data = plistlib.loads(plist_bytes)
    whole_disks = set(data.get("WholeDisks", []))
    candidates: list[DiskCandidate] = []
    for entry in data.get("AllDisksAndPartitions", []):
        identifier = entry.get("DeviceIdentifier")
        if identifier is None or identifier not in whole_disks:
            continue
        partitions = tuple(
            PartitionInfo(
                identifier=p.get("DeviceIdentifier", ""),
                content=p.get("Content", ""),
                volume_name=p.get("VolumeName", ""),
                mount_point=p.get("MountPoint", "") or "",
            )
            for p in entry.get("Partitions", [])
        )
        candidates.append(
            DiskCandidate(
                identifier=identifier,
                size_bytes=int(entry.get("Size", 0) or 0),
                content=entry.get("Content", ""),
                partitions=partitions,
            )
        )
    return candidates


_UNSAFE_MOUNT_PREFIXES = ("/", "/System", "/private/var", "/Library")


def _has_mounted_system_partition(candidate: DiskCandidate) -> bool:
    for p in candidate.partitions:
        mp = p.mount_point
        if mp in ("/",):
            return True
        if any(mp == prefix or mp.startswith(prefix + "/") for prefix in _UNSAFE_MOUNT_PREFIXES):
            return True
    return False


def is_disk_safe_to_offer(candidate: DiskCandidate, info: dict) -> tuple[bool, str]:
    """Second, code-level safety check beyond `diskutil list external
    physical` itself (which already excludes internal/synthesized disks at
    the OS level). Returns (safe, reason_if_unsafe)."""
    if bool(info.get("Internal", True)):
        # One carve-out: a Mac's built-in SDXC slot reports Internal, but an
        # SD card in it is exactly what this tool exists to write. Only the
        # Secure Digital bus with removable media qualifies -- every other
        # internal disk stays refused.
        is_builtin_sd = info.get("BusProtocol") == "Secure Digital" and bool(info.get("RemovableMedia", False))
        if not is_builtin_sd:
            return False, "internal disk (not an SD card in a built-in reader)"
    if bool(info.get("VirtualOrPhysical", "Virtual") == "Virtual"):
        return False, "reported Virtual (synthesized)"
    if not info.get("RemovableMedia", False) and not info.get("Ejectable", False):
        return False, "neither RemovableMedia nor Ejectable"
    if _has_mounted_system_partition(candidate):
        return False, "has a partition mounted under a system path"
    return True, ""


def filter_safe_candidates(candidates: list[DiskCandidate], info_by_id: dict[str, dict]) -> list[DiskCandidate]:
    """Keep only candidates diskutil info also confirms are safe. A
    candidate with no info available is excluded conservatively rather than
    offered."""
    safe = []
    for c in candidates:
        info = info_by_id.get(c.identifier)
        if info is None:
            continue
        ok, _reason = is_disk_safe_to_offer(c, info)
        if ok:
            safe.append(c)
    return safe


def format_disk_candidate(c: DiskCandidate) -> str:
    vol_names = ", ".join(p.volume_name for p in c.partitions if p.volume_name) or "(no volumes)"
    return f"/dev/{c.identifier}  {c.size_gb:.1f} GB  {c.content}  [{vol_names}]"


def confirmation_matches(user_input: str, identifier: str) -> bool:
    """The operator must type the disk identifier back exactly (e.g.
    "disk4") — a bare y/N is not accepted. Surrounding whitespace and an
    optional /dev/ prefix are tolerated; the match is case-sensitive."""
    typed = user_input.strip()
    if typed.startswith("/dev/"):
        typed = typed[len("/dev/") :]
    return typed == identifier


# ---------------------------------------------------------------------------
# Disk fingerprint — best-effort identity binding across the destructive
# sequence (confirm -> flash -> inject).
#
# macOS reuses "diskN" identifiers on unplug/replug: the identifier the
# operator confirmed can end up pointing at a *different* physical card by
# the time flashing starts, or the card can be swapped again between
# flashing and mounting the boot partition to write the identity file. A
# `diskN` string alone is not a stable enough handle for a destructive
# sequence spanning several `diskutil` calls and a human confirmation
# prompt, so each destructive step re-fetches `diskutil info` and checks it
# against the fingerprint captured at confirmation time.
#
# This is explicitly *not* cryptographic and not foolproof: TotalSize alone
# is worthless (two cards of the same model are identical in size), and
# most raw SD-card readers don't report a DiskUUID at the whole-disk level
# at all (that's usually a per-volume property, absent until the card is
# formatted/mounted). Comparing MediaName + TotalSize together already
# catches the common failure (operator swaps in a visibly different card or
# reader), and the UUID is compared too whenever both sides happen to have
# one, tightening the check without requiring it.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DiskFingerprint:
    media_name: str
    total_size: int
    disk_uuid: str  # "" when diskutil doesn't report one for this disk


def fingerprint_from_info(info: dict) -> DiskFingerprint:
    return DiskFingerprint(
        media_name=str(info.get("MediaName") or ""),
        total_size=int(info.get("TotalSize") or 0),
        disk_uuid=str(info.get("DiskUUID") or info.get("VolumeUUID") or ""),
    )


def fingerprint_matches(before: DiskFingerprint, after: DiskFingerprint) -> bool:
    """Best-effort identity binding, not a cryptographic guarantee: see the
    module comment above this class. MediaName and TotalSize must always
    agree; DiskUUID is compared only when both sides report one (a disk
    that never reports a UUID still gets the MediaName+TotalSize check)."""
    if before.total_size != after.total_size:
        return False
    if before.media_name != after.media_name:
        return False
    if before.disk_uuid and after.disk_uuid:
        return before.disk_uuid == after.disk_uuid
    return True


# ---------------------------------------------------------------------------
# Flashing
# ---------------------------------------------------------------------------


def check_image_fits_target(image_size_bytes: int, target_size_bytes: int) -> None:
    """Pre-flight refusal, before any byte is written. A target size of 0
    (unknown/not reported) skips the check rather than false-refusing."""
    if target_size_bytes and image_size_bytes > target_size_bytes:
        raise ProvisionError(
            f"image is {image_size_bytes} bytes, target disk is only {target_size_bytes} bytes "
            "— refusing to flash an oversized image"
        )


def get_xz_uncompressed_size(image_path: Path) -> int | None:
    """Best-effort: ask the `xz` binary for the archive's uncompressed
    size via `xz --robot --list`. Returns None (meaning "skip the
    pre-check") whenever the binary is missing or output can't be parsed —
    the streaming guard in flash_image is the backstop either way."""
    xz_bin = shutil.which("xz")
    if xz_bin is None:
        return None
    try:
        result = subprocess.run(
            [xz_bin, "--robot", "--list", str(image_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return parse_xz_robot_list_uncompressed_size(result.stdout)


def parse_xz_robot_list_uncompressed_size(output: str) -> int | None:
    """Parse the tab-separated `xz --robot --list` output. The "totals"
    line's 5th field (0-indexed: 4) is the uncompressed size in bytes."""
    for line in output.splitlines():
        fields = line.split("\t")
        if fields and fields[0] == "totals":
            try:
                return int(fields[4])
            except (IndexError, ValueError):
                return None
    return None


def flash_image(
    image_path: Path,
    image_format: str,
    target_path: Path,
    *,
    progress: Callable[[int], None] | None = None,
    target_size_bytes: int | None = None,
) -> int:
    """Stream image_path onto target_path (a raw device node, or — in
    tests — a plain file) in FLASH_BLOCK_SIZE chunks, decompressing on the
    fly if image_format == "xz". Returns the number of bytes written.
    `progress`, if given, is called with the running byte count roughly
    every FLASH_PROGRESS_INTERVAL bytes. `target_size_bytes`, if given, is
    a streaming capacity guard: raises ImageTooLargeError before writing
    any chunk that would exceed it (the backstop for when the pre-flight
    check in check_image_fits_target couldn't determine the image size
    up front, e.g. an .img.xz and no `xz` binary available)."""
    written = 0
    next_report = FLASH_PROGRESS_INTERVAL
    opener = lzma.open if image_format == "xz" else open
    with opener(image_path, "rb") as src, open(target_path, "wb") as dst:
        while True:
            chunk = src.read(FLASH_BLOCK_SIZE)
            if not chunk:
                break
            if target_size_bytes is not None and written + len(chunk) > target_size_bytes:
                raise ImageTooLargeError(
                    f"image exceeds target capacity ({target_size_bytes} bytes) after {written} bytes written"
                )
            dst.write(chunk)
            written += len(chunk)
            if progress is not None and written >= next_report:
                progress(written)
                next_report += FLASH_PROGRESS_INTERVAL
        dst.flush()
        os.fsync(dst.fileno())
    if progress is not None:
        progress(written)
    return written


# ---------------------------------------------------------------------------
# diskutil wrappers (real I/O — not unit-tested; --dry-run never calls these)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, **kwargs)


def list_candidate_disks() -> list[DiskCandidate]:
    """External physical disks, plus internal physical ones so a built-in
    SDXC reader is discoverable -- is_disk_safe_to_offer() is what separates
    an SD card in that slot from a real internal disk."""
    candidates: list[DiskCandidate] = []
    seen: set[str] = set()
    for location in ("external", "internal"):
        result = _run(["diskutil", "list", "-plist", location, "physical"])
        if result.returncode != 0:
            raise ProvisionError(f"`diskutil list {location}` failed: {result.stderr.strip()}")
        raw = result.stdout.encode("utf-8") if isinstance(result.stdout, str) else result.stdout
        for candidate in parse_disk_list_plist(raw):
            if candidate.identifier not in seen:
                seen.add(candidate.identifier)
                candidates.append(candidate)
    return candidates


def disk_info(identifier: str) -> dict:
    result = _run(["diskutil", "info", "-plist", identifier])
    if result.returncode != 0:
        raise ProvisionError(f"`diskutil info {identifier}` failed: {result.stderr.strip()}")
    raw = result.stdout.encode("utf-8") if isinstance(result.stdout, str) else result.stdout
    return plistlib.loads(raw)


def unmount_disk(identifier: str) -> None:
    result = _run(["diskutil", "unmountDisk", f"/dev/{identifier}"])
    if result.returncode != 0:
        raise ProvisionError(f"`diskutil unmountDisk /dev/{identifier}` failed: {result.stderr.strip()}")


def eject_disk(identifier: str) -> None:
    result = _run(["diskutil", "eject", f"/dev/{identifier}"])
    if result.returncode != 0:
        raise ProvisionError(f"`diskutil eject /dev/{identifier}` failed: {result.stderr.strip()}")


def mount_partition(partition_identifier: str) -> Path:
    result = _run(["diskutil", "mount", f"/dev/{partition_identifier}"])
    if result.returncode != 0:
        raise ProvisionError(f"`diskutil mount /dev/{partition_identifier}` failed: {result.stderr.strip()}")
    info = disk_info(partition_identifier)
    mount_point = info.get("MountPoint")
    if not mount_point:
        raise ProvisionError(f"/dev/{partition_identifier} mounted but reports no MountPoint")
    return Path(mount_point)


def is_pi_boot_partition(mount_point: Path) -> bool:
    return any((mount_point / marker).exists() for marker in PI_BOOT_MARKERS)


def find_boot_partition(candidate: DiskCandidate) -> PartitionInfo:
    """The boot (FAT) partition is conventionally the first partition of a
    freshly-flashed Pi image (s1). Prefer a FAT-content partition if one is
    identifiable; fall back to s1."""
    for p in candidate.partitions:
        if "fat" in p.content.lower() or "dos" in p.content.lower() or "efi" in p.content.lower():
            return p
    if candidate.partitions:
        return candidate.partitions[0]
    raise ProvisionError(f"/dev/{candidate.identifier} reports no partitions after flashing")


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision_sd.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--image",
        required=True,
        type=Path,
        help="Path to the official Palmimo image (.img or .img.xz).",
    )
    identity_group = parser.add_argument_group("identity (choose one)")
    identity_group.add_argument(
        "--device-id",
        help=f"Device id, must match {DEVICE_ID_PATTERN!r}. Mutually exclusive with --identity.",
    )
    identity_group.add_argument(
        "--password",
        help=(
            f"Plaintext initial password (sticker), must match {PASSWORD_PATTERN!r}. "
            "Omit to generate one (printed at the end for the sticker). Requires --device-id."
        ),
    )
    identity_group.add_argument(
        "--identity",
        type=Path,
        help="Path to a pre-made palmimo-identity.json to use as-is, instead of --device-id/--password.",
    )
    parser.add_argument(
        "--device",
        help="Target disk, e.g. /dev/disk4 or disk4. Omit to pick interactively from external disks.",
    )
    parser.add_argument(
        "--identity-out",
        type=Path,
        help="Also write a copy of the generated palmimo-identity.json to this directory (for records).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except unmount/dd/mount/write/eject; print the plan instead.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.identity is not None:
        if args.device_id is not None or args.password is not None:
            raise ProvisionError("--identity is mutually exclusive with --device-id/--password")
        return
    if args.device_id is None:
        raise ProvisionError("one of --device-id or --identity is required")
    if args.password is not None and args.device_id is None:
        raise ProvisionError("--password requires --device-id")


# ---------------------------------------------------------------------------
# Dry-run plan (pure — used both by --dry-run and by tests)
# ---------------------------------------------------------------------------


def build_plan_text(
    *,
    image_path: Path,
    image_format: str,
    identity: Identity,
    target: str | None,
) -> str:
    lines = [
        "provision_sd.py plan (--dry-run — nothing will be written):",
        f"  image:        {image_path} ({image_format})",
        f"  device_id:    {identity.device_id}",
        f"  password:     {'<generated, see summary>' if identity.generated else '<provided>'}",
        f"  target disk:  {target or '<not yet chosen — would prompt interactively>'}",
        "  steps that would run: unmountDisk -> flash -> sync -> mount boot partition ->",
        f"                        verify Pi boot markers -> write {IDENTITY_FILENAME} -> eject",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _choose_disk_interactively() -> DiskCandidate:
    candidates = list_candidate_disks()
    info_by_id = {c.identifier: disk_info(c.identifier) for c in candidates}
    safe = filter_safe_candidates(candidates, info_by_id)
    if not safe:
        raise ProvisionError(
            "no safe external removable disks found. Plug in the SD card reader, or pass --device explicitly."
        )
    print("Candidate disks (external, removable, not internal, no system mount):")
    for c in safe:
        print(f"  {format_disk_candidate(c)}")
    chosen_id = input("Type the disk identifier to flash (e.g. 'disk4'), or Ctrl-C to abort: ")
    match = next((c for c in safe if confirmation_matches(chosen_id, c.identifier)), None)
    if match is None:
        raise ProvisionError(f"{chosen_id!r} does not match any listed candidate disk — aborting, nothing written")
    return match


def _resolve_target_disk(device_arg: str | None) -> DiskCandidate:
    if device_arg is None:
        return _choose_disk_interactively()
    identifier = device_arg[len("/dev/") :] if device_arg.startswith("/dev/") else device_arg
    candidates = list_candidate_disks()
    match = next((c for c in candidates if c.identifier == identifier), None)
    if match is None:
        # Not in the "external physical" candidate list at all — refuse
        # rather than guess at an internal/unknown disk.
        raise ProvisionError(
            f"--device {device_arg!r} is not a physical disk diskutil will offer "
            "(refusing synthesized/unknown disks, and internal disks other "
            "than an SD card in a built-in reader, by design)"
        )
    info = disk_info(identifier)
    ok, reason = is_disk_safe_to_offer(match, info)
    if not ok:
        raise ProvisionError(f"--device {device_arg!r} refused: {reason}")
    return match


def _confirm_target(candidate: DiskCandidate) -> None:
    print("About to ERASE and flash:")
    print(f"  {format_disk_candidate(candidate)}")
    typed = input(f"Type '{candidate.identifier}' to confirm (anything else aborts): ")
    if not confirmation_matches(typed, candidate.identifier):
        raise ProvisionError("confirmation did not match — aborting, nothing written")


def require_root() -> None:
    """Writing /dev/rdiskN needs root on macOS; fail before any prompt or
    unmount rather than partway into the destructive sequence."""
    if os.geteuid() != 0:
        raise ProvisionError(
            "flashing needs root (writing /dev/rdiskN): re-run as\n"
            "  sudo $(which uv) run tools/provision_sd.py ...\n"
            "(--dry-run works without root)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        validate_args(args)
        image_format = detect_image_format(args.image)
        identity = resolve_identity(args)

        if args.dry_run:
            print(
                build_plan_text(image_path=args.image, image_format=image_format, identity=identity, target=args.device)
            )
            if identity.generated:
                print(f"\n(generated password for the sticker: {identity.password})")
            return 0

        require_root()
        candidate = _resolve_target_disk(args.device)
        _confirm_target(candidate)

        # Fingerprint the confirmed disk now; every later destructive step
        # re-checks against this before touching the disk (see the
        # DiskFingerprint module comment — macOS reuses diskN identifiers).
        confirmed_fingerprint = fingerprint_from_info(disk_info(candidate.identifier))

        if image_format == "raw":
            check_image_fits_target(args.image.stat().st_size, candidate.size_bytes)
        else:
            uncompressed_size = get_xz_uncompressed_size(args.image)
            if uncompressed_size is not None:
                check_image_fits_target(uncompressed_size, candidate.size_bytes)

        print(f"Unmounting /dev/{candidate.identifier} ...")
        unmount_disk(candidate.identifier)

        pre_flash_fingerprint = fingerprint_from_info(disk_info(candidate.identifier))
        if not fingerprint_matches(confirmed_fingerprint, pre_flash_fingerprint):
            raise ProvisionError(
                f"/dev/{candidate.identifier} no longer matches the disk confirmed a moment ago "
                "(fingerprint changed — the card may have been swapped). Aborting, nothing written."
            )

        target_rdisk = Path(f"/dev/r{candidate.identifier}")
        print(f"Flashing {args.image} ({image_format}) -> {target_rdisk} ...")

        def _progress(written: int) -> None:
            print(f"  ... {written / 1_000_000_000:.2f} GB written", flush=True)

        try:
            flash_image(
                args.image,
                image_format,
                target_rdisk,
                progress=_progress,
                target_size_bytes=candidate.size_bytes,
            )
        except PermissionError as exc:
            raise ProvisionError(
                f"opening {target_rdisk} was refused before anything was written ({exc}); "
                "the card is untouched. Re-run with sudo."
            ) from exc
        except (OSError, lzma.LZMAError, EOFError, ImageTooLargeError) as exc:
            raise ProvisionError(
                f"flashing failed partway through ({exc}). The SD card is now INVALID — "
                "do not ship it. Re-run provision_sd.py from the start on a fresh/re-picked card."
            ) from exc

        print("Flushing writes (sync) ...")
        subprocess.run(["sync"], check=False)

        # Re-read the disk layout post-flash: the image just wrote its own
        # partition table, so the pre-flash `candidate` partitions are stale.
        time.sleep(2)
        refreshed = [c for c in list_candidate_disks() if c.identifier == candidate.identifier]
        if not refreshed:
            raise ProvisionError(
                f"/dev/{candidate.identifier} no longer appears after flashing — the SD card is in an "
                "unknown state. Do not ship it; re-run from the start."
            )

        post_flash_fingerprint = fingerprint_from_info(disk_info(candidate.identifier))
        if not fingerprint_matches(confirmed_fingerprint, post_flash_fingerprint):
            raise ProvisionError(
                f"/dev/{candidate.identifier} no longer matches the disk that was flashed "
                "(fingerprint changed — the card may have been swapped). Refusing to write the "
                "identity file to a different disk. The just-flashed card needs to be verified by "
                "hand before reuse."
            )
        boot_partition = find_boot_partition(refreshed[0])

        print(f"Mounting boot partition /dev/{boot_partition.identifier} ...")
        mount_point = mount_partition(boot_partition.identifier)
        if not is_pi_boot_partition(mount_point):
            raise ProvisionError(
                f"{mount_point} does not look like a Raspberry Pi boot partition "
                f"(none of {PI_BOOT_MARKERS} present) — aborting before writing the identity file. "
                "The SD card was flashed but is NOT individualized; do not ship it as-is."
            )

        identity_path = mount_point / IDENTITY_FILENAME
        print(f"Writing {identity_path} ...")
        fd = os.open(identity_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(render_identity_json(identity.device_id, identity.password))
        os.chmod(identity_path, 0o600)

        if args.identity_out is not None:
            args.identity_out.mkdir(parents=True, exist_ok=True)
            record_path = args.identity_out / IDENTITY_FILENAME
            record_path.write_text(render_identity_json(identity.device_id, identity.password), encoding="utf-8")
            os.chmod(record_path, 0o600)

        print(f"Ejecting /dev/{candidate.identifier} ...")
        eject_disk(candidate.identifier)

        print()
        print("=" * 60)
        print("Provisioning complete.")
        print(f"  device_id: {identity.device_id}")
        if identity.generated:
            print(f"  password:  {identity.password}   <-- transcribe to the sticker")
        else:
            print("  password:  <provided, not re-printed>")
        print(f"  image:     {args.image}")
        print(f"  disk:      /dev/{candidate.identifier}")
        print("=" * 60)
        return 0
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted, nothing further written", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
