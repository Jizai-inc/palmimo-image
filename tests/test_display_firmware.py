import gzip
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "platform/files/usr/local/libexec/palmimo/palmimo-display-firmware"
loader = importlib.machinery.SourceFileLoader("display_firmware", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
fw = importlib.util.module_from_spec(spec)
loader.exec_module(fw)


class Fake(fw.Hardware):  # type: ignore[name-defined]
    def __init__(self) -> None:
        self.now = 0.0
        self.ports = [("tty-face", "abcdef0123456789")]
        self.usb = 0
        self.version = "old"
        self.chip = "abcdef0123456789"
        self.commands: list[list[str]] = []
        self.payloads: list[bytes] = []
        self.fail = 0
        self.in_use = False
        self.flash = bytearray(b"X" * 8192)
        self.power: list[str] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def cdc(self) -> list[tuple[str, str]]:
        return self.ports

    def bootsel_count(self) -> int:
        return self.usb

    def port_busy(self, port: str) -> bool:
        return self.in_use

    def exchange(self, port: str, command: str, timeout: float) -> str:
        self.power.append(command)
        return {
            "VERSION": ("VERSION " + self.version) if self.version else "",
            "STAT": "STAT power=ON",
            "POWER ON": "OK",
        }[command]

    def touch(self, port: str) -> None:
        self.ports = []
        self.usb = 1

    def picotool(self, args: list[str], timeout: float) -> str:
        self.commands.append(args)
        if args[:2] == ["info", "-d"]:
            return f"chipid: 0x{self.chip}"
        if args == ["reboot", "-u", "-f"]:
            self.touch("tty-face")
        if args[0] == "load":
            data = Path(args[2]).read_bytes()
            self.payloads.append(data)
            if len(self.payloads) == self.fail:
                raise OSError("USB failure")
            offset = int(args[-1], 16) - 0x10000000
            self.flash[offset : offset + len(data)] = data
        if args == ["reboot", "-a"]:
            self.ports = [("tty-face", self.chip)]
            self.usb = 0
            self.version = "new"
        return ""


@pytest.fixture
def rig(tmp_path: Path) -> tuple[Any, Fake]:
    paths = fw.Paths(tmp_path / "firmware", tmp_path / "state/state.json", tmp_path / "work", tmp_path / "locks")
    paths.firmware.mkdir()
    image = b"new\0" + b"A" * 8188
    (paths.firmware / "face_serial.bin.gz").write_bytes(gzip.compress(image))
    (paths.firmware / "firmware.json").write_text(
        json.dumps({"version": "new", "sha256": hashlib.sha256(image).hexdigest()})
    )
    hw = Fake()
    return fw.Updater(paths, hw), hw


def test_undeclared_does_not_touch_hardware(rig: tuple[Any, Fake]) -> None:
    updater, hw = rig
    (updater.paths.firmware / "firmware.json").unlink()
    result, code = updater.ensure()
    assert (result["result"], code) == ("undeclared", 0)
    assert not hw.commands and not hw.power


def test_write_order_and_power_restoration(rig: tuple[Any, Fake]) -> None:
    updater, hw = rig
    result, code = updater.ensure()
    assert (result["result"], result["before"], result["after"], code) == ("updated", "old", "new", 0)
    assert hw.payloads == [b"\xff" * 4096, b"A" * 4096, b"new\0" + b"A" * 4092]
    assert hw.commands[-1] == ["reboot", "-a"]
    assert hw.power[-1] == "POWER ON"
    assert (updater.paths.locks / "display.lock").stat().st_mode & 0o777 == 0o666
    assert updater.paths.state.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("failure", [2, 3])
def test_interrupted_write_leaves_unbootable_header(rig: tuple[Any, Fake], failure: int) -> None:
    updater, hw = rig
    hw.fail = failure
    result, code = updater.ensure(boot=True)
    assert (result["result"], code) == ("failed", 1)
    assert hw.flash[:4096] == b"\xff" * 4096
    assert ["reboot", "-a"] not in hw.commands
    hw.fail = 0
    hw.payloads.clear()
    result, code = updater.ensure(boot=True)
    assert (result["before"], result["result"], code) == ("bootsel", "updated", 0)


@pytest.mark.parametrize("case", ["silent", "multiple-cdc", "multiple-usb", "unknown-chip", "wrong-chip"])
def test_unidentified_devices_are_never_written(rig: tuple[Any, Fake], case: str) -> None:
    updater, hw = rig
    if case == "silent":
        hw.version = ""
    elif case == "multiple-cdc":
        hw.ports *= 2
    else:
        hw.ports = []
        hw.usb = 2 if case == "multiple-usb" else 1
        if case == "wrong-chip":
            updater.save({"seen": True, "chip_id": "ffffffffffffffff"})
    result, code = updater.ensure()
    assert (result["result"], code) == ("failed", 1)
    assert not hw.payloads


@pytest.mark.parametrize("seen", [False, True])
def test_absence_remembers_previous_face(rig: tuple[Any, Fake], seen: bool) -> None:
    updater, hw = rig
    updater.save({"seen": seen})
    hw.ports = []
    result, code = updater.ensure(boot=True)
    assert (result["result"], code) == (("failed", 1) if seen else ("absent", 0))
    assert hw.now == 15
    assert json.loads(updater.paths.state.read_text())["consecutive_failures"] == 0


@pytest.mark.parametrize("case", ["hash", "version"])
def test_integrity_and_version_mismatch_fail(rig: tuple[Any, Fake], case: str) -> None:
    updater, hw = rig
    if case == "hash":
        (updater.paths.firmware / "face_serial.bin.gz").write_bytes(gzip.compress(b"bad"))
    else:
        original = hw.picotool

        def reboot(args: list[str], timeout: float) -> str:
            output = original(args, timeout)
            if args == ["reboot", "-a"]:
                hw.version = "wrong"
            return output

        hw.picotool = reboot  # type: ignore[method-assign]
    result, code = updater.ensure()
    assert (result["result"], code) == ("failed", 1)
    if case == "hash":
        assert not hw.payloads
    else:
        assert result["after"] == "wrong"


def test_three_boot_write_failures_skip_until_manual_retry(rig: tuple[Any, Fake]) -> None:
    updater, hw = rig
    for _ in range(3):
        hw.payloads.clear()
        hw.fail = 2
        assert updater.ensure(boot=True)[0]["result"] == "failed"
    hw.payloads.clear()
    assert updater.ensure(boot=True)[0]["result"] == "skipped"
    assert not hw.payloads
    hw.fail = 0
    assert updater.ensure()[0]["result"] == "updated"
    assert json.loads(updater.paths.state.read_text())["consecutive_failures"] == 0


@pytest.mark.parametrize("value", [[], {}, {"version": "", "sha256": "x"}, {"version": 5, "sha256": None}])
def test_invalid_declaration_is_config_error(rig: tuple[Any, Fake], value: Any) -> None:
    updater, hw = rig
    (updater.paths.firmware / "firmware.json").write_text(json.dumps(value))
    assert updater.ensure()[1] == 78
    assert not hw.commands


@pytest.mark.parametrize(
    "case", ["valid", "dirty-declaration", "unknown-binary", "hash", "version", "large", "forbidden"]
)
def test_release_validation(rig: tuple[Any, Fake], case: str) -> None:
    updater, _ = rig
    directory = updater.paths.firmware
    data = b"1.2.3+abcdef\0" + b"A" * 5000
    version = "1.2.3+abcdef"
    if case == "dirty-declaration":
        version += ".dirty"
    elif case == "unknown-binary":
        data += b"1.2.3+unknown\0"
    elif case == "version":
        data = b"1.2.3+abcdef.dirty\0" + b"A" * 5000
    elif case == "large":
        data += b"A" * (10 * 1024 * 1024)
    elif case == "forbidden":
        data += b"/source/private-source-tree/file.c"
    digest = hashlib.sha256(data if case != "hash" else b"wrong").hexdigest()
    (directory / "firmware.json").write_text(json.dumps({"version": version, "sha256": digest}))
    (directory / "face_serial.bin.gz").write_bytes(gzip.compress(data))
    if case == "valid":
        fw.validate_artifact(directory, ["private-source-tree"])
    else:
        with pytest.raises(fw.FirmwareError):
            fw.validate_artifact(directory, ["private-source-tree"])


def test_shipped_release_assets() -> None:
    import os

    directory = SCRIPT.parents[2] / "lib/palmimo/display-firmware"
    if not (directory / "firmware.json").exists():
        pytest.skip("No firmware is declared")
    fw.validate_artifact(directory, os.environ.get("PALMIMO_FIRMWARE_FORBIDDEN_TERMS", "").split(","))


@pytest.mark.parametrize("busy", ["reservation", "port"])
def test_busy_never_touches_firmware(rig: tuple[Any, Fake], busy: str) -> None:
    updater, hw = rig
    if busy == "port":
        hw.in_use = True
        assert updater.ensure()[1] == 75
    else:
        with fw.reservation(updater.paths.locks):
            assert updater.ensure()[1] == 75
    assert not hw.payloads


@pytest.mark.parametrize("version", ["new", "ERR VERSION (try LIST)"])
def test_current_or_legacy_display(rig: tuple[Any, Fake], version: str) -> None:
    updater, hw = rig
    hw.version = version
    if version.startswith("ERR"):
        original = hw.exchange

        def exchange(port: str, command: str, timeout: float) -> str:
            if command == "VERSION" and hw.version.startswith("ERR"):
                return hw.version
            return original(port, command, timeout)

        hw.exchange = exchange  # type: ignore[method-assign]
    result, code = updater.ensure()
    assert code == 0
    assert result["result"] == ("current" if version == "new" else "updated")
    assert result["before"] == ("new" if version == "new" else "legacy")
    if version == "new":
        assert not hw.payloads


@pytest.mark.parametrize("case", ["signal", "deadline"])
def test_deadline_stops_without_reboot_and_cleans_work(rig: tuple[Any, Fake], case: str) -> None:
    updater, hw = rig
    original = hw.picotool

    def fail(args: list[str], timeout: float) -> str:
        if args[0] == "load" and len(hw.payloads) == 1:
            if case == "signal":
                fw.terminate(15, None)
            hw.now += 541
            updater.budget()
        return original(args, timeout)

    hw.picotool = fail  # type: ignore[method-assign]
    result, code = updater.ensure(boot=True)
    assert (result["result"], code) == ("failed", 1)
    assert hw.flash[:4096] == b"\xff" * 4096
    assert ["reboot", "-a"] not in hw.commands
    assert not updater.paths.work.exists()


@pytest.mark.parametrize("case", ["unwritable", "corrupt"])
def test_state_failure_prevents_flashing(rig: tuple[Any, Fake], case: str) -> None:
    updater, hw = rig
    if case == "unwritable":
        updater.paths.state.parent.write_text("not a directory")
    else:
        updater.paths.state.parent.mkdir()
        updater.paths.state.write_text("{invalid json")
    assert updater.ensure()[0]["result"] == "failed"
    assert not hw.payloads
    if case == "corrupt":
        assert updater.paths.state.read_text() == "{invalid json"


@pytest.mark.parametrize("reply", ["ERR STAT", "STAT power=OFF", ""])
def test_unknown_or_sleeping_face_is_not_powered_on(rig: tuple[Any, Fake], reply: str) -> None:
    updater, hw = rig
    original = hw.exchange

    def exchange(port: str, command: str, timeout: float) -> str:
        if command == "STAT":
            return reply
        return original(port, command, timeout)

    hw.exchange = exchange  # type: ignore[method-assign]
    assert updater.ensure()[0]["result"] == "updated"
    assert "POWER ON" not in hw.power


def test_new_declaration_resets_boot_failure_limit(rig: tuple[Any, Fake]) -> None:
    updater, hw = rig
    updater.save({"seen": True, "chip_id": hw.chip, "declared_version": "old", "consecutive_failures": 3})
    assert updater.ensure(boot=True)[0]["result"] == "updated"


@pytest.mark.parametrize("mode", ["boot", "return"])
def test_usb_enumeration_delay_is_waited(rig: tuple[Any, Fake], mode: str) -> None:
    updater, hw = rig
    original_cdc, original_usb = hw.cdc, hw.bootsel_count
    if mode == "boot":

        def cdc() -> list[tuple[str, str]]:
            return original_cdc() if hw.now >= 2 else []

        hw.cdc = cdc  # type: ignore[method-assign]
    else:

        def cdc() -> list[tuple[str, str]]:
            if hw.commands and hw.commands[-1] == ["reboot", "-a"] and hw.now < 2:
                return []
            return original_cdc()

        def usb() -> int:
            if hw.commands and hw.commands[-1] == ["reboot", "-a"] and hw.now < 2:
                return 1
            return original_usb()

        hw.cdc = cdc  # type: ignore[method-assign]
        hw.bootsel_count = usb  # type: ignore[method-assign]
    assert updater.ensure(boot=True)[0]["result"] == "updated"
    assert hw.now >= 2


def test_failed_reboot_uses_1200_baud_fallback(rig: tuple[Any, Fake]) -> None:
    updater, hw = rig
    original = hw.picotool

    def pico(args: list[str], timeout: float) -> str:
        if args == ["reboot", "-u", "-f"]:
            raise OSError("unsupported reboot")
        return original(args, timeout)

    hw.picotool = pico  # type: ignore[method-assign]
    assert updater.ensure()[0]["result"] == "updated"


def test_picotool_timeout_kills_descendants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import subprocess
    import time

    marker = tmp_path / "survived"
    stub = tmp_path / "picotool"
    stub.write_text('#!/bin/sh\n(sleep 0.4; touch "$MARKER") &\nwait\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("MARKER", str(marker))
    with pytest.raises(subprocess.TimeoutExpired):
        fw.Hardware().picotool(["info", "-d"], 0.1)
    time.sleep(0.5)
    assert not marker.exists()


def test_serial_events_do_not_hide_version(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    class Connection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.data = iter(b"EVT power=OFF\r\nVERSION 1.2.3+abcd\r\n")

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def reset_input_buffer(self) -> None:
            pass

        def write(self, data: bytes) -> None:
            assert data == b"VERSION\n"

        def read(self, size: int) -> bytes:
            return bytes([next(self.data)])

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=Connection))
    assert fw.Hardware().exchange("fake", "VERSION", 3) == "VERSION 1.2.3+abcd"


def test_cli_emits_one_json_line(
    rig: tuple[Any, Fake], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    updater, hw = rig
    monkeypatch.setattr(fw.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fw, "Paths", lambda: updater.paths)
    monkeypatch.setattr(fw, "Hardware", lambda: hw)
    monkeypatch.setattr(sys, "argv", ["palmimo-display-firmware", "ensure", "--json"])
    assert fw.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["result"] == "updated"


@pytest.mark.parametrize("case", ["oversized", "corrupt"])
def test_bad_compressed_image_never_reaches_flash(rig: tuple[Any, Fake], case: str) -> None:
    updater, hw = rig
    source = updater.paths.firmware / "face_serial.bin.gz"
    source.write_bytes(gzip.compress(b"X" * (12 * 1024 * 1024 + 1)) if case == "oversized" else b"not gzip")
    assert updater.ensure()[0]["result"] == "failed"
    assert not hw.payloads
    assert not updater.paths.work.exists()
