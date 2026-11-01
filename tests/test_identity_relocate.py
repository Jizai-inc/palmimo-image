from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


HELPER = Path(__file__).resolve().parents[1] / "platform/files/usr/lib/palmimo/identity-relocate"
IDENTITY = {"device_id": "406", "initial_password": "StickerSecret406"}
OTHER = {"device_id": "407", "initial_password": "OtherSecret407"}


def run_relocate(boot: Path, directory: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # macOS sync has no -f; persistence on vfat requires a device test.
    sync = bin_dir / "sync"
    sync.write_text("#!/bin/sh\nexit 0\n")
    sync.chmod(0o755)
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--boot-identity",
            str(boot),
            "--identity-dir",
            str(directory),
            "--owner-uid",
            str(os.getuid()),
            "--identity-group",
            str(os.getgid()),
            "--root-group",
            str(os.getgid()),
        ],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("old", "new", "env_exists", "debris", "expected", "code"),
    [
        pytest.param(json.dumps(IDENTITY), None, False, False, IDENTITY, 0, id="old-only"),
        pytest.param(json.dumps(IDENTITY), IDENTITY, False, False, IDENTITY, 0, id="same-before-env"),
        pytest.param(json.dumps(IDENTITY), IDENTITY, True, False, IDENTITY, 0, id="same-after-env"),
        pytest.param(json.dumps(IDENTITY), OTHER, True, False, IDENTITY, 0, id="replacement"),
        pytest.param(json.dumps(IDENTITY), None, False, True, IDENTITY, 0, id="interrupted-write"),
        pytest.param("broken StickerSecret406", OTHER, False, False, OTHER, 0, id="broken-old-valid-new"),
        pytest.param("broken StickerSecret406", None, False, False, None, 1, id="broken-old-only"),
        pytest.param(json.dumps([IDENTITY]), None, False, False, None, 1, id="non-object"),
        pytest.param(json.dumps({**IDENTITY, "device_id": 406}), None, False, False, None, 1, id="wrong-id-type"),
        pytest.param(
            json.dumps({**IDENTITY, "initial_password": 123}), None, False, False, None, 1, id="wrong-password-type"
        ),
        pytest.param(
            json.dumps({"initial_password": IDENTITY["initial_password"]}), None, False, False, None, 1, id="missing-id"
        ),
    ],
)
def test_relocate_converges_without_exposing_passwords(
    tmp_path: Path,
    old: str,
    new: dict[str, str] | None,
    env_exists: bool,
    debris: bool,
    expected: dict[str, str] | None,
    code: int,
) -> None:
    boot = tmp_path / "boot/palmimo-identity.json"
    boot.parent.mkdir()
    boot.write_text(old)
    directory = tmp_path / "etc/palmimo"
    directory.mkdir(parents=True)
    identity = directory / "identity.json"
    env = directory / "identity.env"
    if new is not None:
        identity.write_text(json.dumps(new))
    if env_exists:
        env.write_text(f"PALMIMO_IDENTITY_FILE={identity}\n")
    if debris:
        (directory / ".identity.json.interrupted").write_text("partial secret")
        (directory / ".identity.env.interrupted").write_text("partial path")
    result = run_relocate(boot, directory, tmp_path)
    assert result.returncode == code
    for password in (IDENTITY["initial_password"], OTHER["initial_password"]):
        assert password not in result.stdout + result.stderr
    if expected is None:
        assert boot.read_text() == old
        assert not identity.exists()
        assert not env.exists()
        assert set(directory.iterdir()) == set()
    else:
        assert not boot.exists()
        assert json.loads(identity.read_text()) == expected
        assert stat.S_IMODE(identity.stat().st_mode) == 0o640
        assert env.read_text() == f"PALMIMO_IDENTITY_FILE={identity}\n"
        assert stat.S_IMODE(env.stat().st_mode) == 0o644
        assert set(directory.iterdir()) == {identity, env}


def test_relocate_preserves_boot_identity_when_destination_is_unwritable(tmp_path: Path) -> None:
    boot = tmp_path / "palmimo-identity.json"
    boot.write_text(json.dumps(IDENTITY))
    directory = tmp_path / "not-a-directory"
    directory.write_text("occupied")
    result = run_relocate(boot, directory, tmp_path)
    assert result.returncode == 1
    assert json.loads(boot.read_text()) == IDENTITY
    assert directory.read_text() == "occupied"
    assert IDENTITY["initial_password"] not in result.stdout + result.stderr


def test_relocate_keeps_boot_copy_until_portal_path_can_be_published(tmp_path: Path) -> None:
    boot = tmp_path / "palmimo-identity.json"
    boot.write_text(json.dumps(IDENTITY))
    directory = tmp_path / "etc/palmimo"
    directory.mkdir(parents=True)
    identity = directory / "identity.json"
    env = directory / "identity.env"
    env.mkdir()
    result = run_relocate(boot, directory, tmp_path)
    assert result.returncode == 1
    assert json.loads(boot.read_text()) == IDENTITY
    assert json.loads(identity.read_text()) == IDENTITY
    assert env.is_dir()
    assert IDENTITY["initial_password"] not in result.stdout + result.stderr
    env.rmdir()
    result = run_relocate(boot, directory, tmp_path)
    assert result.returncode == 0
    assert not boot.exists()
    assert json.loads(identity.read_text()) == IDENTITY
    assert env.read_text() == f"PALMIMO_IDENTITY_FILE={identity}\n"
    assert set(directory.iterdir()) == {identity, env}


def test_relocate_preserves_all_inputs_when_boot_identity_cannot_be_read(tmp_path: Path) -> None:
    boot = tmp_path / "boot/palmimo-identity.json"
    boot.mkdir(parents=True)
    sentinel = boot / "manufacturing-input"
    sentinel.write_text("keep manufacturing input")
    directory = tmp_path / "etc/palmimo"
    directory.mkdir(parents=True)
    identity = directory / "identity.json"
    env = directory / "identity.env"
    identity.write_text(json.dumps(OTHER))
    env.write_text(f"PALMIMO_IDENTITY_FILE={identity}\n# retained configuration\n")
    for path in (boot, sentinel, identity, env):
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (sentinel, identity, env)}
    boot_mtime = boot.stat().st_mtime_ns
    result = run_relocate(boot, directory, tmp_path)
    assert result.returncode == 1
    assert boot.is_dir()
    assert boot.stat().st_mtime_ns == boot_mtime
    assert set(boot.iterdir()) == {sentinel}
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before
    assert set(directory.iterdir()) == {identity, env}


def test_relocate_leaves_protected_files_unchanged_when_boot_identity_is_absent(tmp_path: Path) -> None:
    boot = tmp_path / "boot/palmimo-identity.json"
    boot.parent.mkdir()
    directory = tmp_path / "etc/palmimo"
    directory.mkdir(parents=True)
    identity = directory / "identity.json"
    env = directory / "identity.env"
    identity.write_text(json.dumps(IDENTITY))
    env.write_text(f"PALMIMO_IDENTITY_FILE={identity}\n")
    for path in (identity, env):
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (identity, env)}
    result = run_relocate(boot, directory, tmp_path)
    assert result.returncode == 0
    assert not boot.exists()
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before
    assert set(directory.iterdir()) == {identity, env}
