from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "files/usr/local/lib/palmimo/firstboot.sh"
BOOT = {"device_id": "406", "initial_password": "BootSecret406"}
PROTECTED = {"device_id": "407", "initial_password": "ProtectedSecret407"}


@pytest.mark.parametrize(
    ("boot", "protected", "expected"),
    [
        pytest.param(json.dumps(BOOT), None, BOOT, id="boot-only"),
        pytest.param(None, json.dumps(PROTECTED), PROTECTED, id="protected-only"),
        pytest.param(json.dumps(BOOT), json.dumps(PROTECTED), BOOT, id="both-boot-wins"),
        pytest.param(None, None, None, id="neither"),
        pytest.param("not json", json.dumps(PROTECTED), None, id="broken-boot-does-not-fall-back"),
        *[
            pytest.param(
                payload if location == "boot" else None,
                payload if location == "protected" else None,
                None,
                id=f"{location}-{invalid}",
            )
            for location in ("boot", "protected")
            for invalid, payload in (
                ("invalid-json", "not json"),
                ("invalid-device-id", json.dumps({**BOOT, "device_id": "bad/id"})),
                ("invalid-password", json.dumps({**BOOT, "initial_password": "bad!password"})),
            )
        ],
    ],
)
def test_firstboot_personalizes_from_the_preferred_available_identity(
    tmp_path: Path, boot: str | None, protected: str | None, expected: dict[str, str] | None
) -> None:
    root = tmp_path / "root"
    for relative, contents in (
        ("boot/firmware/palmimo-identity.json", boot),
        ("etc/palmimo/identity.json", protected),
        ("etc/hostname", "palmimo\n"),
        ("etc/hosts", "127.0.0.1 localhost\n"),
        ("etc/comitup.conf", "ap_password: unchanged\nweb_service: palmimo-portal\n"),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if contents is not None:
            path.write_text(contents)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("hostnamectl", "chown"):
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "PALMIMO_ROOT": str(root), "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if expected is not None else 1), result.stderr
    passwords = [
        line.removeprefix("ap_password:").strip()
        for line in (root / "etc/comitup.conf").read_text().splitlines()
        if line.startswith("ap_password:")
    ]
    if expected is None:
        assert (root / "etc/hostname").read_text() == "palmimo\n"
        assert (root / "etc/hosts").read_text() == "127.0.0.1 localhost\n"
        assert passwords == ["unchanged"]
        assert not (root / "var/lib/palmimo/firstboot-done").exists()
    else:
        hostname = f"palmimo-{expected['device_id']}"
        assert (root / "etc/hostname").read_text() == f"{hostname}\n"
        assert (root / "etc/hosts").read_text() == f"127.0.0.1 localhost\n127.0.1.1\t{hostname}\n"
        assert passwords == [expected["initial_password"]]
        assert (root / "var/lib/palmimo/firstboot-done").is_file()
