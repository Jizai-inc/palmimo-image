"""Contracts for the isolated, Portal-controlled Wi-Fi QR reader."""

import configparser
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ROOT / "platform"
UNIT_NAME = "palmimo-wifi-qr.service"
UNIT_PATH = Path("etc/systemd/system") / UNIT_NAME
RULE_PATH = Path("etc/polkit-1/rules.d/63-palmimo-portal-wifi-qr.rules")


@pytest.fixture()
def unit() -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None, strict=False)
    assert config.read(PLATFORM / "files" / UNIT_PATH)
    return config


def test_reader_has_no_network_access(unit: configparser.ConfigParser) -> None:
    service = unit["Service"]
    assert service.getboolean("PrivateNetwork")
    assert service["RestrictAddressFamilies"].split() == ["AF_UNIX"]


def test_reader_uses_dynamic_identity_and_limited_devices(unit: configparser.ConfigParser) -> None:
    service = unit["Service"]
    assert service.getboolean("DynamicUser")
    assert "User" not in service and "Group" not in service
    assert set(service["SupplementaryGroups"].split()) == {"video", "dialout"}
    assert service["DevicePolicy"] == "closed"
    devices = [
        shlex.split(line.partition("=")[2])
        for line in (PLATFORM / "files" / UNIT_PATH).read_text().splitlines()
        if line.startswith("DeviceAllow=")
    ]
    assert sorted(devices) == [["char-ttyACM", "rw"], ["char-video4linux", "rw"]]


def test_reader_stops_with_portal_without_restarting(unit: configparser.ConfigParser) -> None:
    assert "palmimo-portal.service" in unit["Unit"]["StopPropagatedFrom"].split()
    assert "palmimo-portal.service" in unit["Unit"]["After"].split()
    assert not unit["Unit"].get("PartOf", "")
    assert unit["Service"]["Restart"] == "no"
    assert float(unit["Service"]["TimeoutStopSec"]) == 5


def test_reader_runs_isolated_code_with_shared_locks(unit: configparser.ConfigParser) -> None:
    service = unit["Service"]
    assert shlex.split(service["ExecStart"]) == ["/usr/bin/python3", "-I", "/run/palmimo-wifi-qr-code/reader.py"]
    assert service["BindReadOnlyPaths"].split() == [
        "/home/user/palmimo-portal/palmimo_portal/wifi_qr_reader:/run/palmimo-wifi-qr-code"
    ]
    assert service["RuntimeDirectory"].split() == ["palmimo-wifi-qr"]
    assert "/run/lock/palmimo" in service["ReadWritePaths"].split()
    assert "PALMIMO_WIFI_QR_PEER_USER=user" in shlex.split(service["Environment"])


def test_reader_ships_as_static_unit_with_dependencies(unit: configparser.ConfigParser) -> None:
    assert "Install" not in unit
    manifest = json.loads((PLATFORM / "manifest.json").read_text())
    assert UNIT_NAME in manifest["owns"]["units"]
    for path in (UNIT_PATH, RULE_PATH):
        assert {"path": str(path), "owner": "root", "group": "root", "mode": "0644"} in manifest["owns"]["files"]
    for packages in (manifest["owns"]["apt_packages"], (ROOT / "packages.txt").read_text().splitlines()):
        for package in ("python3-zbar", "libturbojpeg0", "python3-serial"):
            assert packages.count(package) == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js to execute polkit JavaScript")
def test_reader_polkit_grants_only_portal_start_and_stop() -> None:
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let rule;
const Result = { YES: 'yes', NOT_HANDLED: 'not-handled' };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
    polkit: { Result, addRule: callback => { rule = callback; } }
});
for (const id of ['org.freedesktop.systemd1.manage-units', 'org.freedesktop.systemd1.manage-unit-files', '']) {
    for (const user of ['user', 'palmimo-app', 'root', 'other', undefined]) {
        for (const unit of ['palmimo-wifi-qr.service', 'palmimo-portal.service', 'comitup.service', 'palmimo-app@demo.service', undefined]) {
            for (const verb of ['start', 'stop', 'restart', 'try-restart', 'reload', 'reload-or-restart', 'enable', 'set-property', 'reset-failed', undefined]) {
                const actual = rule({ id, lookup: key => ({ unit, verb })[key] }, { user });
                const allowed = id === 'org.freedesktop.systemd1.manage-units' && user === 'user' &&
                    unit === 'palmimo-wifi-qr.service' && ['start', 'stop'].includes(verb);
                assert.equal(actual, allowed ? Result.YES : Result.NOT_HANDLED, JSON.stringify({id, user, unit, verb}));
            }
        }
    }
}
"""
    result = subprocess.run(["node", "-e", script, str(PLATFORM / "files" / RULE_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
