#!/usr/bin/env python3
"""Print the sha256 of every file under ROOT (relative path + content),
combined in sorted order.

Used by the platform-bundle CI convergence check
(.github/workflows/ci.yml, job platform_convergence) to compare two
installed trees in a bare container without pytest -- the same walk
tests/test_platform_bundle.py's _tree_hash does, kept in a standalone
script since that check runs outside the uv project.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: compute_tree_hash.py ROOT", file=sys.stderr)
        return 2
    print(tree_hash(Path(sys.argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
