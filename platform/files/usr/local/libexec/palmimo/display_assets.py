# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jizai Inc.
"""Root-owned asset storage and windowed display protocol v2 synchronization."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any


CAPACITY = 6_275_072
APP = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\.[a-z][a-z0-9-]{0,39}")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
CRC = re.compile(r"[0-9a-f]{8}")


class StoreError(Exception):
    pass


class NoSpaceError(StoreError):
    pass


class BusyError(StoreError):
    pass


class CapacityError(StoreError):
    def __init__(self, short_by: int) -> None:
        super().__init__("Assets exceed display capacity including replacement space")
        self.short_by = short_by


def shortfall(standard: dict[str, bytes], app: dict[str, bytes]) -> int:
    sizes = [(len(data) + 4095) // 4096 * 4096 for data in (*standard.values(), *app.values())]
    return max(0, sum(sizes) + max(sizes, default=0) - CAPACITY)


def remove_collection(path: Path) -> None:
    try:
        with directory(path):
            pass
    except FileNotFoundError:
        return
    with directory(path.parent) as parent:
        shutil.rmtree(path.name, dir_fd=parent)
        os.fsync(parent)


def app_id(value: str) -> str:
    if APP.fullmatch(value) is None:
        raise StoreError("Invalid app ID")
    return value


def asset_name(value: str) -> str:
    if NAME.fullmatch(value) is None or value == "include":
        raise StoreError("Invalid asset name")
    return value


def identifier(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2:
        raise StoreError("Invalid wire asset ID")
    owner, name = parts
    if owner != "palmimo":
        app_id(owner)
    asset_name(name)
    if len(value) > 127:
        raise StoreError("Asset ID exceeds 127 bytes")
    return owner, name


@contextlib.contextmanager
def directory(path: Path, create: bool = False) -> Iterator[int]:
    path = path.absolute()
    if ".." in path.parts:
        raise StoreError("Parent traversal is not permitted")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def regular_at(fd: int, name: str, missing: bool = False) -> bool:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing:
            return False
        raise
    if not stat.S_ISREG(info.st_mode):
        raise StoreError("Expected a regular file without symlinks")
    return True


def read_file(path: Path, limit: int = CAPACITY) -> bytes:
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise StoreError("Input must be a bounded regular file")
            data = stream.read(limit + 1)
    if len(data) > limit:
        raise StoreError("Input exceeds size limit")
    return data


def atomic_file(path: Path, data: bytes) -> None:
    with directory(path.parent, create=True) as parent:
        regular_at(parent, path.name, missing=True)
        # The private parent and store lock prevent another caller replacing this directory.
        fd, temporary = tempfile.mkstemp(prefix=".asset-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(Path(temporary).name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(Path(temporary).name, dir_fd=parent)


class Store:
    def __init__(self, root: Path, catalog: Path) -> None:
        self.root, self.catalog = root, catalog
        loader = importlib.machinery.SourceFileLoader(
            "asset_inspector", str(Path(__file__).with_name("palmimo-display-assets"))
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        self.converter = importlib.util.module_from_spec(spec)
        loader.exec_module(self.converter)

    @contextlib.contextmanager
    def transaction(self, wait_s: float = 0) -> Iterator[None]:
        with directory(self.root, create=True) as parent:
            fd = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise StoreError("Asset store lock must be a regular file")
                end = time.monotonic() + wait_s
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as error:
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            raise BusyError("Asset store is busy") from error
                        time.sleep(min(0.1, remaining))
                yield
            finally:
                os.close(fd)

    def inspect(self, data: bytes) -> dict[str, Any]:
        try:
            return self.converter.inspect(data)
        except ValueError as error:
            raise StoreError(f"Invalid asset: {error}") from error

    def standards(self) -> list[str]:
        catalog = json.loads(read_file(self.catalog / "catalog.json", 1_048_576))
        if (
            not isinstance(catalog, dict)
            or type(catalog.get("schema")) is not int
            or catalog["schema"] != 1
            or not isinstance(catalog.get("assets"), list)
        ):
            raise StoreError("Invalid standard catalog")
        names = []
        with directory(self.catalog) as parent:
            for filename in sorted(os.listdir(parent)):
                if filename.endswith(".bin"):
                    regular_at(parent, filename)
                    names.append(asset_name(filename[:-4]))
        seen: set[str] = set()
        for entry in catalog["assets"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise StoreError("Invalid standard catalog entry")
            owner, name = identifier(entry["id"])
            if owner != "palmimo" or name in seen or name not in names:
                raise StoreError("Catalog IDs must be a unique subset of the standard assets")
            seen.add(name)
        return names

    def blob_names(self, path: Path, missing: bool = False) -> list[str]:
        try:
            with directory(path) as parent:
                result = []
                for filename in sorted(os.listdir(parent)):
                    if not filename.endswith(".bin"):
                        raise StoreError("Unexpected entry in asset directory")
                    regular_at(parent, filename)
                    result.append(asset_name(filename[:-4]))
                return result
        except FileNotFoundError:
            if missing:
                return []
            raise

    def blobs(self, path: Path, missing: bool = False) -> dict[str, bytes]:
        result = {}
        for name in self.blob_names(path, missing):
            data = read_file(path / f"{name}.bin")
            self.inspect(data)
            result[name] = data
        return result

    def standard_files(self) -> dict[str, bytes]:
        result = {}
        for name in self.standards():
            try:
                data = read_file(self.root / "overrides" / f"{name}.bin")
            except FileNotFoundError:
                data = read_file(self.catalog / f"{name}.bin")
            self.inspect(data)
            result[name] = data
        return result

    def app_names(self) -> list[str]:
        try:
            with directory(self.root / "apps") as parent:
                names = [app_id(name) for name in sorted(os.listdir(parent)) if not name.startswith(".")]
            for name in names:
                with directory(self.root / "apps" / name):
                    pass
            return names
        except FileNotFoundError:
            return []

    def cleanup(self) -> None:
        try:
            with directory(self.root / "apps") as parent:
                names = sorted(os.listdir(parent))
        except FileNotFoundError:
            return
        for name in names:
            if name.startswith((".replace-", ".old-")):
                remove_collection(self.root / "apps" / name)

    def install(self, app: str, name: str, source: Path) -> None:
        app_id(app)
        asset_name(name)
        data = read_file(source)
        self.inspect(data)
        with self.transaction(wait_s=120):
            atomic_file(self.root / "apps" / app / f"{name}.bin", data)

    def uninstall(self, app: str) -> None:
        app_id(app)
        with self.transaction(wait_s=120):
            self.cleanup()
            remove_collection(self.root / "apps" / app)

    def replace(self, app: str, source: Path) -> None:
        app_id(app)
        candidate = self.blobs(source)
        shortage = shortfall(self.standard_files(), candidate)
        if shortage:
            raise CapacityError(shortage)
        with self.transaction(wait_s=120):
            self.cleanup()
            # A waiting writer must not rely on the override snapshot from before the lock.
            shortage = shortfall(self.standard_files(), candidate)
            if shortage:
                raise CapacityError(shortage)
            apps = self.root / "apps"
            if not candidate:
                remove_collection(apps / app)
                return
            staging, previous = f".replace-{app}", f".old-{app}"
            with directory(apps, create=True) as parent:
                try:
                    info = os.stat(app, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    existing = False
                else:
                    if not stat.S_ISDIR(info.st_mode):
                        raise StoreError("Existing app collection must be a directory without symlinks")
                    with directory(apps / app):
                        pass
                    existing = True
                with directory(apps / staging, create=True) as stage:
                    for name, data in candidate.items():
                        atomic_file(apps / staging / f"{name}.bin", data)
                    os.fsync(stage)
                if existing:
                    os.rename(app, previous, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                os.rename(staging, app, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                if existing:
                    remove_collection(apps / previous)

    def override(self, name: str, source: Path | None) -> None:
        asset_name(name)
        data = read_file(source) if source is not None else None
        if data is not None:
            self.inspect(data)
        with self.transaction(wait_s=120):
            target = self.root / "overrides" / f"{name}.bin"
            if data is not None:
                standard = self.standard_files()
                if name not in standard:
                    raise StoreError("Unknown standard asset")
                standard[name] = data
                shortages = [shortfall(standard, self.blobs(self.root / "apps" / app)) for app in self.app_names()]
                shortage = max(shortages, default=shortfall(standard, {}))
                if shortage:
                    raise CapacityError(shortage)
                atomic_file(target, data)
            else:
                try:
                    with directory(target.parent) as parent:
                        if regular_at(parent, target.name, missing=True):
                            os.unlink(target.name, dir_fd=parent)
                            os.fsync(parent)
                except FileNotFoundError:
                    pass

    def status(self) -> dict[str, Any]:
        with self.transaction(wait_s=120):
            self.cleanup()
            standard = []
            names = self.standards()
            for name in names:
                default_data = read_file(self.catalog / f"{name}.bin")
                default_meta = self.inspect(default_data)
                try:
                    data = read_file(self.root / "overrides" / f"{name}.bin")
                except FileNotFoundError:
                    data, overridden = default_data, False
                else:
                    overridden = True
                meta = self.inspect(data)
                standard.append(
                    {
                        "name": name,
                        "bytes": len(data),
                        "kind": meta["kind"],
                        "params": sorted(meta["params"]),
                        "overridden": overridden,
                        "default_bytes": len(default_data),
                        "default_kind": default_meta["kind"],
                        "default_params": sorted(default_meta["params"]),
                    }
                )
            apps = {}
            for app in self.app_names():
                files = self.blobs(self.root / "apps" / app)
                sizes = [(len(data) + 4095) // 4096 * 4096 for data in files.values()]
                apps[app] = {"bytes": sum(sizes), "largest": max(sizes, default=0), "names": list(files)}
            orphans = [name for name in self.blob_names(self.root / "overrides", missing=True) if name not in names]
            return {"capacity": CAPACITY, "standard": standard, "apps": apps, "orphan_overrides": orphans}

    def desired(self, app: str | None) -> dict[str, bytes]:
        standard = self.standard_files()
        candidate = self.blobs(self.root / "apps" / app_id(app), missing=True) if app is not None else {}
        shortage = shortfall(standard, candidate)
        if shortage:
            raise CapacityError(shortage)
        return {
            **{f"palmimo/{name}": data for name, data in standard.items()},
            **{f"{app}/{name}": data for name, data in candidate.items()},
        }

    def launches(self, app: str | None) -> dict[str, int]:
        path = self.root / "launch.json"
        try:
            state = json.loads(read_file(path, 1_048_576))
        except FileNotFoundError:
            state = {"schema": 1, "sequence": 0, "apps": {}}
        if (
            not isinstance(state, dict)
            or type(state.get("schema")) is not int
            or state["schema"] != 1
            or type(state.get("sequence")) is not int
            or state["sequence"] < 0
            or not isinstance(state.get("apps"), dict)
        ):
            raise StoreError("Invalid launch sequence")
        for owner, sequence in state["apps"].items():
            app_id(owner)
            if type(sequence) is not int or not 1 <= sequence <= state["sequence"]:
                raise StoreError("Invalid app launch sequence")
        if app is not None:
            state["sequence"] += 1
            state["apps"][app] = state["sequence"]
            atomic_file(path, json.dumps(state).encode())
        return state["apps"]


class Wire:
    def __init__(self, connection: Any, clock: Any) -> None:
        self.connection, self.clock = connection, clock

    def write(self, data: bytes) -> None:
        end = self.clock() + 2
        offset = 0
        while offset < len(data):
            count = self.connection.write(data[offset:])
            if not isinstance(count, int) or not 0 <= count <= len(data) - offset:
                raise StoreError("Invalid serial write count")
            if count:
                offset += count
                end = self.clock() + 2
            if self.clock() >= end:
                raise StoreError("Serial write stalled")

    def line(self) -> str:
        end = self.clock() + 2
        data = bytearray()
        while self.clock() < end:
            value = self.connection.read(1)
            if not value:
                continue
            data.extend(value)
            if len(data) > 1024:
                raise StoreError("Display reply exceeds 1024 bytes")
            if data.endswith(b"\n"):
                try:
                    line = data.decode("ascii").strip()
                except UnicodeError as error:
                    raise StoreError("Non-ASCII display reply") from error
                data.clear()
                if line and not line.startswith("EVT "):
                    return line
        raise StoreError("Display response stalled for two seconds")

    def command(self, value: str) -> str:
        self.write((value + "\n").encode("ascii"))
        return self.line()

    def listing(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        index: int | None = None
        while True:
            line = self.command("ASSET LIST" + (f" {index}" if index is not None else ""))
            parts = line.split()
            if len(parts) < 4 or parts[:3] != ["OK", "ASSET", "LIST"] or not parts[3].startswith("next="):
                raise StoreError(f"Invalid ASSET LIST: {line}")
            for entry in parts[4:]:
                fields = entry.split(":")
                if len(fields) != 4:
                    raise StoreError("Invalid ASSET LIST entry")
                name, kind, size, crc = fields
                identifier(name)
                if (
                    name in entries
                    or kind not in ("vector", "image", "gif")
                    or not size.isdecimal()
                    or not 0 < int(size) <= CAPACITY
                    or CRC.fullmatch(crc) is None
                ):
                    raise StoreError("Invalid or duplicate ASSET LIST entry")
                entries[name] = crc
            following = parts[3][5:]
            if following == "-":
                return entries
            if (
                not following.isdecimal()
                or not parts[4:]
                or not (index or 0) < int(following) <= CAPACITY // 4096
                or len(entries) > CAPACITY // 4096
            ):
                raise StoreError("Invalid ASSET LIST continuation")
            index = int(following)

    def delete(self, name: str) -> None:
        response = self.command(f"ASSET DEL {name}")
        if response != f"OK ASSET DEL {name}":
            raise StoreError(response)

    def put(self, name: str, data: bytes) -> None:
        response = self.command(f"ASSET PUT {name} {len(data)} {zlib.crc32(data):08x}")
        if response == "ERR ASSET NOSPACE":
            raise NoSpaceError(response)
        if response != f"OK XFER BEGIN {len(data)} ACK=4096":
            raise StoreError(response)
        sent = acked = 0
        while True:
            while sent < len(data) and sent < acked + 16384:
                end = min(sent + 4096, len(data), acked + 16384)
                self.write(data[sent:end])
                sent = end
            response = self.line()
            if response.startswith("ACK "):
                count = response[4:]
                if (
                    not count.isdecimal()
                    or int(count) != min(acked + 4096, len(data))
                    or int(count) > sent
                    or acked == len(data)
                ):
                    raise StoreError("Invalid cumulative XFER ACK")
                acked = int(count)
            elif acked == len(data) and response == f"OK ASSET {name}":
                return
            elif acked == len(data) and response == "ERR ASSET NOSPACE":
                raise NoSpaceError(response)
            else:
                raise StoreError(f"Asset transfer failed: {response}")


def synchronize(store: Store, hardware: Any, app: str | None) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {"result": "failed", "put": [], "deleted": [], "reason": None}
    if app is not None:
        app_id(app)
    try:
        ports, bootsel = hardware.cdc(), hardware.bootsel_count()
        if not ports and not bootsel:
            result["result"] = "skipped"
            return result, 0
        if len(ports) != 1 or bootsel:
            raise StoreError("Cannot identify one CDC face display")
        port = ports[0][0]
        if hardware.port_busy(port):
            result.update(result="busy", reason="Display port is open in another process")
            return result, 75
        with hardware.asset_connection(port) as connection:
            try:
                wire = Wire(connection, hardware.monotonic)
                hello = wire.command("HELLO")
                if not hello.startswith("OK HELLO ") or "proto=2" not in hello.split()[2:]:
                    result["result"] = "skipped"
                    return result, 0
                with store.transaction():
                    desired = store.desired(app)
                    launches = store.launches(app)
                    present = wire.listing()

                    def delete(name: str) -> None:
                        print(f"ASSET DEL {name}", file=sys.stderr, flush=True)
                        wire.delete(name)
                        present.pop(name, None)
                        result["deleted"].append(name)

                    def put(name: str) -> None:
                        print(f"ASSET PUT {name}", file=sys.stderr, flush=True)
                        wire.put(name, desired[name])
                        present[name] = f"{zlib.crc32(desired[name]):08x}"
                        result["put"].append(name)

                    others = sorted(
                        {
                            name.split("/")[0]
                            for name in present
                            if not name.startswith("palmimo/") and name.split("/")[0] != app
                        },
                        key=lambda owner: (launches.get(owner, 0), owner),
                    )
                    repacked = False
                    for name, data in desired.items():
                        if present.get(name) == f"{zlib.crc32(data):08x}":
                            continue
                        while True:
                            try:
                                put(name)
                                break
                            except NoSpaceError:
                                if others:
                                    owner = others.pop(0)
                                    for old in sorted(n for n in present if n.startswith(owner + "/")):
                                        delete(old)
                                elif not repacked:
                                    repacked = True
                                    for old in sorted(n for n in present if not n.startswith("palmimo/")):
                                        delete(old)
                                    # Descending sizes reduce the free runs consumed by small app assets.
                                    for pending in sorted(desired, key=lambda n: (-len(desired[n]), n)):
                                        if present.get(pending) != f"{zlib.crc32(desired[pending]):08x}":
                                            put(pending)
                                    break
                                else:
                                    raise StoreError("Insufficient contiguous flash space after repacking") from None
                    result["result"] = "synced"
            finally:
                connection.dtr = False
    except BusyError as error:
        result.update(result="busy", reason=str(error))
        return result, 75
    except (StoreError, OSError, ValueError, ImportError) as error:
        result["reason"] = str(error)
        return result, 1
    return result, 0
