#!/usr/bin/env python3
"""Read manifest.json for install.sh and verify_platform.py.

install.sh has no JSON parser of its own; every manifest field it needs
comes through one of this script's subcommands so the manifest stays the
single source of truth for what the bundle owns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(manifest_path: str) -> dict:
    return json.loads(Path(manifest_path).read_text(encoding="utf-8"))


def cmd_version(manifest: dict) -> None:
    print(manifest["version"])


def cmd_files(manifest: dict) -> None:
    for entry in manifest["owns"]["files"]:
        print(f"{entry['path']}\t{entry['mode']}\t{entry['owner']}\t{entry['group']}")


def cmd_managed_directories(manifest: dict) -> None:
    for entry in manifest["owns"]["managed_directories"]:
        print(f"{entry['path']}\t{entry['mode']}\t{entry['owner']}\t{entry['group']}")


def cmd_state_directories(manifest: dict) -> None:
    for entry in manifest["owns"]["state_directories"]:
        print(f"{entry['path']}\t{entry['mode']}\t{entry['owner']}\t{entry['group']}")


def cmd_external_binaries(manifest: dict) -> None:
    for entry in manifest["owns"]["external_binaries"]:
        print(f"{entry['path']}\t{entry['mode']}\t{entry['owner']}\t{entry['group']}")


def cmd_bundle_cache(manifest: dict) -> None:
    for entry in manifest["owns"]["bundle_cache"]:
        print(f"{entry['path']}\t{entry['mode']}\t{entry['owner']}\t{entry['group']}")


def cmd_retired(manifest: dict) -> None:
    for path in manifest.get("retired", []):
        print(path)


COMMANDS = {
    "version": cmd_version,
    "files": cmd_files,
    "managed-directories": cmd_managed_directories,
    "state-directories": cmd_state_directories,
    "external-binaries": cmd_external_binaries,
    "bundle-cache": cmd_bundle_cache,
    "retired": cmd_retired,
}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in COMMANDS:
        print(f"usage: manifest_tool.py MANIFEST_JSON {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    manifest = _load(sys.argv[1])
    COMMANDS[sys.argv[2]](manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
