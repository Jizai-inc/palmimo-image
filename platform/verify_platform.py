#!/usr/bin/env python3
"""Verify a --root tree against manifest.json's owns/retired sections.

Prints one JSON object per line for each difference found (never for a
match) and exits non-zero if any are printed. Used by install.sh verify and
directly by tests, which need the same walk install.sh does without
shelling out to it a second time.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[2:].zfill(4)


def _diff(kind: str, path: str, **extra: object) -> dict:
    return {"kind": kind, "path": path, **extra}


def _fake_account_lines(fake_accounts_file: str) -> set[str]:
    try:
        return set(Path(fake_accounts_file).read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return set()


def _check_files(manifest: dict, files_dir: Path, root: Path, fake_lines: set[str] | None) -> list[dict]:
    diffs = []
    for entry in manifest["owns"]["files"]:
        rel = entry["path"]
        src = files_dir / rel
        dst = root / rel
        if not dst.is_file():
            diffs.append(_diff("missing", rel))
            continue
        if _sha256(dst) != _sha256(src):
            diffs.append(_diff("content", rel))
        if _mode(dst) != entry["mode"].zfill(4):
            diffs.append(_diff("mode", rel, expected=entry["mode"], actual=_mode(dst)))
        diffs.extend(_check_owner_group(rel, dst, entry, fake_lines))
    return diffs


def _check_managed_directory_tree(
    rel_dir: str, src_dir: Path, dst_dir: Path, entry: dict, fake_lines: set[str] | None
) -> list[dict]:
    diffs = []
    if not dst_dir.is_dir():
        diffs.append(_diff("missing", rel_dir))
        return diffs
    src_files = {p.relative_to(src_dir).as_posix() for p in src_dir.rglob("*") if p.is_file()}
    dst_files = {p.relative_to(dst_dir).as_posix() for p in dst_dir.rglob("*") if p.is_file()}
    for rel_file in sorted(src_files - dst_files):
        diffs.append(_diff("missing", f"{rel_dir}/{rel_file}"))
    for rel_file in sorted(dst_files - src_files):
        diffs.append(_diff("unexpected", f"{rel_dir}/{rel_file}"))
    for rel_file in sorted(src_files & dst_files):
        src_file = src_dir / rel_file
        dst_file = dst_dir / rel_file
        if _sha256(dst_file) != _sha256(src_file):
            diffs.append(_diff("content", f"{rel_dir}/{rel_file}"))
        if src_file.stat().st_mode & stat.S_IXUSR and _mode(dst_file) != _mode(src_file):
            diffs.append(_diff("mode", f"{rel_dir}/{rel_file}", expected=_mode(src_file), actual=_mode(dst_file)))
        # A managed directory has no per-file manifest entries, so a file
        # inside it is expected to carry the owning directory's owner/group.
        diffs.extend(_check_owner_group(f"{rel_dir}/{rel_file}", dst_file, entry, fake_lines))
    return diffs


def _check_managed_directories(manifest: dict, files_dir: Path, root: Path, fake_lines: set[str] | None) -> list[dict]:
    diffs = []
    for entry in manifest["owns"]["managed_directories"]:
        rel = entry["path"]
        diffs.extend(_check_managed_directory_tree(rel, files_dir / rel, root / rel, entry, fake_lines))
        dst = root / rel
        if dst.is_dir():
            if _mode(dst) != entry["mode"].zfill(4):
                diffs.append(_diff("mode", rel, expected=entry["mode"], actual=_mode(dst)))
            diffs.extend(_check_owner_group(rel, dst, entry, fake_lines))
    return diffs


def _check_owner_group(rel: str, dst: Path, entry: dict, fake_lines: set[str] | None) -> list[dict]:
    # Ownership by a synthetic account cannot exist on a bare test root:
    # PALMIMO_FAKE_ACCOUNTS mode checks that install.sh *intended* to chown
    # here instead (the account itself is asserted separately).
    if fake_lines is not None:
        return []
    diffs = []
    try:
        import grp
        import pwd

        owner_name = pwd.getpwuid(dst.stat().st_uid).pw_name
        group_name = grp.getgrgid(dst.stat().st_gid).gr_name
    except (KeyError, ImportError):
        return [_diff("owner", rel, note="uid/gid does not resolve to a name")]
    if owner_name != entry["owner"]:
        diffs.append(_diff("owner", rel, expected=entry["owner"], actual=owner_name))
    if group_name != entry["group"]:
        diffs.append(_diff("group", rel, expected=entry["group"], actual=group_name))
    return diffs


def _check_state_directories(manifest: dict, root: Path, fake_lines: set[str] | None) -> list[dict]:
    diffs = []
    for entry in manifest["owns"]["state_directories"]:
        rel = entry["path"]
        dst = root / rel
        if not dst.is_dir():
            diffs.append(_diff("missing", rel))
            continue
        if _mode(dst) != entry["mode"].zfill(4):
            diffs.append(_diff("mode", rel, expected=entry["mode"], actual=_mode(dst)))
        diffs.extend(_check_owner_group(rel, dst, entry, fake_lines))
    return diffs


def _check_bundle_cache(manifest: dict, root: Path, fake_lines: set[str] | None) -> list[dict]:
    # Only the cache directory's own owner/mode is checked here, never its
    # contents: it is a full copy of the bundle that was installed (built
    # by install_bundle_cache), and diffing it against itself would be
    # meaningless. It is what a later `verify` run is invoked *from* (see
    # manifest owns.bundle_cache), not something verify inspects.
    diffs = []
    for entry in manifest["owns"]["bundle_cache"]:
        rel = entry["path"]
        dst = root / rel
        if not dst.is_dir():
            diffs.append(_diff("missing", rel))
            continue
        if _mode(dst) != entry["mode"].zfill(4):
            diffs.append(_diff("mode", rel, expected=entry["mode"], actual=_mode(dst)))
        diffs.extend(_check_owner_group(rel, dst, entry, fake_lines))
    return diffs


def _check_external_binaries(manifest: dict, root: Path, fake_lines: set[str] | None) -> list[dict]:
    diffs = []
    for entry in manifest["owns"]["external_binaries"]:
        rel = entry["path"]
        dst = root / rel
        if not dst.is_file() or not os.access(dst, os.X_OK):
            diffs.append(_diff("missing", rel))
            continue
        if _mode(dst) != entry["mode"].zfill(4):
            diffs.append(_diff("mode", rel, expected=entry["mode"], actual=_mode(dst)))
        diffs.extend(_check_owner_group(rel, dst, entry, fake_lines))
    return diffs


def _check_repair_root_owned(manifest: dict, root: Path, fake_lines: set[str] | None) -> list[dict]:
    diffs = []
    for path in manifest["owns"].get("repair_root_owned", []):
        rel = "." if path == "." else path
        dst = root if path == "." else root / path
        if not dst.exists():
            continue
        if fake_lines is not None:
            continue
        try:
            import pwd

            owner_name = pwd.getpwuid(dst.stat().st_uid).pw_name
        except KeyError:
            diffs.append(_diff("owner", rel, note="uid does not resolve to a name"))
            continue
        if owner_name != "root":
            diffs.append(_diff("owner", rel, expected="root", actual=owner_name))
        mode = stat.S_IMODE(dst.stat().st_mode)
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            diffs.append(_diff("mode", rel, note="group- or world-writable", actual=oct(mode)[2:].zfill(4)))
    return diffs


def _check_apt_packages(manifest: dict, root: Path, fake_lines: set[str] | None) -> list[dict]:
    # No dpkg database exists on a bare test root, and fake-accounts mode
    # never touches apt at all (install.sh only records intent) -- see
    # install_apt_packages in install.sh.
    if fake_lines is not None:
        return []
    diffs = []
    admindir_args = [] if root == Path("/") else [f"--admindir={root}/var/lib/dpkg"]
    for pkg in manifest["owns"].get("apt_packages", []):
        try:
            result = subprocess.run(
                ["dpkg-query", *admindir_args, "-W", "-f=${Status}", pkg],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            # Every real target is Debian-based and always has dpkg-query;
            # this only happens verifying a non-Debian host (e.g. running
            # the test suite on macOS), where the package can't be checked.
            diffs.append(_diff("missing", pkg, note="dpkg-query not available"))
            continue
        if "install ok installed" not in result.stdout:
            diffs.append(_diff("missing", pkg))
    return diffs


def _check_accounts(manifest: dict, root: Path, fake_accounts_file: str | None) -> list[dict]:
    diffs = []
    if fake_accounts_file:
        lines = _fake_account_lines(fake_accounts_file)
        for group in manifest["owns"]["groups"]:
            if not any(line.startswith(f"groupadd --system {group}") for line in lines):
                diffs.append(_diff("group_missing", group))
        for user in manifest["owns"]["users"]:
            if not any(line.split()[-1] == user["name"] and line.startswith("useradd") for line in lines):
                diffs.append(_diff("user_missing", user["name"]))
        return diffs

    passwd = (root / "etc" / "passwd").read_text(encoding="utf-8") if (root / "etc" / "passwd").is_file() else ""
    group = (root / "etc" / "group").read_text(encoding="utf-8") if (root / "etc" / "group").is_file() else ""
    for grp_name in manifest["owns"]["groups"]:
        if not any(line.startswith(f"{grp_name}:") for line in group.splitlines()):
            diffs.append(_diff("group_missing", grp_name))
    for user in manifest["owns"]["users"]:
        if not any(line.startswith(f"{user['name']}:") for line in passwd.splitlines()):
            diffs.append(_diff("user_missing", user["name"]))
    return diffs


def _check_retired(manifest: dict, root: Path) -> list[dict]:
    diffs = []
    for rel in manifest.get("retired", []):
        if (root / rel).exists():
            diffs.append(_diff("retired_present", rel))
    return diffs


def verify(manifest: dict, files_dir: Path, root: Path, fake_accounts_file: str | None) -> list[dict]:
    diffs: list[dict] = []
    fake_lines = _fake_account_lines(fake_accounts_file) if fake_accounts_file else None
    diffs.extend(_check_files(manifest, files_dir, root, fake_lines))
    diffs.extend(_check_managed_directories(manifest, files_dir, root, fake_lines))
    diffs.extend(_check_state_directories(manifest, root, fake_lines))
    diffs.extend(_check_bundle_cache(manifest, root, fake_lines))
    diffs.extend(_check_external_binaries(manifest, root, fake_lines))
    diffs.extend(_check_repair_root_owned(manifest, root, fake_lines))
    diffs.extend(_check_apt_packages(manifest, root, fake_lines))
    diffs.extend(_check_accounts(manifest, root, fake_accounts_file))
    diffs.extend(_check_retired(manifest, root))
    return diffs


def main() -> int:
    # Exit code is a contract the Portal reads: 0 clean, 1 drift found, 2+
    # could not run at all (bad args, unreadable manifest, ...). An
    # unhandled exception would otherwise exit 1 too (Python's default),
    # indistinguishable from "verify ran and found drift" -- the Portal's
    # platform-ready check depends on telling those apart.
    if len(sys.argv) != 4:
        print("usage: verify_platform.py MANIFEST_JSON FILES_DIR ROOT", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        files_dir = Path(sys.argv[2])
        root = Path(sys.argv[3])
        diffs = verify(manifest, files_dir, root, os.environ.get("PALMIMO_FAKE_ACCOUNTS"))
    except Exception as exc:  # deliberately broad -- see the exit-code contract above
        print(f"verify_platform: could not run: {exc}", file=sys.stderr)
        return 2
    for entry in diffs:
        print(json.dumps(entry, sort_keys=True))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
