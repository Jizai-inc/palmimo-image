"""Unit tests for tools/generate_rust_crate_licenses.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "tools" / "generate_rust_crate_licenses.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_rust_crate_licenses", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


grcl = _load_module()


def _package(name: str) -> dict[str, str]:
    return {"id": name, "name": name, "version": "1.0.0", "license": "MIT", "manifest_path": f"/src/{name}/Cargo.toml"}


def _dep(pkg: str, kind: str | None) -> dict[str, object]:
    return {"pkg": pkg, "dep_kinds": [{"kind": kind, "target": None}]}


def test_linked_crates_follows_only_normal_dependencies() -> None:
    metadata = {
        "packages": [_package(n) for n in ("root", "serde", "itoa", "cc", "proptest", "build-only-dep")],
        "resolve": {
            "root": "root",
            "nodes": [
                {"id": "root", "deps": [_dep("serde", None), _dep("cc", "build"), _dep("proptest", "dev")]},
                {"id": "serde", "deps": [_dep("itoa", None)]},
                {"id": "cc", "deps": [_dep("build-only-dep", None)]},
                {"id": "itoa", "deps": []},
                {"id": "proptest", "deps": []},
                {"id": "build-only-dep", "deps": []},
            ],
        },
    }

    assert [c.name for c in grcl.linked_crates(metadata)] == ["itoa", "serde"]
