"""Contracts for tools/provision_sd.py -- the SD flash + identity
injection CLI (Phase 3 part B).

See doc/design.md ("Phase 3 への接続"): the CLI's contract is
"flash the official .img, then write palmimo-identity.json to the boot FAT
partition — touch NOTHING else". This module pins the parts a static check
can verify: regex agreement with tools/make_identity.py (so the two cannot
drift), image-format detection, disk-candidate safety filtering from
recorded `diskutil` plist fixtures, the confirmation-string check, identity
JSON rendering, and --dry-run's plan output. Anything that actually calls
`diskutil`/`dd` is out of scope here by design -- --dry-run never calls those,
which is exactly what lets this module test it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PROVISION_SCRIPT = REPO_ROOT / "tools" / "provision_sd.py"
MAKE_IDENTITY_SCRIPT = REPO_ROOT / "tools" / "make_identity.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("provision_sd", PROVISION_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations via sys.modules[cls.__module__], so
    # the module must be registered there before exec_module runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provision_sd = _load_module()


# ---------------------------------------------------------------------------
# Regex agreement with make_identity.py — must not drift apart.
# ---------------------------------------------------------------------------

_MAKE_IDENTITY_DEVICE_ID_PATTERN_LINE = re.compile(r'^DEVICE_ID_PATTERN\s*=\s*r"(?P<pattern>.*)"\s*$', re.MULTILINE)
_MAKE_IDENTITY_PASSWORD_PATTERN_LINE = re.compile(r'^PASSWORD_PATTERN\s*=\s*r"(?P<pattern>.*)"\s*$', re.MULTILINE)


def _extract_make_identity_device_id_pattern() -> str:
    text = MAKE_IDENTITY_SCRIPT.read_text(encoding="utf-8")
    match = _MAKE_IDENTITY_DEVICE_ID_PATTERN_LINE.search(text)
    assert match is not None
    return match.group("pattern")


def _extract_make_identity_password_pattern() -> str:
    text = MAKE_IDENTITY_SCRIPT.read_text(encoding="utf-8")
    match = _MAKE_IDENTITY_PASSWORD_PATTERN_LINE.search(text)
    assert match is not None
    return match.group("pattern")


def test_device_id_pattern_matches_make_identity() -> None:
    assert _extract_make_identity_device_id_pattern() == provision_sd.DEVICE_ID_PATTERN


def test_password_pattern_matches_make_identity() -> None:
    assert _extract_make_identity_password_pattern() == provision_sd.PASSWORD_PATTERN


# ---------------------------------------------------------------------------
# Identity validation / generation / rendering
# ---------------------------------------------------------------------------


def test_validate_device_id_accepts_valid() -> None:
    provision_sd.validate_device_id("405")
    provision_sd.validate_device_id("palmimo-405")


def test_validate_device_id_rejects_invalid() -> None:
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.validate_device_id("Not Valid!")


def test_validate_password_rejects_short_and_non_alnum() -> None:
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.validate_password("short7x")
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.validate_password("has|pipe1")


def test_generate_password_always_matches_password_regex() -> None:
    for _ in range(50):
        password = provision_sd.generate_password()
        assert provision_sd.PASSWORD_REGEX.match(password)


def test_generate_password_rejects_too_short_length() -> None:
    with pytest.raises(ValueError):
        provision_sd.generate_password(length=4)


def test_render_identity_json_matches_make_identity_shape() -> None:
    text = provision_sd.render_identity_json("405", "s3cr3tplain9")
    assert text == json.dumps({"device_id": "405", "initial_password": "s3cr3tplain9"}, indent=2) + "\n"
    assert json.loads(text) == {"device_id": "405", "initial_password": "s3cr3tplain9"}


def test_resolve_identity_generates_password_when_omitted() -> None:
    args = provision_sd.build_parser().parse_args(["--image", "x.img", "--device-id", "405"])
    identity = provision_sd.resolve_identity(args)
    assert identity.device_id == "405"
    assert identity.generated is True
    provision_sd.PASSWORD_REGEX.match(identity.password)


def test_resolve_identity_uses_provided_password() -> None:
    args = provision_sd.build_parser().parse_args(
        ["--image", "x.img", "--device-id", "405", "--password", "s3cr3tplain9"]
    )
    identity = provision_sd.resolve_identity(args)
    assert identity == provision_sd.Identity(device_id="405", password="s3cr3tplain9", generated=False)


def test_resolve_identity_loads_identity_file(tmp_path: Path) -> None:
    identity_path = tmp_path / "palmimo-identity.json"
    identity_path.write_text(json.dumps({"device_id": "405", "initial_password": "s3cr3tplain9"}), encoding="utf-8")
    args = provision_sd.build_parser().parse_args(["--image", "x.img", "--identity", str(identity_path)])
    identity = provision_sd.resolve_identity(args)
    assert identity == provision_sd.Identity(device_id="405", password="s3cr3tplain9", generated=False)


def test_resolve_identity_rejects_malformed_identity_file(tmp_path: Path) -> None:
    identity_path = tmp_path / "palmimo-identity.json"
    identity_path.write_text("{not json", encoding="utf-8")
    args = provision_sd.build_parser().parse_args(["--image", "x.img", "--identity", str(identity_path)])
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.resolve_identity(args)


# ---------------------------------------------------------------------------
# Argument validation: mutual exclusivity
# ---------------------------------------------------------------------------


def test_validate_args_requires_device_id_or_identity() -> None:
    args = provision_sd.build_parser().parse_args(["--image", "x.img"])
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.validate_args(args)


def test_validate_args_rejects_identity_with_device_id() -> None:
    args = provision_sd.build_parser().parse_args(["--image", "x.img", "--identity", "id.json", "--device-id", "405"])
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.validate_args(args)


def test_validate_args_accepts_device_id_alone() -> None:
    args = provision_sd.build_parser().parse_args(["--image", "x.img", "--device-id", "405"])
    provision_sd.validate_args(args)  # must not raise


def test_validate_args_accepts_identity_alone() -> None:
    args = provision_sd.build_parser().parse_args(["--image", "x.img", "--identity", "id.json"])
    provision_sd.validate_args(args)  # must not raise


# ---------------------------------------------------------------------------
# Image format detection
# ---------------------------------------------------------------------------


def test_detect_image_format_raw_by_extension(tmp_path: Path) -> None:
    p = tmp_path / "palmimo-1.0.0.img"
    p.write_bytes(b"\x00" * 1024)
    assert provision_sd.detect_image_format(p) == "raw"


def test_detect_image_format_xz_by_extension(tmp_path: Path) -> None:
    import lzma

    p = tmp_path / "palmimo-1.0.0.img.xz"
    p.write_bytes(lzma.compress(b"\x00" * 1024))
    assert provision_sd.detect_image_format(p) == "xz"


def test_detect_image_format_sniffs_magic_when_extension_unclear(tmp_path: Path) -> None:
    import lzma

    p = tmp_path / "palmimo-image.bin"
    p.write_bytes(lzma.compress(b"\x00" * 1024))
    assert provision_sd.detect_image_format(p) == "xz"


def test_detect_image_format_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.detect_image_format(tmp_path / "nope.img")


def test_detect_image_format_rejects_unrecognized_content(tmp_path: Path) -> None:
    p = tmp_path / "not-an-image.bin"
    p.write_bytes(b"hello world, not xz and not a .img")
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.detect_image_format(p)


# ---------------------------------------------------------------------------
# flash_image: streams raw and xz sources into a target file (a plain file,
# never a real device — this is what makes it unit-testable at all).
# ---------------------------------------------------------------------------


def test_flash_image_copies_raw_bytes_exactly(tmp_path: Path) -> None:
    src = tmp_path / "src.img"
    payload = bytes(range(256)) * 1000
    src.write_bytes(payload)
    dst = tmp_path / "dst.bin"
    written = provision_sd.flash_image(src, "raw", dst)
    assert written == len(payload)
    assert dst.read_bytes() == payload


def test_flash_image_decompresses_xz_source(tmp_path: Path) -> None:
    import lzma

    payload = bytes(range(256)) * 1000
    src = tmp_path / "src.img.xz"
    src.write_bytes(lzma.compress(payload))
    dst = tmp_path / "dst.bin"
    written = provision_sd.flash_image(src, "xz", dst)
    assert written == len(payload)
    assert dst.read_bytes() == payload


def test_flash_image_reports_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src.img"
    src.write_bytes(b"\x01" * (1024 * 1024))
    dst = tmp_path / "dst.bin"
    reports: list[int] = []
    # Shrink the module constant so this test doesn't depend on a
    # multi-hundred-MB fixture to see more than one report. monkeypatch's
    # string-keyed setattr keeps this off mypy's radar for the
    # dynamically-loaded module, which has no static attribute list.
    monkeypatch.setattr(provision_sd, "FLASH_PROGRESS_INTERVAL", 100_000)
    provision_sd.flash_image(src, "raw", dst, progress=reports.append)
    assert len(reports) >= 2
    assert reports[-1] == src.stat().st_size


def test_flash_image_raises_image_too_large_before_exceeding_target_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the block size so a small fixture still produces multiple
    # chunks — the guard must fire on the chunk that would cross the
    # capacity line, not just at EOF.
    monkeypatch.setattr(provision_sd, "FLASH_BLOCK_SIZE", 100)
    src = tmp_path / "src.img"
    src.write_bytes(b"\x02" * 1000)
    dst = tmp_path / "dst.bin"
    with pytest.raises(provision_sd.ImageTooLargeError):
        provision_sd.flash_image(src, "raw", dst, target_size_bytes=500)
    # Only whole chunks below the capacity line were written.
    assert dst.stat().st_size <= 500


def test_flash_image_within_target_capacity_succeeds(tmp_path: Path) -> None:
    src = tmp_path / "src.img"
    payload = b"\x03" * 1000
    src.write_bytes(payload)
    dst = tmp_path / "dst.bin"
    written = provision_sd.flash_image(src, "raw", dst, target_size_bytes=len(payload))
    assert written == len(payload)


def test_flash_image_raises_on_truncated_xz_source(tmp_path: Path) -> None:
    import lzma

    full = lzma.compress(b"\x04" * (1024 * 1024))
    src = tmp_path / "src.img.xz"
    src.write_bytes(full[: len(full) // 2])  # cut the stream in half
    dst = tmp_path / "dst.bin"
    with pytest.raises((lzma.LZMAError, EOFError)):
        provision_sd.flash_image(src, "xz", dst)


# ---------------------------------------------------------------------------
# check_image_fits_target / get_xz_uncompressed_size (pre-flight size guard)
# ---------------------------------------------------------------------------


def test_check_image_fits_target_accepts_image_within_capacity() -> None:
    provision_sd.check_image_fits_target(1000, 2000)  # must not raise


def test_check_image_fits_target_rejects_oversized_image() -> None:
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.check_image_fits_target(2000, 1000)


def test_check_image_fits_target_skips_when_target_size_unknown() -> None:
    provision_sd.check_image_fits_target(1_000_000, 0)  # must not raise


def test_parse_xz_robot_list_uncompressed_size_reads_totals_line() -> None:
    output = (
        "name\t/tmp/x.img.xz\nfile\t1\t1\t216\t512000\t0.000\tCRC64\t0\ntotals\t1\t1\t216\t512000\t0.000\tCRC64\t0\t1\n"
    )
    assert provision_sd.parse_xz_robot_list_uncompressed_size(output) == 512000


def test_parse_xz_robot_list_uncompressed_size_returns_none_when_absent() -> None:
    assert provision_sd.parse_xz_robot_list_uncompressed_size("not xz --robot output at all") is None


@pytest.mark.skipif(shutil.which("xz") is None, reason="xz binary not installed")
def test_get_xz_uncompressed_size_matches_real_xz_binary(tmp_path: Path) -> None:
    import lzma
    import subprocess as sp

    payload = b"\x05" * (500 * 1024)
    src = tmp_path / "real.img.xz"
    src.write_bytes(lzma.compress(payload))
    # Sanity: our own parser agrees with a direct xz invocation.
    result = sp.run(["xz", "--robot", "--list", str(src)], capture_output=True, text=True, check=True)
    assert provision_sd.parse_xz_robot_list_uncompressed_size(result.stdout) == len(payload)
    assert provision_sd.get_xz_uncompressed_size(src) == len(payload)


def test_get_xz_uncompressed_size_returns_none_when_xz_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision_sd.shutil, "which", lambda _name: None)
    src = tmp_path / "whatever.img.xz"
    src.write_bytes(b"not a real xz file")
    assert provision_sd.get_xz_uncompressed_size(src) is None


# ---------------------------------------------------------------------------
# Disk fingerprint — best-effort identity binding across confirm->flash and
# flash->identity-injection (macOS reuses diskN identifiers on replug).
# ---------------------------------------------------------------------------


def test_fingerprint_from_info_reads_media_name_size_and_uuid() -> None:
    info = {"MediaName": "SD Card Reader Media", "TotalSize": 63_864_569_856, "DiskUUID": "ABC-123"}
    fp = provision_sd.fingerprint_from_info(info)
    assert fp == provision_sd.DiskFingerprint(
        media_name="SD Card Reader Media", total_size=63_864_569_856, disk_uuid="ABC-123"
    )


def test_fingerprint_from_info_falls_back_to_volume_uuid() -> None:
    info = {"MediaName": "SD Card Reader Media", "TotalSize": 1000, "VolumeUUID": "VOL-1"}
    fp = provision_sd.fingerprint_from_info(info)
    assert fp.disk_uuid == "VOL-1"


def test_fingerprint_from_info_defaults_missing_fields() -> None:
    fp = provision_sd.fingerprint_from_info({})
    assert fp == provision_sd.DiskFingerprint(media_name="", total_size=0, disk_uuid="")


def test_fingerprint_matches_identical_disk() -> None:
    fp = provision_sd.DiskFingerprint(media_name="SD Card Reader Media", total_size=1000, disk_uuid="UUID-1")
    assert provision_sd.fingerprint_matches(fp, fp) is True


def test_fingerprint_matches_rejects_changed_size() -> None:
    before = provision_sd.DiskFingerprint(media_name="SD Card Reader Media", total_size=1000, disk_uuid="")
    after = dataclasses.replace(before, total_size=2000)
    assert provision_sd.fingerprint_matches(before, after) is False


def test_fingerprint_matches_rejects_changed_media_name() -> None:
    before = provision_sd.DiskFingerprint(media_name="SD Card Reader Media", total_size=1000, disk_uuid="")
    after = dataclasses.replace(before, media_name="A Different Card")
    assert provision_sd.fingerprint_matches(before, after) is False


def test_fingerprint_matches_rejects_changed_uuid() -> None:
    before = provision_sd.DiskFingerprint(media_name="SD Card Reader Media", total_size=1000, disk_uuid="UUID-1")
    after = dataclasses.replace(before, disk_uuid="UUID-2")
    assert provision_sd.fingerprint_matches(before, after) is False


def test_fingerprint_matches_same_size_different_uuid_still_size_ok_but_uuid_wins() -> None:
    # Two cards of the exact same model: size and name agree, but a UUID
    # difference (when both sides happen to report one) must still refuse.
    before = provision_sd.DiskFingerprint(
        media_name="Generic SD Card Reader", total_size=63_864_569_856, disk_uuid="UUID-A"
    )
    after = provision_sd.DiskFingerprint(
        media_name="Generic SD Card Reader", total_size=63_864_569_856, disk_uuid="UUID-B"
    )
    assert provision_sd.fingerprint_matches(before, after) is False


def test_fingerprint_matches_missing_uuid_on_both_sides_still_compares_the_rest() -> None:
    before = provision_sd.DiskFingerprint(media_name="Generic SD Card Reader", total_size=1000, disk_uuid="")
    after = provision_sd.DiskFingerprint(media_name="Generic SD Card Reader", total_size=1000, disk_uuid="")
    assert provision_sd.fingerprint_matches(before, after) is True
    # ...and still refuses when name/size disagree despite both UUIDs being blank.
    changed = dataclasses.replace(after, total_size=999)
    assert provision_sd.fingerprint_matches(before, changed) is False


# ---------------------------------------------------------------------------
# Disk discovery / safety filtering from recorded diskutil plist fixtures
# ---------------------------------------------------------------------------


def _external_sd_disk_plist() -> bytes:
    """A recorded-shape fixture for `diskutil list -plist external
    physical` with one plausible SD card candidate."""
    data = {
        "AllDisks": ["disk4", "disk4s1", "disk4s2"],
        "AllDisksAndPartitions": [
            {
                "Content": "FDisk_partition_scheme",
                "DeviceIdentifier": "disk4",
                "Size": 63_864_569_856,
                "Partitions": [
                    {
                        "Content": "Windows_FAT_32",
                        "DeviceIdentifier": "disk4s1",
                        "VolumeName": "bootfs",
                        "Size": 536_870_912,
                        "MountPoint": "/Volumes/bootfs",
                    },
                    {
                        "Content": "Linux",
                        "DeviceIdentifier": "disk4s2",
                        "VolumeName": "rootfs",
                        "Size": 63_327_698_944,
                        "MountPoint": "",
                    },
                ],
            }
        ],
        "VolumesFromDisks": ["bootfs"],
        "WholeDisks": ["disk4"],
    }
    return plistlib.dumps(data)


def _internal_disk_plist() -> bytes:
    """Recorded shape of an internal disk (`diskutil list -plist internal
    physical`) — must never be offered even if it somehow showed up."""
    data = {
        "AllDisks": ["disk0", "disk0s1"],
        "AllDisksAndPartitions": [
            {
                "Content": "GUID_partition_scheme",
                "DeviceIdentifier": "disk0",
                "OSInternal": False,
                "Size": 1_000_555_581_440,
                "Partitions": [
                    {
                        "Content": "Apple_APFS",
                        "DeviceIdentifier": "disk0s2",
                        "VolumeName": "Macintosh HD",
                        "Size": 994_662_584_320,
                        "MountPoint": "/",
                    }
                ],
            }
        ],
        "VolumesFromDisks": [],
        "WholeDisks": ["disk0"],
    }
    return plistlib.dumps(data)


def test_parse_disk_list_plist_extracts_whole_disk_candidates() -> None:
    candidates = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())
    assert len(candidates) == 1
    disk = candidates[0]
    assert disk.identifier == "disk4"
    assert disk.size_bytes == 63_864_569_856
    assert [p.identifier for p in disk.partitions] == ["disk4s1", "disk4s2"]
    assert disk.partitions[0].volume_name == "bootfs"


def test_parse_disk_list_plist_excludes_partitions_from_top_level() -> None:
    # Only whole disks (per WholeDisks) become candidates, not their
    # partitions, even though partitions also appear in
    # AllDisksAndPartitions-shaped listings in the wild.
    candidates = provision_sd.parse_disk_list_plist(_internal_disk_plist())
    assert [c.identifier for c in candidates] == ["disk0"]


def test_is_disk_safe_to_offer_accepts_external_removable_sd_card() -> None:
    candidates = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())
    disk = candidates[0]
    info = {"Internal": False, "VirtualOrPhysical": "Physical", "RemovableMedia": True, "Ejectable": True}
    ok, _reason = provision_sd.is_disk_safe_to_offer(disk, info)
    assert ok is True


def test_is_disk_safe_to_offer_refuses_internal_disk() -> None:
    candidates = provision_sd.parse_disk_list_plist(_internal_disk_plist())
    disk = candidates[0]
    info = {"Internal": True, "VirtualOrPhysical": "Physical", "RemovableMedia": False, "Ejectable": False}
    ok, reason = provision_sd.is_disk_safe_to_offer(disk, info)
    assert ok is False
    assert "internal" in reason


def test_is_disk_safe_to_offer_refuses_disk_with_root_mounted() -> None:
    candidates = provision_sd.parse_disk_list_plist(_internal_disk_plist())
    disk = candidates[0]
    # Even if diskutil somehow reported it as external/removable, a
    # partition mounted at "/" must refuse it.
    info = {"Internal": False, "VirtualOrPhysical": "Physical", "RemovableMedia": True, "Ejectable": True}
    ok, reason = provision_sd.is_disk_safe_to_offer(disk, info)
    assert ok is False
    assert "system" in reason


def test_filter_safe_candidates_keeps_only_sd_card() -> None:
    sd = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())[0]
    internal = provision_sd.parse_disk_list_plist(_internal_disk_plist())[0]
    info_by_id = {
        sd.identifier: {"Internal": False, "VirtualOrPhysical": "Physical", "RemovableMedia": True, "Ejectable": True},
        internal.identifier: {
            "Internal": True,
            "VirtualOrPhysical": "Physical",
            "RemovableMedia": False,
            "Ejectable": False,
        },
    }
    safe = provision_sd.filter_safe_candidates([sd, internal], info_by_id)
    assert [c.identifier for c in safe] == [sd.identifier]


def test_filter_safe_candidates_excludes_disk_with_no_info() -> None:
    sd = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())[0]
    safe = provision_sd.filter_safe_candidates([sd], {})
    assert safe == []


# ---------------------------------------------------------------------------
# Confirmation string check — a bare y/N is not accepted, only the exact
# disk identifier.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("disk4", True),
        ("  disk4  ", True),
        ("/dev/disk4", True),
        ("y", False),
        ("Y", False),
        ("yes", False),
        ("disk5", False),
        ("disk40", False),
        ("", False),
    ],
)
def test_confirmation_matches(typed: str, expected: bool) -> None:
    assert provision_sd.confirmation_matches(typed, "disk4") is expected


# ---------------------------------------------------------------------------
# Pi boot partition detection
# ---------------------------------------------------------------------------


def test_is_pi_boot_partition_true_when_marker_present(tmp_path: Path) -> None:
    (tmp_path / "config.txt").write_text("", encoding="utf-8")
    assert provision_sd.is_pi_boot_partition(tmp_path) is True


def test_is_pi_boot_partition_false_when_no_marker(tmp_path: Path) -> None:
    (tmp_path / "unrelated.txt").write_text("", encoding="utf-8")
    assert provision_sd.is_pi_boot_partition(tmp_path) is False


def test_find_boot_partition_prefers_fat_content() -> None:
    disk = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())[0]
    boot = provision_sd.find_boot_partition(disk)
    assert boot.identifier == "disk4s1"
    assert boot.content == "Windows_FAT_32"


# ---------------------------------------------------------------------------
# --dry-run plan output (also exercised end-to-end via subprocess below)
# ---------------------------------------------------------------------------


def test_build_plan_text_includes_key_fields() -> None:
    identity = provision_sd.Identity(device_id="405", password="s3cr3tplain9", generated=False)
    text = provision_sd.build_plan_text(
        image_path=Path("palmimo-1.0.0.img.xz"), image_format="xz", identity=identity, target="disk4"
    )
    assert "405" in text
    assert "disk4" in text
    assert "xz" in text
    assert "--dry-run" in text


def test_dry_run_end_to_end_via_subprocess(tmp_path: Path) -> None:
    image_path = tmp_path / "fake.img"
    image_path.write_bytes(b"\x00" * (2 * 1024 * 1024))
    result = subprocess.run(
        [
            sys.executable,
            str(PROVISION_SCRIPT),
            "--image",
            str(image_path),
            "--device-id",
            "405",
            "--password",
            "s3cr3tplain9",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "405" in result.stdout
    assert "plan" in result.stdout.lower()
    # Never any diskutil/dd/mount invocation text implying a real write.
    assert "Ejecting" not in result.stdout


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(PROVISION_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--device-id" in result.stdout


def test_missing_identity_source_errors() -> None:
    with pytest.raises(provision_sd.ProvisionError):
        provision_sd.validate_args(provision_sd.build_parser().parse_args(["--image", "x.img"]))


# ---------------------------------------------------------------------------
# _resolve_target_disk: refuses a --device not offered by diskutil at all.
# Real diskutil is kept out — list_candidate_disks is monkeypatched to a
# fixed, in-memory candidate list.
# ---------------------------------------------------------------------------


def test_resolve_target_disk_refuses_device_absent_from_candidate_list(monkeypatch: pytest.MonkeyPatch) -> None:
    sd_candidate = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())[0]
    monkeypatch.setattr(provision_sd, "list_candidate_disks", lambda: [sd_candidate])
    with pytest.raises(provision_sd.ProvisionError, match="not a physical disk"):
        provision_sd._resolve_target_disk("disk99")


def test_resolve_target_disk_refuses_device_that_fails_the_safety_check(monkeypatch: pytest.MonkeyPatch) -> None:
    sd_candidate = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())[0]
    monkeypatch.setattr(provision_sd, "list_candidate_disks", lambda: [sd_candidate])
    monkeypatch.setattr(provision_sd, "disk_info", lambda _identifier: {"Internal": True})
    with pytest.raises(provision_sd.ProvisionError, match="refused"):
        provision_sd._resolve_target_disk(sd_candidate.identifier)


def test_resolve_target_disk_accepts_a_safe_matching_device(monkeypatch: pytest.MonkeyPatch) -> None:
    sd_candidate = provision_sd.parse_disk_list_plist(_external_sd_disk_plist())[0]
    monkeypatch.setattr(provision_sd, "list_candidate_disks", lambda: [sd_candidate])
    monkeypatch.setattr(
        provision_sd,
        "disk_info",
        lambda _identifier: {
            "Internal": False,
            "VirtualOrPhysical": "Physical",
            "RemovableMedia": True,
            "Ejectable": True,
        },
    )
    resolved = provision_sd._resolve_target_disk(f"/dev/{sd_candidate.identifier}")
    assert resolved == sd_candidate


def test_builtin_sd_reader_is_offered_despite_internal() -> None:
    # A Mac's built-in SDXC slot reports Internal=True; the Secure Digital
    # bus + removable media is what distinguishes the card from a real
    # internal disk (observed live 2026-08-25).
    info = {
        "Internal": True,
        "BusProtocol": "Secure Digital",
        "RemovableMedia": True,
        "Ejectable": True,
        "VirtualOrPhysical": "Physical",
    }
    candidate = provision_sd.DiskCandidate(identifier="disk4", size_bytes=124657860608, content="", partitions=())
    ok, reason = provision_sd.is_disk_safe_to_offer(candidate, info)
    assert ok, reason


def test_internal_non_sd_disk_stays_refused() -> None:
    info = {
        "Internal": True,
        "BusProtocol": "Apple Fabric",
        "RemovableMedia": False,
        "Ejectable": False,
        "VirtualOrPhysical": "Physical",
    }
    candidate = provision_sd.DiskCandidate(identifier="disk0", size_bytes=10**12, content="", partitions=())
    ok, reason = provision_sd.is_disk_safe_to_offer(candidate, info)
    assert not ok
    assert "internal" in reason


def test_internal_sd_bus_without_removable_media_stays_refused() -> None:
    info = {
        "Internal": True,
        "BusProtocol": "Secure Digital",
        "RemovableMedia": False,
        "Ejectable": False,
        "VirtualOrPhysical": "Physical",
    }
    candidate = provision_sd.DiskCandidate(identifier="disk9", size_bytes=10**9, content="", partitions=())
    ok, _ = provision_sd.is_disk_safe_to_offer(candidate, info)
    assert not ok


def test_require_root_refuses_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision_sd.os, "geteuid", lambda: 501)
    with pytest.raises(provision_sd.ProvisionError, match="sudo"):
        provision_sd.require_root()


def test_require_root_passes_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision_sd.os, "geteuid", lambda: 0)
    provision_sd.require_root()
