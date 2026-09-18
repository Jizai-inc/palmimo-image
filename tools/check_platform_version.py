#!/usr/bin/env python3
"""Fail when a platform/ content change is not accompanied by a manifest.json
version bump.

The Portal decides whether a device needs an update purely by comparing
manifest.json's integer `version` between what it has and what is on offer
(see platform/manifest.json and doc/design/palmimo-app-platform.md 2.8) --
it never diffs file content. A content change shipped under an unchanged or
lowered version therefore never reaches devices. Runs with plain python3
(stdlib plus the sibling build_platform_bundle.py): CI and the release
workflow invoke it outside the uv project.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from build_platform_bundle import bundle_files


def _manifest_bytes_for_hash(path: Path) -> bytes:
    # Re-serialized with `version` removed and keys sorted, so a version
    # bump or a formatting-only edit (key order, indentation) never counts
    # as a content change on its own.
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("version", None)
    return json.dumps(data, sort_keys=True).encode()


def _content_hash(root: Path) -> str:
    # Only what the bundle tarball ships counts: a README or bytecode next to
    # platform/ never reaches a device, so changing it needs no version bump.
    digest = hashlib.sha256()
    for path in bundle_files(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        if rel == "manifest.json":
            digest.update(_manifest_bytes_for_hash(path))
        else:
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _manifest_version(platform_dir: Path) -> int:
    manifest = json.loads((platform_dir / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    if not isinstance(version, int):
        raise ValueError(f"manifest.json version is not an integer: {version!r}")
    return version


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: check_platform_version.py BASE_DIR HEAD_DIR", file=sys.stderr)
        return 2

    base_dir, head_dir = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        base_version = _manifest_version(base_dir)
        head_version = _manifest_version(head_dir)
        content_changed = _content_hash(base_dir) != _content_hash(head_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"check_platform_version: {exc}", file=sys.stderr)
        return 2

    if not content_changed or head_version > base_version:
        return 0

    print(
        f"platform/ content changed but manifest.json version did not increase "
        f"(base={base_version}, head={head_version})",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
