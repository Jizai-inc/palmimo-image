#!/usr/bin/env python3
"""Build palmimo-platform-<tag>.tar.gz deterministically from platform/.

Deterministic: entries are added in a fixed (sorted) order with a fixed
mtime, so two builds from the same tree byte-for-byte match -- CI's build
and a local rebuild of the same commit produce the same sha256, which is
what test_build_platform_bundle_is_deterministic pins.

install.sh loads manifest_tool.py and verify_platform.py from its own
directory at runtime (see platform/install.sh), so the bundle carries all
four platform/ files, not just manifest.json + install.sh + files/.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import sys
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = REPO_ROOT / "platform"

# Fixed so the tarball is reproducible regardless of when it was built.
FIXED_MTIME = 0


def bundle_files(platform_dir: Path = PLATFORM_DIR) -> list[Path]:
    """Return the files of *platform_dir* that the bundle tarball ships, sorted by bundle path."""
    paths = [
        platform_dir / "manifest.json",
        platform_dir / "install.sh",
        platform_dir / "manifest_tool.py",
        platform_dir / "verify_platform.py",
        *(platform_dir / "files").rglob("*"),
    ]
    shipped = [p for p in paths if p.is_file() and "__pycache__" not in p.relative_to(platform_dir).parts]
    return sorted(shipped, key=lambda p: p.relative_to(platform_dir).as_posix())


def build(out_path: Path) -> None:
    files = bundle_files()
    # gzip's own header carries a second, independent mtime/filename field
    # that tarfile.open("w:gz") does not let us pin -- wrap a GzipFile
    # explicitly so the compressed bytes are reproducible too, not just the
    # tar entries.
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=out_path.open("wb"), mtime=FIXED_MTIME) as gz,
        tarfile.open(fileobj=gz, mode="w:") as tar,
    ):
        for path in files:
            arcname = path.relative_to(PLATFORM_DIR).as_posix()
            info = tar.gettarinfo(path, arcname=arcname)
            info.mtime = FIXED_MTIME
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with path.open("rb") as f:
                tar.addfile(info, f)


def _sha256_sidecar(data: bytes, name: str, sha256_out: Path | None) -> str:
    digest = hashlib.sha256(data).hexdigest()
    if sha256_out:
        sha256_out.parent.mkdir(parents=True, exist_ok=True)
        sha256_out.write_text(f"{digest}  {name}\n", encoding="utf-8")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="output .tar.gz path")
    parser.add_argument("--sha256-out", type=Path, help="optional path for a <name>.sha256 sidecar")
    # The Portal polls for the latest platform version far more often than
    # it actually applies one -- a standalone manifest.json asset lets that
    # check skip downloading the whole tarball every time.
    parser.add_argument("--manifest-out", type=Path, help="optional path for a standalone manifest.json asset")
    parser.add_argument(
        "--manifest-sha256-out", type=Path, help="optional path for the manifest asset's .sha256 sidecar"
    )
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    build(args.out)
    digest = _sha256_sidecar(args.out.read_bytes(), args.out.name, args.sha256_out)
    print(digest)

    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        manifest_bytes = (PLATFORM_DIR / "manifest.json").read_bytes()
        args.manifest_out.write_bytes(manifest_bytes)
        manifest_digest = _sha256_sidecar(manifest_bytes, args.manifest_out.name, args.manifest_sha256_out)
        print(manifest_digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
