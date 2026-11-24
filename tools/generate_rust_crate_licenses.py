#!/usr/bin/env python3
"""Write the license files of every Rust crate a Python sdist links into
its extension module, for oss-static-licenses/rust-crates/<dist>-<version>/.

maturin-built wheels (pydantic-core, watchfiles, ...) statically link their
crate graph but ship only the top-level project's LICENSE. This resolves the
sdist's own Cargo.lock for the image's target, keeps only `normal`
dependencies reachable from the root package (build-dependencies run on the
build host and dev-dependencies never reach the wheel), and copies each
crate's license files as published on crates.io.

Requires cargo on PATH and network access to crates.io:

    tools/generate_rust_crate_licenses.py --sdist pydantic_core-2.46.4.tar.gz \\
        --out oss-static-licenses/rust-crates/pydantic-core-2.46.4
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DEFAULT_TARGET = "aarch64-unknown-linux-gnu"

_LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|COPYRIGHT|NOTICE|UNLICENSE)", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class Crate:
    name: str
    version: str
    license: str
    source_dir: Path


def linked_crates(metadata: Mapping[str, Any]) -> list[Crate]:
    """Crates reachable from the resolve root through `normal` dependency
    edges only, sorted by (name, version). The root itself is excluded --
    its LICENSE already ships in the wheel's dist-info."""
    packages = {p["id"]: p for p in metadata["packages"]}
    nodes = {n["id"]: n for n in metadata["resolve"]["nodes"]}
    root = metadata["resolve"]["root"]
    if root is None:
        raise ValueError("cargo metadata has no resolve root (virtual workspace?)")

    seen: set[str] = set()
    stack = [root]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        for dep in nodes[node_id]["deps"]:
            if any(kind["kind"] is None for kind in dep["dep_kinds"]):
                stack.append(dep["pkg"])
    seen.discard(root)

    crates = [
        Crate(
            name=packages[i]["name"],
            version=packages[i]["version"],
            license=packages[i]["license"] or "",
            source_dir=Path(packages[i]["manifest_path"]).parent,
        )
        for i in seen
    ]
    return sorted(crates, key=lambda c: (c.name, c.version))


def crate_license_files(source_dir: Path) -> list[Path]:
    return sorted(p for p in source_dir.iterdir() if p.is_file() and _LICENSE_FILE.match(p.name))


def write_crate_licenses(crates: list[Crate], out_dir: Path) -> list[Crate]:
    """Copy each crate's license files to out_dir/<name>-<version>/ and write
    CRATES.tsv. Returns the crates that publish no license file at all."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    without_files: list[Crate] = []
    rows = ["name\tversion\tlicense"]
    for crate in crates:
        rows.append(f"{crate.name}\t{crate.version}\t{crate.license}")
        files = crate_license_files(crate.source_dir)
        if not files:
            without_files.append(crate)
            continue
        dest = out_dir / f"{crate.name}-{crate.version}"
        dest.mkdir()
        for src in files:
            shutil.copyfile(src, dest / src.name)
    (out_dir / "CRATES.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return without_files


def _cargo(args: list[str], cwd: Path) -> str:
    return subprocess.run(["cargo", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sdist", required=True, type=Path, help="The dist's .tar.gz sdist (contains Cargo.lock).")
    parser.add_argument("--out", required=True, type=Path, help="Output directory; replaced if it exists.")
    parser.add_argument("--target", default=DEFAULT_TARGET, help=f"Rust target triple (default: {DEFAULT_TARGET}).")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(args.sdist) as archive:
            archive.extractall(tmp, filter="data")
        (project,) = [p for p in Path(tmp).iterdir() if p.is_dir()]
        _cargo(["fetch", "--locked", "--target", args.target], project)
        metadata = json.loads(
            _cargo(["metadata", "--format-version", "1", "--locked", "--filter-platform", args.target], project)
        )
        crates = linked_crates(metadata)
        without_files = write_crate_licenses(crates, args.out)

    print(f"{len(crates)} crates written to {args.out}")
    if without_files:
        names = ", ".join(f"{c.name}-{c.version} ({c.license})" for c in without_files)
        print(f"error: crates with no license file -- add their text by hand: {names}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
