"""Publication hygiene for this repository.

This repository is published (unlike the internal monorepo it was extracted
from). This module scans every tracked file for content that must never
reach a public repository: private-network addresses, cloud/manufacturing
secrets, and the internal monorepo's own name -- a stray reference to it
would point a reader at a repository they cannot see.

Modeled on the internal monorepo's own copy of this contract (private
maintenance tooling, not part of any published tree), trimmed to the
patterns that make sense standalone here (no cross-tree scanning, no
internal-vocabulary denylist -- this repository's own contents decide
what's confidential).
"""

from __future__ import annotations

import codecs
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# This file itself necessarily carries the banned patterns as literal test
# fixtures (the whole repository is published, unlike the internal
# monorepo's version of this contract, which lives in a tree that is never
# itself scanned) -- exclude it from the content scan by identity, not by
# weakening a pattern to dodge its own test data.
SELF_PATH = Path(__file__).resolve()

MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)")
MARKDOWN_LINK_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
EXTERNAL_TARGET = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)")

# The internal monorepo this tree was extracted from, spelled out of two
# pieces so this file's own source text never contains the contiguous name
# either (this module's content scan below applies to itself too, minus the
# literal fixture data covered by SELF_PATH).
_MONOREPO_REPO_NAME = "mi" + "-mo-devkit-pre"

# Content that must never appear in this repository's published history.
BANNED_CONTENT = [
    # A reader outside the company cannot see the internal monorepo above;
    # a reference to it here is either a dead link or an accidental
    # disclosure that it exists.
    re.escape(_MONOREPO_REPO_NAME),
    # AWS access key IDs and private-key headers -- key material, not just
    # vocabulary.
    "AKIA[0-9A-Z]{16}",
    "BEGIN [A-Z ]*PRIVATE KEY",
    # Addresses that reach a person directly.
    r"(?i:@gmail\.com)",
    # Private-network addresses. Scoped to the two ranges actually in use
    # elsewhere in this project (the lab LAN and Tailscale's CGNAT range)
    # rather than all of RFC 1918, so a four-part version string cannot
    # trip it.
    r"(^|[^0-9.])192\.168\.[0-9]{1,3}\.[0-9]{1,3}",
    r"(^|[^0-9.])100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}",
    # A MAC address identifies one physical board, so it survives reimaging
    # and reassignment in a way an IP does not.
    r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",
]

# Multi-byte encodings interleave ASCII with NUL bytes, so no byte-level
# pattern can match them reliably. Rather than silently skip such a file,
# its BOM makes it an offender outright: tracked text here is UTF-8.
UNSCANNABLE_BOMS = (
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
    codecs.BOM_UTF32_LE,
    codecs.BOM_UTF32_BE,
)

BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".xz",
        ".img",
        ".zip",
    }
)


def _banned_hits(text: str) -> list[str]:
    return [pattern for pattern in BANNED_CONTENT if re.search(pattern, text, re.MULTILINE)]


def _tracked_files() -> list[Path]:
    # Enumerate through git (tracked, plus new files git would accept)
    # rather than rglob'ing the working tree: only what git ships can be
    # published, so build output and tool caches under a gitignored path
    # (dist/, pigen/.workspace/) can never reach the published repository
    # and must not be held to its contracts.
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(path for name in result.stdout.split("\0") if name and (path := REPO_ROOT / name).is_file())


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_git_enumerates_tracked_files() -> None:
    # Each check below reports offenders, so an empty enumeration would pass
    # every one of them in silence.
    assert _tracked_files(), "git enumerated no tracked files -- the scan root moved."


def test_repository_carries_no_banned_content() -> None:
    offenders = []
    for path in _tracked_files():
        if path == SELF_PATH:
            continue
        rel = _relative(path)

        for pattern in _banned_hits(rel):
            offenders.append(f"{rel} path matched /{pattern}/")

        if path.suffix in BINARY_SUFFIXES:
            continue

        data = path.read_bytes()
        if data.startswith(UNSCANNABLE_BOMS):
            offenders.append(f"{rel} is not UTF-8 (multi-byte BOM), so it cannot be scanned")
            continue
        # errors="ignore" cannot hide an ASCII pattern: dropping undecodable
        # bytes only ever pulls ASCII runs together, never apart.
        text = data.decode("utf-8", errors="ignore")
        for pattern in _banned_hits(text):
            offenders.append(f"{rel} matched /{pattern}/")

    assert offenders == [], f"Content banned from this repository: {offenders}"


def test_banned_content_scan_covers_the_monorepo_name() -> None:
    assert _banned_hits(f"see Jizai-inc/{_MONOREPO_REPO_NAME} for the internal history")
    assert not _banned_hits("see the palmimo-devkit repository")


def test_banned_content_scan_covers_private_addresses_and_secrets() -> None:
    assert _banned_hits("ssh user@192.168.11.65")
    assert _banned_hits("reachable at 100.109.9.49")
    assert _banned_hits("the board's MAC is 2c:cf:67:11:22:33")
    assert _banned_hits("AKIAABCDEFGHIJKLMNOP")
    assert _banned_hits("-----BEGIN RSA PRIVATE KEY-----")
    assert _banned_hits("contact devs@gmail.com")

    # Text this repository legitimately carries. Each sits close to a
    # pattern above, so a widening of one shows up as a failure here rather
    # than as a blocked pull request on unrelated work.
    assert not _banned_hits("ssh user@palmimo-406.local")
    assert not _banned_hits('version = "10.1.2.3"')


def test_banned_content_scan_rejects_multibyte_boms() -> None:
    assert "AKIAABCDEFGHIJKLMNOP".encode("utf-16").startswith(UNSCANNABLE_BOMS)
    assert not b"plain ascii".startswith(UNSCANNABLE_BOMS)


def test_markdown_links_resolve_inside_the_repository() -> None:
    # This repository ships as one unit, so a relative link that climbs
    # above the repo root is a dead link once published.
    offenders = []
    for path in _tracked_files():
        if path.suffix != ".md":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        targets = MARKDOWN_LINK.findall(text) + MARKDOWN_LINK_DEFINITION.findall(text)
        for target in targets:
            if EXTERNAL_TARGET.match(target):
                continue
            resolved = (path.parent / target.split("#")[0]).resolve()
            if resolved != REPO_ROOT and REPO_ROOT not in resolved.parents:
                offenders.append(f"{_relative(path)} -> {target}")

    assert offenders == [], f"Links must resolve inside this repository: {offenders}"
