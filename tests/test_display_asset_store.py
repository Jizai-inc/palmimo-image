from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "platform/files/usr/local/libexec/palmimo/palmimo-display-firmware"
loader = importlib.machinery.SourceFileLoader("asset_store_firmware", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
fw = importlib.util.module_from_spec(spec)
loader.exec_module(fw)


def asset(pages: int = 1, value: int = 0) -> bytes:
    size = pages * 4096
    blob = bytearray(
        struct.pack("<4s4I3f12I", b"SPAF", 1, 1, size, 0, 30, 0, 1, 80, 0, 80, 0, 80, 0, 80, 0, 80, 0, 80, size - 80)
    )
    blob.extend(bytes([value]) * (size - 80))
    struct.pack_into("<I", blob, 16, zlib.crc32(blob))
    return bytes(blob)


class Flash:
    def __init__(self, pages: int = 1532) -> None:
        self.pages = pages
        self.assets: dict[str, tuple[int, bytes]] = {}
        self.commands: list[str] = []
        self.deleted: list[str] = []
        self.puts: list[str] = []
        self.clock = 0.0
        self.failure = ""
        self.v2 = True
        self.drops = 0
        self.max_ahead = 0
        self.short_writes = False

    def allocate(self, size: int) -> int | None:
        needed = (size + 4095) // 4096
        occupied = {p for start, data in self.assets.values() for p in range(start, start + (len(data) + 4095) // 4096)}
        for start in range(self.pages - needed + 1):
            if not occupied.intersection(range(start, start + needed)):
                return start
        return None

    def connect(self) -> Peer:
        return Peer(self)


class Peer:
    def __init__(self, flash: Flash) -> None:
        self.flash = flash
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.read_line = bytearray()
        self.pending: tuple[str, int, int, int] | None = None
        self.body = bytearray()
        self.delivered_ack = 0
        self.closed = False
        self._dtr = True

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = value
        if not value:
            self.flash.drops += 1
            self.pending = None
            self.incoming.clear()

    def close(self) -> None:
        if not self.closed:
            if self.dtr:
                self.dtr = False
            self.closed = True

    def send(self, line: str) -> None:
        self.outgoing.extend(("EVT TAP 1 2\n" + line + "\n").encode())

    def read(self, size: int) -> bytes:
        self.flash.clock += 0.001 if self.outgoing else 0.1
        chunk = bytes(self.outgoing[:size])
        del self.outgoing[:size]
        self.read_line.extend(chunk)
        if self.read_line.endswith(b"\n"):
            if self.read_line.startswith(b"ACK "):
                self.delivered_ack = int(self.read_line[4:])
            self.read_line.clear()
        return chunk

    def write(self, data: bytes) -> int:
        if self.flash.short_writes:
            data = data[:1024]
        if self.pending is None:
            self.incoming.extend(data)
            if self.incoming.endswith(b"\n"):
                line = self.incoming.decode().rstrip("\n")
                assert not line.endswith("\r")
                self.incoming.clear()
                self.command(line)
        else:
            name, count, crc, start = self.pending
            previous = len(self.body)
            self.body.extend(data)
            received = len(self.body)
            self.flash.max_ahead = max(self.flash.max_ahead, received - self.delivered_ack)
            assert received - self.delivered_ack <= 16384
            assert received <= count
            if self.flash.failure != "stall":
                acknowledgements = list(range((previous // 4096 + 1) * 4096, received + 1, 4096))
                if received == count and count % 4096:
                    acknowledgements.append(count)
                for ack in acknowledgements:
                    if self.flash.failure == "bad-ack" and ack == 4096:
                        self.send("ACK 999999")
                    elif self.flash.failure == "early-final" and ack == 4096:
                        self.send(f"OK ASSET {name}")
                    else:
                        self.send(f"ACK {ack}")
            if received == count:
                if self.flash.failure == "final":
                    self.send("ERR ASSET CRC")
                elif self.flash.failure not in ("stall", "final-stall"):
                    assert zlib.crc32(self.body) == crc
                    self.flash.assets[name] = (start, bytes(self.body))
                    self.flash.puts.append(name)
                    self.send(f"OK ASSET {name}")
                self.pending = None
        return len(data)

    def command(self, line: str) -> None:
        self.flash.commands.append(line)
        parts = line.split()
        if line == "HELLO":
            self.send("OK HELLO proto=2 fw=test caps=asset" if self.flash.v2 else "ERR HELLO (try LIST)")
        elif parts[:2] == ["ASSET", "LIST"]:
            index = int(parts[2]) if len(parts) == 3 else 0
            entries = sorted(self.flash.assets.items())
            # One entry per page exercises the same continuation contract as a full reply buffer.
            page = entries[index : index + 1]
            next_index = str(index + 1) if index + 1 < len(entries) else "-"
            values = " ".join(f"{name}:vector:{len(data)}:{zlib.crc32(data):08x}" for name, (_, data) in page)
            self.send(f"OK ASSET LIST next={next_index}" + (" " + values if values else ""))
        elif parts[:2] == ["ASSET", "INFO"]:
            name = parts[2]
            if name in self.flash.assets:
                _, data = self.flash.assets[name]
                self.send(
                    f"OK ASSET INFO {name} kind=vector bytes={len(data)} crc32={zlib.crc32(data):08x} next=- params=-"
                )
            else:
                self.send("ERR ASSET UNKNOWN")
        elif parts[:2] == ["ASSET", "DEL"]:
            name = parts[2]
            assert name in self.flash.assets
            del self.flash.assets[name]
            self.flash.deleted.append(name)
            self.send(f"OK ASSET DEL {name}")
        elif parts[:2] == ["ASSET", "PUT"]:
            name, count, crc = parts[2], int(parts[3]), int(parts[4], 16)
            start = self.flash.allocate(count)
            if start is None:
                self.send("ERR ASSET NOSPACE")
            else:
                self.body.clear()
                self.delivered_ack = 0
                self.pending = name, count, crc, start
                self.send(f"OK XFER BEGIN {count} ACK=4096")
        else:
            raise AssertionError(line)


class Hardware:
    def __init__(self, flash: Flash) -> None:
        self.flash = flash

    def cdc(self) -> list[tuple[str, str]]:
        return [("fake", "abcdef0123456789")]

    def bootsel_count(self) -> int:
        return 0

    def port_busy(self, port: str) -> bool:
        return False

    def monotonic(self) -> float:
        return self.flash.clock

    @contextlib.contextmanager
    def asset_connection(self, port: str) -> Any:
        peer = self.flash.connect()
        try:
            yield peer
        finally:
            peer.close()


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Flash]:
    paths = fw.Paths(assets=tmp_path / "state", catalog=tmp_path / "standard")
    paths.locks = tmp_path / "locks"
    paths.catalog.mkdir()
    (paths.catalog / "catalog.json").write_text('{"schema":1,"assets":[]}')
    flash = Flash()
    monkeypatch.setattr(fw, "Paths", lambda: paths)
    monkeypatch.setattr(fw, "Hardware", lambda: Hardware(flash))
    monkeypatch.setattr(fw.os, "geteuid", lambda: 0)
    return paths, flash


def invoke(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(sys, "argv", ["palmimo-display-firmware", "assets", *args])
    code = fw.main()
    return code, json.loads(capsys.readouterr().out)


def standard(paths: Any, name: str, data: bytes) -> None:
    catalog_path = paths.catalog / "catalog.json"
    catalog = json.loads(catalog_path.read_text())
    catalog["assets"].append({"id": f"palmimo/{name}", "description": name})
    catalog_path.write_text(json.dumps(catalog))
    (paths.catalog / f"{name}.bin").write_bytes(data)


def installed(paths: Any, app: str, name: str, data: bytes) -> None:
    directory = paths.assets / "apps" / app
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.bin").write_bytes(data)


def test_install_and_uninstall_preserve_other_apps(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    source = tmp_path / "source.bin"
    source.write_bytes(asset())
    installed(paths, "zip.other", "keep", asset())
    assert invoke(monkeypatch, capsys, "install", "zip.demo", "hello", str(source)) == (0, {"ok": True, "reason": None})
    assert (paths.assets / "apps/zip.demo/hello.bin").read_bytes() == source.read_bytes()
    assert (paths.assets / "apps/zip.demo/hello.bin").stat().st_mode & 0o777 == 0o600
    assert (paths.assets / "apps/zip.demo").stat().st_mode & 0o777 == 0o700
    assert invoke(monkeypatch, capsys, "uninstall", "zip.demo")[0] == 0
    assert not (paths.assets / "apps/zip.demo").exists()
    assert (paths.assets / "apps/zip.other/keep.bin").exists()
    assert not flash.commands


@pytest.mark.parametrize("name", ["../escape", "Upper", "include", "a" * 41, "-bad"])
def test_install_rejects_invalid_asset_names(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(asset())
    assert invoke(monkeypatch, capsys, "install", "zip.demo", "--", name, str(source))[0] != 0


@pytest.mark.parametrize("app", ["palmimo", "../bad", "zip.bad/name", "A.demo", "zip." + "a" * 41])
def test_asset_operations_reject_invalid_app_ids(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], app: str
) -> None:
    assert invoke(monkeypatch, capsys, "uninstall", app)[0] != 0
    assert invoke(monkeypatch, capsys, "sync", "--app", app, "--json")[0] != 0
    assert not rig[1].commands


@pytest.mark.parametrize("bad", [b"bad", b"SPAF", b"x" * 6_275_073])
def test_install_revalidates_and_preserves_previous_asset(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad: bytes,
) -> None:
    paths, _ = rig
    installed(paths, "zip.demo", "hello", asset())
    source = tmp_path / "source.bin"
    source.write_bytes(bad)
    assert invoke(monkeypatch, capsys, "install", "zip.demo", "hello", str(source))[0] != 0
    assert (paths.assets / "apps/zip.demo/hello.bin").read_bytes() == asset()


@pytest.mark.parametrize("location", ["source", "source-parent", "destination", "destination-parent"])
def test_install_never_follows_symlinks(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    location: str,
) -> None:
    paths, _ = rig
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "hello.bin"
    target.write_bytes(asset())
    source = tmp_path / "source.bin"
    source.write_bytes(asset(value=1))
    if location == "source":
        source.unlink()
        source.symlink_to(target)
    elif location == "source-parent":
        link = tmp_path / "source-link"
        link.symlink_to(outside, target_is_directory=True)
        source = link / "hello.bin"
    else:
        directory = paths.assets / "apps/zip.demo"
        directory.parent.mkdir(parents=True)
        if location == "destination-parent":
            directory.symlink_to(outside, target_is_directory=True)
        else:
            directory.mkdir()
            (directory / "hello.bin").symlink_to(target)
    assert invoke(monkeypatch, capsys, "install", "zip.demo", "hello", str(source))[0] != 0
    assert target.read_bytes() == asset()


def test_override_reset_uses_catalog_and_keeps_default(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    standard(paths, "happy", asset())
    source = tmp_path / "source.bin"
    source.write_bytes(asset(value=1))
    assert invoke(monkeypatch, capsys, "override", "happy", str(source))[0] == 0
    assert (paths.assets / "overrides/happy.bin").read_bytes() == source.read_bytes()
    assert (paths.catalog / "happy.bin").read_bytes() == asset()
    assert invoke(monkeypatch, capsys, "override", "--reset", "happy")[0] == 0
    assert not (paths.assets / "overrides/happy.bin").exists()
    assert invoke(monkeypatch, capsys, "override", "unknown", str(source))[0] != 0


@pytest.mark.parametrize("command", [["uninstall", "zip.demo"], ["sync", "--json"]])
def test_asset_commands_require_root(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    monkeypatch.setattr(fw.os, "geteuid", lambda: 1000)
    assert invoke(monkeypatch, capsys, *command)[0] != 0
    assert not rig[1].commands


def test_sync_empty_standard_set_is_supported(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert invoke(monkeypatch, capsys, "sync", "--json") == (
        0,
        {"result": "synced", "put": [], "deleted": [], "reason": None},
    )


def test_sync_updates_only_missing_or_different_assets(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    standard(paths, "happy", asset())
    standard(paths, "sleep", asset(value=1))
    installed(paths, "zip.demo", "welcome", asset(6, 2))
    flash.assets = {"palmimo/happy": (0, asset()), "palmimo/sleep": (1, asset()), "zip.other/keep": (2, asset())}
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code == 0, result
    assert set(result["put"]) == {"palmimo/sleep", "zip.demo/welcome"}
    assert not result["deleted"]
    assert flash.assets["zip.demo/welcome"][1] == asset(6, 2)
    assert flash.max_ahead == 16384
    assert invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")[1]["put"] == []
    launch = json.loads((paths.assets / "launch.json").read_text())
    assert launch["sequence"] == 2 and launch["apps"] == {"zip.demo": 2}


@pytest.mark.parametrize("mode", ["busy", "v1"])
def test_sync_busy_or_wrong_protocol_does_not_modify_flash(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    paths, flash = rig
    if mode == "busy":
        with fw.reservation(paths.locks):
            code, result = invoke(monkeypatch, capsys, "sync", "--json")
        assert (code, result["result"]) == (75, "busy")
        assert not flash.commands
    else:
        flash.v2 = False
        assert invoke(monkeypatch, capsys, "sync", "--json")[1]["result"] == "skipped"
        assert flash.commands == ["HELLO"]
    assert not flash.puts and not flash.deleted
    assert not paths.assets.exists()


def test_sync_evicts_oldest_other_app_first(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    flash.pages = 6
    standard(paths, "happy", asset())
    installed(paths, "zip.current", "new", asset(3))
    flash.assets = {"palmimo/happy": (0, asset()), "zip.older/a": (1, asset(3)), "zip.newer/b": (4, asset(2))}
    (paths.assets / "launch.json").write_text(
        json.dumps({"schema": 1, "sequence": 2, "apps": {"zip.older": 1, "zip.newer": 2}})
    )
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.current", "--json")
    assert code == 0, result
    assert result["deleted"] == ["zip.older/a"]
    assert "zip.newer/b" in flash.assets


def test_sync_repacks_current_app_after_fragmentation(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    flash.pages = 14
    standard(paths, "happy", asset())
    for name, pages in (("a", 4), ("b", 2), ("c", 2)):
        installed(paths, "zip.demo", name, asset(pages))
    flash.assets = {
        "palmimo/happy": (0, asset()),
        "zip.demo/a": (1, asset(2)),
        "zip.demo/b": (6, asset(2)),
        "zip.demo/c": (11, asset(2)),
    }
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code == 0, result
    assert set(result["deleted"]) == {"zip.demo/a", "zip.demo/b", "zip.demo/c"}
    assert set(result["put"]) == {"zip.demo/a", "zip.demo/b", "zip.demo/c"}
    assert flash.assets["zip.demo/a"][1] == asset(4)
    assert "palmimo/happy" not in flash.deleted


@pytest.mark.parametrize("failure", ["stall", "final", "bad-ack", "early-final", "final-stall"])
def test_transfer_failure_preserves_old_flash_asset(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    paths, flash = rig
    installed(paths, "zip.demo", "a", asset(6, 1))
    flash.assets = {"zip.demo/a": (0, asset())}
    flash.failure = failure
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code != 0 and result["result"] == "failed"
    assert not result["put"]
    assert flash.assets["zip.demo/a"][1] == asset()
    assert flash.drops >= 1
    if failure == "stall":
        assert flash.clock < 4


@pytest.mark.parametrize("short_writes", [False, True])
def test_xfer_handles_final_partial_block_and_short_writes(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], short_writes: bool
) -> None:
    paths, flash = rig
    data = bytearray(asset(6)[:-4])
    struct.pack_into("<I", data, 12, len(data))
    struct.pack_into("<I", data, 76, len(data) - 80)
    struct.pack_into("<I", data, 16, 0)
    struct.pack_into("<I", data, 16, zlib.crc32(data))
    installed(paths, "zip.demo", "a", bytes(data))
    flash.short_writes = short_writes
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code == 0, result
    assert flash.assets["zip.demo/a"][1] == bytes(data)
    assert flash.max_ahead <= 16384


def test_install_failed_replace_keeps_previous_asset(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    installed(paths, "zip.demo", "hello", asset())
    source = tmp_path / "source.bin"
    source.write_bytes(asset(value=1))

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("Disk failure")

    monkeypatch.setattr(fw.os, "replace", fail)
    assert invoke(monkeypatch, capsys, "install", "zip.demo", "hello", str(source))[0] != 0
    assert (paths.assets / "apps/zip.demo/hello.bin").read_bytes() == asset()


def test_install_accepts_identifier_boundaries(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    app, name = "1" * 40 + "." + "a" * 40, "a" * 40
    source = tmp_path / "source.bin"
    source.write_bytes(asset())
    assert invoke(monkeypatch, capsys, "install", app, name, str(source))[0] == 0
    assert (paths.assets / "apps" / app / f"{name}.bin").read_bytes() == asset()


def test_sync_prefers_override_and_reset_restores_default(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    standard(paths, "happy", asset())
    source = tmp_path / "source.bin"
    source.write_bytes(asset(value=1))
    assert invoke(monkeypatch, capsys, "override", "happy", str(source))[0] == 0
    assert invoke(monkeypatch, capsys, "sync", "--json")[0] == 0
    assert flash.assets["palmimo/happy"][1] == source.read_bytes()
    assert invoke(monkeypatch, capsys, "override", "--reset", "happy")[0] == 0
    assert invoke(monkeypatch, capsys, "sync", "--json")[0] == 0
    assert flash.assets["palmimo/happy"][1] == asset()


@pytest.mark.parametrize("location", ["app", "override", "launch"])
def test_sync_rejects_symlinked_local_state(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    location: str,
) -> None:
    paths, flash = rig
    outside = tmp_path / "outside.bin"
    outside.write_bytes(asset())
    installed(paths, "zip.demo", "a", asset())
    if location == "app":
        link = paths.assets / "apps/zip.demo/a.bin"
        link.unlink()
    elif location == "override":
        standard(paths, "happy", asset())
        link = paths.assets / "overrides/happy.bin"
        link.parent.mkdir()
    else:
        link = paths.assets / "launch.json"
    link.symlink_to(outside)
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code != 0 and result["result"] == "failed"
    assert not flash.puts and not flash.deleted
    assert outside.read_bytes() == asset()


def test_sync_with_no_app_does_not_advance_launch_sequence(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    installed(paths, "zip.demo", "a", asset())
    assert invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")[0] == 0
    previous = (paths.assets / "launch.json").read_bytes()
    assert invoke(monkeypatch, capsys, "sync", "--json")[0] == 0
    assert (paths.assets / "launch.json").read_bytes() == previous


@pytest.mark.parametrize("state", ["launch", "catalog", "capacity"])
def test_sync_rejects_invalid_local_plan_before_flash_changes(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    paths, flash = rig
    installed(paths, "zip.demo", "a", asset())
    if state == "launch":
        (paths.assets / "launch.json").write_text('{"schema":1,"sequence":true,"apps":{}}')
    elif state == "catalog":
        (paths.catalog / "catalog.json").write_text('{"schema":1,"assets":[{"id":"zip.other/a"}]}')
    else:
        for i in range(6):
            installed(paths, "zip.demo", f"large{i}", asset(256))
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code != 0 and result["result"] == "failed"
    assert not flash.puts and not flash.deleted


@pytest.mark.parametrize(
    "reply",
    [
        "OK ASSET LIST next=1",
        "OK ASSET LIST next=0 zip.demo/a:vector:4096:00000000",
        "OK ASSET LIST next=- ../x:vector:4096:00000000",
        "OK ASSET LIST next=- zip.demo/a:vector:4096:bad",
    ],
)
def test_sync_rejects_malformed_flash_listing(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reply: str
) -> None:
    paths, flash = rig
    installed(paths, "zip.demo", "a", asset())
    original = Peer.command

    def command(peer: Peer, line: str) -> None:
        if line.startswith("ASSET LIST"):
            peer.send(reply)
        else:
            original(peer, line)

    monkeypatch.setattr(Peer, "command", command)
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code != 0 and result["result"] == "failed"
    assert not flash.puts and not flash.deleted


def test_sync_recovers_standard_fragmentation_by_repacking(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    flash.pages = 11
    standard(paths, "left", asset())
    standard(paths, "right", asset())
    installed(paths, "zip.demo", "a", asset(4))
    flash.assets = {"palmimo/left": (3, asset()), "palmimo/right": (7, asset())}
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    # Without recovery, a capacity-valid collection can remain permanently unsynchronizable.
    assert code == 0 and result["result"] == "synced"
    assert set(flash.assets) == {"palmimo/left", "palmimo/right", "zip.demo/a"}
    assert all(data == (asset(4) if name == "zip.demo/a" else asset()) for name, (_, data) in flash.assets.items())


def test_sync_removes_obsolete_standard_assets(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without this, bundle renames leave flash occupancy outside the capacity calculation.
    paths, flash = rig
    standard(paths, "new", asset())
    flash.assets = {"palmimo/old": (0, asset())}
    code, result = invoke(monkeypatch, capsys, "sync", "--json")
    assert code == 0 and result["result"] == "synced"
    assert set(flash.assets) == {"palmimo/new"}


def test_sync_reports_exhausted_repack(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without this, a NOSPACE response after recovery loses its machine-readable failure stage.
    paths, flash = rig
    flash.pages = 0
    installed(paths, "zip.demo", "a", asset())
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code == 1 and result["result"] == "failed"
    assert result["reason"] == "insufficient_contiguous_space_after_repacking"


@pytest.mark.parametrize("link_type", ["directory", "entry"])
def test_uninstall_does_not_follow_symlinks(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    link_type: str,
) -> None:
    paths, _ = rig
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.bin"
    target.write_bytes(asset())
    app = paths.assets / "apps/zip.demo"
    app.parent.mkdir(parents=True)
    if link_type == "directory":
        app.symlink_to(outside, target_is_directory=True)
    else:
        app.mkdir()
        (app / "keep.bin").symlink_to(target)
    code, _result = invoke(monkeypatch, capsys, "uninstall", "zip.demo")
    assert (code == 0) is (link_type == "entry")
    assert target.read_bytes() == asset()


def test_sync_includes_system_assets_absent_from_vocabulary(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    for name in ("system.idle", "system.sleep", "system.wake"):
        (paths.catalog / f"{name}.bin").write_bytes(asset())
    code, result = invoke(monkeypatch, capsys, "sync", "--json")
    assert code == 0, result
    assert set(result["put"]) == {"palmimo/system.idle", "palmimo/system.sleep", "palmimo/system.wake"}
    assert set(flash.assets) == set(result["put"])
    assert json.loads((paths.catalog / "catalog.json").read_text())["assets"] == []


@pytest.mark.parametrize("name", ["system.idle", "system.sleep", "system.wake"])
def test_override_accepts_system_assets_absent_from_vocabulary(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    paths, flash = rig
    (paths.catalog / f"{name}.bin").write_bytes(asset())
    source = tmp_path / "source.bin"
    source.write_bytes(asset(value=1))
    assert invoke(monkeypatch, capsys, "override", name, str(source))[0] == 0
    assert invoke(monkeypatch, capsys, "sync", "--json")[0] == 0
    assert flash.assets[f"palmimo/{name}"][1] == source.read_bytes()
    assert invoke(monkeypatch, capsys, "override", "--reset", name)[0] == 0
    assert invoke(monkeypatch, capsys, "sync", "--json")[0] == 0
    assert flash.assets[f"palmimo/{name}"][1] == asset()


def test_capacity_includes_system_assets_absent_from_vocabulary(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    (paths.catalog / "system.idle.bin").write_bytes(asset(256))
    for i in range(4):
        installed(paths, "zip.demo", f"part{i}", asset(256))
    code, result = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert code != 0 and result["result"] == "failed"
    assert not flash.puts and not flash.deleted


@pytest.mark.parametrize("kind", ["symlink", "directory", "invalid-name"])
def test_sync_rejects_invalid_standard_directory_entries(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    paths, flash = rig
    target = paths.catalog / "system.idle.bin"
    if kind == "symlink":
        source = tmp_path / "outside.bin"
        source.write_bytes(asset())
        target.symlink_to(source)
    elif kind == "directory":
        target.mkdir()
    else:
        (paths.catalog / "Upper.bin").write_bytes(asset())
    assert invoke(monkeypatch, capsys, "sync", "--json")[1]["result"] == "failed"
    assert not flash.puts and not flash.deleted


def test_shipped_standard_assets_sync_as_complete_set(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, flash = rig
    paths.catalog = SCRIPT.parents[2] / "share/palmimo/display-assets"
    catalog = json.loads((paths.catalog / "catalog.json").read_text())
    vocabulary = {entry["id"] for entry in catalog["assets"]}
    files = {f"palmimo/{path.stem}": path for path in paths.catalog.glob("*.bin")}
    systems = {"palmimo/system.idle", "palmimo/system.sleep", "palmimo/system.wake"}
    assert len(catalog["assets"]) == len(vocabulary) == 14
    assert len(files) == 17
    assert set(files) - vocabulary == systems
    assert vocabulary <= files.keys()
    code, result = invoke(monkeypatch, capsys, "sync", "--json")
    assert code == 0, result
    assert set(result["put"]) == set(files)
    assert {name: data for name, (_, data) in flash.assets.items()} == {
        name: path.read_bytes() for name, path in files.items()
    }


def test_status_reports_effective_defaults_apps_and_orphans(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    paths, flash = rig
    source = tmp_path / "neon.bin"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT.with_name("palmimo-display-assets")),
            "convert",
            str(Path(__file__).parent / "fixtures/display-assets/happy_neon.json"),
            "-o",
            str(source),
            "--json",
        ],
        check=True,
        capture_output=True,
    )
    standard(paths, "happy", source.read_bytes())
    (paths.catalog / "system.idle.bin").write_bytes(asset())
    stream = struct.pack("<16H", *([0xFFFF, 0] * 7 + [0x83FF, 0]))
    body = struct.pack("<20sHHII", b"IMAGE", 1, 0, 64, 0) + struct.pack("<3I", 76, 16, 0) + stream
    image = struct.pack("<4sHHII16x", b"PIMG", 2, 1, 32 + len(body), zlib.crc32(body)) + body
    installed(paths, "zip.first", "a", asset())
    installed(paths, "zip.first", "b", asset(4))
    installed(paths, "zip.second", "c", asset(2))
    overrides = paths.assets / "overrides"
    overrides.mkdir()
    (overrides / "happy.bin").write_bytes(image)
    (overrides / "removed.bin").write_bytes(asset())
    code, result = invoke(monkeypatch, capsys, "status", "--json")
    assert code == 0, result
    assert result["capacity"] == 6275072
    assert result["standard"][0] == {
        "name": "happy",
        "bytes": len(image),
        "kind": "image",
        "params": [],
        "overridden": True,
        "default_bytes": source.stat().st_size,
        "default_kind": "vector",
        "default_params": ["eye_color"],
    }
    assert result["standard"][1]["name"] == "system.idle"
    assert result["standard"][1]["overridden"] is False
    assert result["apps"] == {
        "zip.first": {"bytes": 20480, "largest": 16384, "names": ["a", "b"]},
        "zip.second": {"bytes": 8192, "largest": 8192, "names": ["c"]},
    }
    assert result["orphan_overrides"] == ["removed"]
    assert not flash.commands


@pytest.mark.parametrize("damage", ["unexpected-file", "symlink", "bad-default", "bad-override"])
def test_status_rejects_invalid_store_content(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], damage: str
) -> None:
    paths, flash = rig
    standard(paths, "happy", asset())
    installed(paths, "zip.demo", "a", asset())
    if damage == "unexpected-file":
        (paths.assets / "apps/zip.demo/extra.txt").write_text("x")
    elif damage == "symlink":
        (paths.assets / "apps/zip.demo/link.bin").symlink_to(paths.catalog / "happy.bin")
    elif damage == "bad-default":
        (paths.catalog / "happy.bin").write_bytes(b"bad")
    else:
        (paths.assets / "overrides").mkdir()
        (paths.assets / "overrides/happy.bin").write_bytes(b"bad")
    code, result = invoke(monkeypatch, capsys, "status", "--json")
    assert code == 1 and result["ok"] is False
    assert not flash.commands


@pytest.mark.parametrize("case", ["new", "changed", "empty"])
def test_replace_publishes_complete_app_collection(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    paths, flash = rig
    installed(paths, "zip.other", "keep", asset())
    if case != "new":
        installed(paths, "zip.demo", "a", asset())
        installed(paths, "zip.demo", "b", asset())
    source = tmp_path / "staging"
    source.mkdir()
    expected = {} if case == "empty" else {"a.bin": asset(2, 1), "c.bin": asset(3, 2)}
    for name, data in expected.items():
        (source / name).write_bytes(data)
    assert invoke(monkeypatch, capsys, "replace", "zip.demo", str(source)) == (0, {"ok": True, "reason": None})
    target = paths.assets / "apps/zip.demo"
    assert {p.name: p.read_bytes() for p in target.glob("*")} == expected
    assert target.exists() is bool(expected)
    assert (paths.assets / "apps/zip.other/keep.bin").read_bytes() == asset()
    assert not flash.commands


@pytest.mark.parametrize("damage", ["bad-bin", "symlink", "subdirectory", "non-bin", "source-symlink"])
def test_replace_invalid_candidate_preserves_collection(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: str,
) -> None:
    paths, _ = rig
    installed(paths, "zip.demo", "old", asset())
    source = tmp_path / "staging"
    source.mkdir()
    (source / "good.bin").write_bytes(asset())
    if damage == "bad-bin":
        (source / "bad.bin").write_bytes(b"bad")
    elif damage == "symlink":
        (source / "link.bin").symlink_to(source / "good.bin")
    elif damage == "subdirectory":
        (source / "nested.bin").mkdir()
    elif damage == "non-bin":
        (source / "extra.txt").write_text("x")
    else:
        link = tmp_path / "link"
        link.symlink_to(source, target_is_directory=True)
        source = link
    code, result = invoke(monkeypatch, capsys, "replace", "zip.demo", str(source))
    assert code == 1 and result["ok"] is False
    assert {p.name: p.read_bytes() for p in (paths.assets / "apps/zip.demo").glob("*")} == {"old.bin": asset()}


def test_replace_capacity_failure_reports_shortfall_without_mutation(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    (paths.catalog / "system.idle.bin").write_bytes(asset(256))
    installed(paths, "zip.demo", "old", asset())
    source = tmp_path / "staging"
    source.mkdir()
    for i in range(4):
        (source / f"part{i}.bin").write_bytes(asset(256))
    code, result = invoke(monkeypatch, capsys, "replace", "zip.demo", str(source))
    assert code == 1 and result["ok"] is False and result["short_by"] == 16384
    assert (paths.assets / "apps/zip.demo/old.bin").read_bytes() == asset()


def test_interrupted_replace_restores_previous_collection_and_can_be_retried(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    installed(paths, "zip.demo", "old", asset())
    source = tmp_path / "staging"
    source.mkdir()
    (source / "new.bin").write_bytes(asset(value=1))
    rename = fw.os.rename

    def interrupt(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if str(src).endswith(".replace-zip.demo"):
            raise OSError("Interrupted before new collection publication")
        rename(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fw.os, "rename", interrupt)
        assert invoke(monkeypatch, capsys, "replace", "zip.demo", str(source))[0] == 1
    assert not (paths.assets / "apps/zip.demo").exists()
    # Without rollback, a power loss between renames silently discards the complete old collection.
    code, status = invoke(monkeypatch, capsys, "status", "--json")
    assert code == 0 and status["apps"]["zip.demo"]["names"] == ["old"]
    assert (paths.assets / "apps/zip.demo/old.bin").read_bytes() == asset()
    sync_code, synced = invoke(monkeypatch, capsys, "sync", "--app", "zip.demo", "--json")
    assert sync_code == 0 and synced["put"] == ["zip.demo/old"]
    assert not any(p.name.startswith((".replace-", ".old-")) for p in (paths.assets / "apps").iterdir())
    assert invoke(monkeypatch, capsys, "replace", "zip.demo", str(source))[0] == 0
    assert (paths.assets / "apps/zip.demo/new.bin").read_bytes() == asset(value=1)


@pytest.mark.parametrize("action", ["replace", "uninstall", "status"])
def test_store_entries_clean_abandoned_collections(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    paths, _ = rig
    for name in (".replace-zip.old", ".old-zip.old"):
        directory = paths.assets / "apps" / name
        directory.mkdir(parents=True)
        (directory / "a.bin").write_bytes(asset())
    source = tmp_path / "staging"
    source.mkdir()
    args = (
        ("replace", "zip.demo", str(source))
        if action == "replace"
        else ("uninstall", "zip.demo")
        if action == "uninstall"
        else ("status", "--json")
    )
    assert invoke(monkeypatch, capsys, *args)[0] == 0
    assert not list((paths.assets / "apps").glob(".*"))


@pytest.mark.parametrize("app", [".replace-x", ".replace-zip.demo", ".old-zip.demo"])
def test_replace_rejects_reserved_work_names(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    app: str,
) -> None:
    source = tmp_path / "staging"
    source.mkdir()
    assert invoke(monkeypatch, capsys, "replace", app, str(source))[0] == 1


def test_override_checks_all_apps_and_reports_maximum_shortfall(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    standard(paths, "happy", asset())
    for app in ("zip.first", "zip.second"):
        for i in range(4):
            installed(paths, app, f"part{i}", asset(256))
    installed(paths, "zip.second", "half", asset(128))
    (paths.assets / "overrides").mkdir()
    target = paths.assets / "overrides/happy.bin"
    target.write_bytes(asset(value=1))
    source = tmp_path / "source.bin"
    source.write_bytes(asset(256))
    code, result = invoke(monkeypatch, capsys, "override", "happy", str(source))
    assert code == 1 and result["ok"] is False and result["short_by"] == 540672
    assert target.read_bytes() == asset(value=1)


def test_override_reset_removes_orphan(
    rig: tuple[Any, Flash], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = rig
    directory = paths.assets / "overrides"
    directory.mkdir(parents=True)
    target = directory / "removed.bin"
    target.write_bytes(asset())
    assert invoke(monkeypatch, capsys, "override", "--reset", "removed") == (0, {"ok": True, "reason": None})
    assert not target.exists()


@contextlib.contextmanager
def held_store_lock(paths: Any) -> Any:
    import subprocess

    paths.assets.mkdir(parents=True, exist_ok=True)
    program = "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); fcntl.flock(fd,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read(1)"
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(paths.assets / ".lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stdin is not None
    try:
        assert child.stdout.readline().strip() == "ready"
        yield child
    finally:
        if child.poll() is None:
            try:
                with contextlib.suppress(BrokenPipeError):
                    child.stdin.write("x")
                    child.stdin.flush()
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
        with contextlib.suppress(BrokenPipeError):
            child.stdin.close()
        child.stdout.close()


def test_replace_waits_for_other_process_then_publishes(
    rig: tuple[Any, Flash], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import threading

    paths, _ = rig
    installed(paths, "zip.demo", "old", asset())
    source = tmp_path / "staging"
    source.mkdir()
    (source / "new.bin").write_bytes(asset(value=1))
    observed = []
    with held_store_lock(paths) as child:

        def release() -> None:
            observed.append((paths.assets / "apps/zip.demo/old.bin").read_bytes())
            child.stdin.write("x")
            child.stdin.flush()

        timer = threading.Timer(0.2, release)
        timer.start()
        try:
            code, result = invoke(monkeypatch, capsys, "replace", "zip.demo", str(source))
        finally:
            timer.join()
    assert code == 0, result
    assert observed == [asset()]
    assert (paths.assets / "apps/zip.demo/new.bin").read_bytes() == asset(value=1)
    assert not (paths.assets / "apps/zip.demo/old.bin").exists()


@pytest.mark.parametrize("action", ["replace", "uninstall", "override", "status", "install", "sync"])
def test_store_lock_timeout_and_sync_nonblocking_return_75(
    rig: tuple[Any, Flash],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    import time

    paths, flash = rig
    standard(paths, "happy", asset())
    installed(paths, "zip.demo", "old", asset())
    source = tmp_path / "staging"
    source.mkdir()
    candidate = source / "new.bin"
    candidate.write_bytes(asset())
    arguments = {
        "replace": ["replace", "zip.demo", str(source)],
        "uninstall": ["uninstall", "zip.demo"],
        "override": ["override", "happy", str(candidate)],
        "status": ["status", "--json"],
        "install": ["install", "zip.demo", "new", str(candidate)],
        "sync": ["sync", "--json"],
    }
    now = 0.0

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    with held_store_lock(paths), monkeypatch.context() as patch:
        patch.setattr(time, "monotonic", lambda: now)
        patch.setattr(time, "sleep", sleep)
        code, result = invoke(monkeypatch, capsys, *arguments[action])
    assert code == 75
    assert now == pytest.approx(0 if action == "sync" else 120)
    assert (result["result"] == "busy") if action == "sync" else (result["ok"] is False)
    assert (paths.assets / "apps/zip.demo/old.bin").read_bytes() == asset()
    assert not flash.puts and not flash.deleted
