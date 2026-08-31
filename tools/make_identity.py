#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Generate a Palmimo identity file (identity file spec v2).

Test/dev-only tool: writes ``palmimo-identity.json`` — ``{"device_id",
"initial_password"}``, both plaintext — to drop onto a Pi's boot (FAT)
partition so ``palmimo-firstboot.service`` individualizes the device. See
``doc/design.md`` ("識別ファイル仕様 v2") for why this is
plaintext (no argon2/hash field): the value is printed on the physical seal,
so a boot-partition plaintext copy is within the same threat model as the
seal itself, and the manufacturing line (O10) uses this exact format as the
source of truth for what it writes.

Usage:
    uv run tools/make_identity.py --device-id 405 --password <plain>
    uv run tools/make_identity.py --device-id 405 --password <plain> --out /path/to/palmimo-identity.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# Kept in sync by hand with the DEVICE_ID_REGEX in
# files/usr/local/lib/palmimo/firstboot.sh — that script is the
# other holder of this pattern (it re-validates device_id at firstboot time,
# independently of whatever this tool already checked when the identity file
# was made). tests/test_image_contracts.py pins that the two
# patterns stay textually equal.
DEVICE_ID_PATTERN = r"^[a-z0-9-]{1,32}$"
DEVICE_ID_REGEX = re.compile(DEVICE_ID_PATTERN)

# The manufacturing (O10) sticker alphabet: 8-63 chars is the WPA2-PSK
# passphrase length bound, alphanumeric-only keeps the printed sticker legible
# and — just as importantly — keeps the value safe to interpolate anywhere
# downstream without escaping (comitup.conf line, sed replacement text, shell
# argument, etc.). Kept in sync by hand with the PASSWORD_REGEX in
# files/usr/local/lib/palmimo/firstboot.sh — that script is the
# other holder of this pattern (it re-validates initial_password at firstboot
# time). tests/test_image_contracts.py pins that the two patterns
# stay textually equal.
PASSWORD_PATTERN = r"^[A-Za-z0-9]{8,63}$"
PASSWORD_REGEX = re.compile(PASSWORD_PATTERN)

DEFAULT_OUT = Path("palmimo-identity.json")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device-id",
        required=True,
        help=f"Device id, must match {DEVICE_ID_PATTERN!r} (hostname-safe).",
    )
    parser.add_argument(
        "--password",
        required=True,
        help=f"Plaintext initial password (as printed on the device seal), must match {PASSWORD_PATTERN!r}.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output path (default: {DEFAULT_OUT}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if not DEVICE_ID_REGEX.match(args.device_id):
        print(
            f"error: --device-id {args.device_id!r} does not match {DEVICE_ID_PATTERN!r}",
            file=sys.stderr,
        )
        return 1
    if not PASSWORD_REGEX.match(args.password):
        print(
            f"error: --password does not match {PASSWORD_PATTERN!r} "
            "(alphanumeric, 8-63 chars — the manufacturing sticker alphabet)",
            file=sys.stderr,
        )
        return 1

    identity = {"device_id": args.device_id, "initial_password": args.password}

    out_path = args.out
    # Write with restrictive permissions from the start (O_CREAT|O_EXCL-free,
    # but umask-independent): open with mode 0o600 rather than chmod after
    # the fact, so the plaintext password is never briefly world-readable.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(identity, f, indent=2)
        f.write("\n")
    os.chmod(out_path, 0o600)

    print(f"wrote {out_path} (device_id={args.device_id!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
