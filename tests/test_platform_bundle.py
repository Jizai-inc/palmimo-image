"""Behavior contracts for the platform bundle (platform/) -- see
doc/design/palmimo-app-platform.md 2.8 (palmimo-devkit monorepo).

Account creation (useradd/groupadd/usermod) is exercised through
PALMIMO_FAKE_ACCOUNTS, never against the real account database -- these
tests never need root.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DIR = REPO_ROOT / "platform"
INSTALL_SH = PLATFORM_DIR / "install.sh"
MANIFEST_PATH = PLATFORM_DIR / "manifest.json"
VERIFY_TOOL = PLATFORM_DIR / "verify_platform.py"

APPLY_SCRIPT = REPO_ROOT / "apply-pi.sh"
PIGEN_STAGE_SCRIPT = REPO_ROOT / "pigen" / "stage-palmimo" / "05-app-platform" / "00-run.sh"
BUILD_BUNDLE_SCRIPT = REPO_ROOT / "tools" / "build_platform_bundle.py"

APP_UNIT = PLATFORM_DIR / "files" / "etc" / "systemd" / "system" / "palmimo-app@.service"
APP_SYNC_UNIT = PLATFORM_DIR / "files" / "etc" / "systemd" / "system" / "palmimo-app-sync@.service"
APP_SYNC_HELPER = PLATFORM_DIR / "files" / "usr" / "lib" / "palmimo" / "app-sync"
APP_LAUNCH_HELPER = PLATFORM_DIR / "files" / "usr" / "lib" / "palmimo" / "app-launch"

# Paths that would identify the platform's internals leaking into a script
# that is only supposed to call the installer.
_PLATFORM_INTERNAL_MARKERS = [
    "/usr/lib/palmimo",
    "polkit-1/rules.d/60-",
    "tmpfiles.d/palmimo",
    "journald.conf.d",
    "palmimo-app@",
]


def _stub_uv(tmp_path: Path) -> Path:
    stub = tmp_path / "fake-uv"
    stub.write_text("#!/bin/sh\necho fake-uv\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _run_install(
    root: Path, fake_accounts: Path, uv_source: Path, *, command: str = "install"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(INSTALL_SH), command, "--root", str(root)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PALMIMO_FAKE_ACCOUNTS": str(fake_accounts),
            "PALMIMO_UV_SOURCE": str(uv_source),
        },
        capture_output=True,
        text=True,
    )


def _tree_hash(root: Path) -> str:
    # Same walk as tools/compute_tree_hash.py, which the CI convergence
    # check (.github/workflows/ci.yml) uses instead of this pytest-only
    # helper.
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture()
def installed_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    root.mkdir()
    fake_accounts = tmp_path / "fake_accounts.txt"
    fake_accounts.touch()
    uv_source = _stub_uv(tmp_path)
    result = _run_install(root, fake_accounts, uv_source)
    assert result.returncode == 0, result.stderr
    return root, fake_accounts, uv_source


def test_install_twice_is_idempotent(installed_root: tuple[Path, Path, Path]) -> None:
    root, fake_accounts, uv_source = installed_root
    hash_before = _tree_hash(root)
    fake_accounts_before = fake_accounts.read_text(encoding="utf-8")

    result = _run_install(root, fake_accounts, uv_source)

    assert result.returncode == 0, result.stderr
    assert _tree_hash(root) == hash_before
    assert fake_accounts.read_text(encoding="utf-8") == fake_accounts_before


def test_verify_passes_after_install(installed_root: tuple[Path, Path, Path]) -> None:
    root, fake_accounts, _uv_source = installed_root
    result = _run_install(root, fake_accounts, Path(), command="verify")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_verify_reports_a_diff_when_an_owned_file_is_modified(installed_root: tuple[Path, Path, Path]) -> None:
    root, fake_accounts, _uv_source = installed_root
    tampered = root / "etc" / "tmpfiles.d" / "palmimo.conf"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")

    result = _run_install(root, fake_accounts, Path(), command="verify")

    assert result.returncode != 0
    diffs = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(d["path"] == "etc/tmpfiles.d/palmimo.conf" and d["kind"] == "content" for d in diffs)


def test_verify_reports_a_diff_when_an_owned_file_is_removed(installed_root: tuple[Path, Path, Path]) -> None:
    root, fake_accounts, _uv_source = installed_root
    removed = root / "etc" / "polkit-1" / "rules.d" / "60-palmimo-app-platform.rules"
    removed.unlink()

    result = _run_install(root, fake_accounts, Path(), command="verify")

    assert result.returncode != 0
    diffs = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(
        d["path"] == "etc/polkit-1/rules.d/60-palmimo-app-platform.rules" and d["kind"] == "missing" for d in diffs
    )


def test_verify_reports_a_diff_when_a_retired_path_is_present(tmp_path: Path) -> None:
    # verify_platform.verify() takes the manifest as data, so this exercises
    # the retired-path check without needing a real v(N-1) -> v1 bundle
    # pair (platform v1 itself retires nothing yet).
    sys.path.insert(0, str(PLATFORM_DIR))
    try:
        import verify_platform
    finally:
        sys.path.pop(0)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["retired"] = ["usr/lib/palmimo-old-helper"]
    root = tmp_path / "root"
    (root / "usr" / "lib").mkdir(parents=True)
    (root / "usr" / "lib" / "palmimo-old-helper").write_text("stale", encoding="utf-8")

    diffs = verify_platform.verify(manifest, PLATFORM_DIR / "files", root, None)

    assert any(d["kind"] == "retired_present" and d["path"] == "usr/lib/palmimo-old-helper" for d in diffs)


def test_install_converges_stale_leftovers_to_a_fresh_install_tree_hash(tmp_path: Path) -> None:
    # Simulates a device carrying v(N-1)-style leftovers: a stray file
    # inside a bundle-owned directory, and a path a hypothetical next
    # version would retire. Both must disappear so a pre-existing device
    # and a fresh image converge to the same tree.
    bundle_dir = tmp_path / "bundle"
    shutil.copytree(PLATFORM_DIR, bundle_dir, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["retired"] = ["etc/palmimo-old-app-platform.conf"]
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    install_sh = bundle_dir / "install.sh"

    fake_accounts = tmp_path / "fake_accounts.txt"
    fake_accounts.touch()
    uv_source = _stub_uv(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PALMIMO_FAKE_ACCOUNTS": str(fake_accounts),
        "PALMIMO_UV_SOURCE": str(uv_source),
    }

    stale_root = tmp_path / "stale_root"
    stale_root.mkdir()
    (stale_root / "usr" / "lib" / "palmimo").mkdir(parents=True)
    (stale_root / "usr" / "lib" / "palmimo" / "old-helper.py").write_text("stale", encoding="utf-8")
    (stale_root / "etc").mkdir()
    (stale_root / "etc" / "palmimo-old-app-platform.conf").write_text("stale", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(install_sh), "install", "--root", str(stale_root)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    fresh_root = tmp_path / "fresh_root"
    fresh_root.mkdir()
    fresh_fake_accounts = tmp_path / "fresh_fake_accounts.txt"
    fresh_fake_accounts.touch()
    fresh_env = dict(env, PALMIMO_FAKE_ACCOUNTS=str(fresh_fake_accounts))
    result = subprocess.run(
        ["bash", str(install_sh), "install", "--root", str(fresh_root)], env=fresh_env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    assert not (stale_root / "usr" / "lib" / "palmimo" / "old-helper.py").exists()
    assert not (stale_root / "etc" / "palmimo-old-app-platform.conf").exists()
    assert _tree_hash(stale_root) == _tree_hash(fresh_root)


def test_install_writes_only_paths_the_manifest_owns(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    owned_prefixes = [entry["path"] for entry in manifest["owns"]["files"]]
    owned_prefixes += [entry["path"] for entry in manifest["owns"]["managed_directories"]]
    owned_prefixes += [entry["path"] for entry in manifest["owns"]["state_directories"]]
    owned_prefixes += [entry["path"] for entry in manifest["owns"]["external_binaries"]]
    owned_prefixes.append("var/log/journal")  # mkdir'd directly, not chowned/tracked as state

    root = tmp_path / "root"
    root.mkdir()
    fake_accounts = tmp_path / "fake_accounts.txt"
    fake_accounts.touch()
    uv_source = _stub_uv(tmp_path)
    before = set(root.rglob("*"))

    result = _run_install(root, fake_accounts, uv_source)
    assert result.returncode == 0, result.stderr

    written = {p.relative_to(root).as_posix() for p in root.rglob("*") if p not in before}
    for written_path in written:
        # A written path is fine either as (or under) an owned path, or as
        # an ancestor directory mkdir -p had to create to reach one --
        # e.g. "etc/systemd/system" on the way to
        # "etc/systemd/system/palmimo-app@.service".
        assert any(
            written_path == prefix or written_path.startswith(prefix + "/") or prefix.startswith(written_path + "/")
            for prefix in owned_prefixes
        ), f"install.sh wrote {written_path!r}, which is outside manifest.owns"


def test_bundle_build_is_deterministic(tmp_path: Path) -> None:
    out1 = tmp_path / "b1.tar.gz"
    out2 = tmp_path / "b2.tar.gz"
    for out in (out1, out2):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "build_platform_bundle.py"), "--out", str(out)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    assert hashlib.sha256(out1.read_bytes()).hexdigest() == hashlib.sha256(out2.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# contract: the pi-gen stage and apply-pi.sh call the installer, they do not
# reimplement any part of what it owns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", [APPLY_SCRIPT, PIGEN_STAGE_SCRIPT], ids=lambda p: p.name)
def test_platform_paths_appear_only_via_the_installer_call(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    for marker in _PLATFORM_INTERNAL_MARKERS:
        assert marker not in text, f"{script.name} references platform internal path {marker!r} directly"


def test_pigen_stage_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(PIGEN_STAGE_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_sh_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_sh_shellcheck_clean() -> None:
    result = subprocess.run(["shellcheck", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


# ---------------------------------------------------------------------------
# /run/palmimo ownership: the Portal (running as "user") creates
# /run/palmimo/apps/<name>/ itself at start time (design 2.1/3.5), so both
# directories must be writable by "user", not root-only.
# ---------------------------------------------------------------------------


def test_tmpfiles_conf_makes_run_palmimo_apps_writable_by_user() -> None:
    tmpfiles_conf = PLATFORM_DIR / "files" / "etc" / "tmpfiles.d" / "palmimo.conf"
    entries = {}
    for line in tmpfiles_conf.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        entries[fields[1]] = fields

    assert entries["/run/palmimo/apps"][3] == "user"


# ---------------------------------------------------------------------------
# add_user_to_group: never touch the host account DB, and never re-record
# membership a device (or a previous install) already has
# ---------------------------------------------------------------------------


def test_add_user_to_group_is_idempotent_when_membership_already_exists(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "group").write_text("palmimo-apps:x:999:user\n", encoding="utf-8")
    (root / "etc" / "passwd").write_text("user:x:1000:1000::/home/user:/bin/bash\n", encoding="utf-8")
    fake_accounts = tmp_path / "fake_accounts.txt"
    fake_accounts.touch()
    uv_source = _stub_uv(tmp_path)

    result = _run_install(root, fake_accounts, uv_source)

    assert result.returncode == 0, result.stderr
    assert "usermod" not in fake_accounts.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# bundle cache: what the Portal runs `install.sh verify` from later (at
# startup and periodically), per manifest owns.bundle_cache
# ---------------------------------------------------------------------------


def test_install_leaves_a_verifiable_bundle_cache(installed_root: tuple[Path, Path, Path]) -> None:
    root, fake_accounts, _uv_source = installed_root
    cached_install_sh = root / "var" / "lib" / "palmimo" / "platform" / "current" / "install.sh"
    assert cached_install_sh.is_file()

    result = subprocess.run(
        ["bash", str(cached_install_sh), "verify", "--root", str(root)],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PALMIMO_FAKE_ACCOUNTS": str(fake_accounts)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# .staging/.trash: the Portal (uid "user") needs group-write on these
# parents so palmimo-app-owned entries under them (created/rmdir'd as
# palmimo-app) can be renamed/removed by the Portal's own job bookkeeping.
# ---------------------------------------------------------------------------


def test_install_creates_staging_and_trash_setgid_for_the_apps_group(
    installed_root: tuple[Path, Path, Path],
) -> None:
    root, _fake_accounts, _uv_source = installed_root
    for name in ("staging", "trash"):
        path = root / "var" / "lib" / "palmimo" / "apps" / f".{name}"
        assert path.is_dir()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert oct(mode) == "0o2775", f".{name} has mode {oct(mode)}, expected 0o2775"


# ---------------------------------------------------------------------------
# palmimo-app-sync@.service: same sandbox as palmimo-app@.service, so an
# untrusted pyproject's build backend / path deps run confined the same way
# `uv run` does for the app itself
# ---------------------------------------------------------------------------

_SANDBOX_KEYS = ["ProtectSystem", "ProtectHome", "PrivateTmp", "NoNewPrivileges", "ProtectProc", "ProcSubset"]


def _parse_service_section(unit_path: Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in unit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key, []).append(value)
    return values


def test_app_sync_unit_sandbox_keys_match_app_unit() -> None:
    app = _parse_service_section(APP_UNIT)
    app_sync = _parse_service_section(APP_SYNC_UNIT)
    for key in _SANDBOX_KEYS:
        assert key in app, f"{APP_UNIT.name} is missing {key}"
        assert key in app_sync, f"{APP_SYNC_UNIT.name} is missing {key}"
        assert app[key] == app_sync[key], f"{key} differs: {app[key]!r} (app) vs {app_sync[key]!r} (app-sync)"


@pytest.mark.parametrize(
    "frozen, expected_sync_argv_tail",
    [
        (True, "--package mypkg --frozen"),
        # uv 0.11 has no --no-frozen flag ("unexpected argument"): omitting
        # --frozen entirely is what "resolve normally" means to uv.
        (False, "--package mypkg"),
    ],
    ids=["frozen", "not_frozen"],
)
def test_app_sync_helper_builds_expected_uv_sync_argv(
    tmp_path: Path, frozen: bool, expected_sync_argv_tail: str
) -> None:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    uv_log = tmp_path / "uv.log"
    (stub_dir / "uv").write_text('#!/bin/sh\necho "$@" >> "$FAKE_UV_LOG"\nexit 0\n', encoding="utf-8")
    (stub_dir / "uv").chmod(0o755)

    staging_dir = tmp_path / "staging"
    project_dir = tmp_path / "apps" / "myapp"
    (project_dir / ".venv").mkdir(parents=True)  # pre-existing venv: skip the `uv venv` step
    (staging_dir / "myapp").mkdir(parents=True)
    (staging_dir / "myapp" / "sync.json").write_text(
        json.dumps({"project": str(project_dir), "package": "mypkg", "frozen": frozen, "relocatable": True}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(APP_SYNC_HELPER), "myapp"],
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "PALMIMO_STAGING_DIR": str(staging_dir),
            "FAKE_UV_LOG": str(uv_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    logged = uv_log.read_text(encoding="utf-8").splitlines()
    assert logged == [
        f"sync --project {project_dir} --no-editable {expected_sync_argv_tail}",
        "cache prune",
    ]


def test_app_sync_helper_refuses_to_purge_a_path_outside_the_allowed_roots(tmp_path: Path) -> None:
    staging_dir = tmp_path / "apps" / ".staging"
    (staging_dir / "myapp").mkdir(parents=True)
    outside_target = tmp_path / "outside" / "secret"
    (outside_target / "sub").mkdir(parents=True)
    (outside_target / "sub" / "file").write_text("keep me", encoding="utf-8")
    (staging_dir / "myapp" / "sync.json").write_text(json.dumps({"purge": str(outside_target)}), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(APP_SYNC_HELPER), "myapp"],
        env={"PATH": "/usr/bin:/bin", "PALMIMO_STAGING_DIR": str(staging_dir)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert (outside_target / "sub" / "file").read_text(encoding="utf-8") == "keep me"


# ---------------------------------------------------------------------------
# app-launch: the Portal's needs_repair mapping (exit 78) and the exact uv
# invocation it execs into
# ---------------------------------------------------------------------------


def test_app_launch_exits_78_with_one_stderr_line_when_venv_is_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    project_dir = tmp_path / "apps" / "myapp"
    (run_dir / "myapp").mkdir(parents=True)
    project_dir.mkdir(parents=True)  # no .venv under it
    (run_dir / "myapp" / "argv.json").write_text(
        json.dumps({"argv": ["myapp"], "project": str(project_dir)}), encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, str(APP_LAUNCH_HELPER), "myapp"],
        env={"PATH": "/usr/bin:/bin", "PALMIMO_RUN_DIR": str(run_dir)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    assert len(result.stderr.splitlines()) == 1


def test_app_launch_execs_uv_run_frozen_no_sync(tmp_path: Path) -> None:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    uv_log = tmp_path / "uv.log"
    (stub_dir / "uv").write_text('#!/bin/sh\necho "$@" >> "$FAKE_UV_LOG"\nexit 0\n', encoding="utf-8")
    (stub_dir / "uv").chmod(0o755)

    run_dir = tmp_path / "run"
    project_dir = tmp_path / "apps" / "myapp"
    (project_dir / ".venv" / "bin").mkdir(parents=True)
    (project_dir / ".venv" / "bin" / "python").touch()
    (run_dir / "myapp").mkdir(parents=True)
    (run_dir / "myapp" / "argv.json").write_text(
        json.dumps({"argv": ["myapp", "--foo"], "project": str(project_dir)}), encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, str(APP_LAUNCH_HELPER), "myapp"],
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "PALMIMO_RUN_DIR": str(run_dir),
            "FAKE_UV_LOG": str(uv_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    logged = uv_log.read_text(encoding="utf-8").splitlines()
    assert logged == [f"run --frozen --no-sync --project {project_dir} -- myapp --foo"]


# ---------------------------------------------------------------------------
# release asset: a standalone manifest.json the Portal can poll without
# downloading the whole tarball
# ---------------------------------------------------------------------------


def test_build_platform_bundle_emits_manifest_asset_matching_its_sha256(tmp_path: Path) -> None:
    manifest_out = tmp_path / "palmimo-platform-v1.manifest.json"
    manifest_sha_out = tmp_path / "palmimo-platform-v1.manifest.json.sha256"
    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_BUNDLE_SCRIPT),
            "--out",
            str(tmp_path / "bundle.tar.gz"),
            "--manifest-out",
            str(manifest_out),
            "--manifest-sha256-out",
            str(manifest_sha_out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    assert manifest_out.read_bytes() == MANIFEST_PATH.read_bytes()
    recorded_sha256 = manifest_sha_out.read_text(encoding="utf-8").split()[0]
    assert recorded_sha256 == hashlib.sha256(manifest_out.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# verify's exit code is a contract the Portal reads -- 0 clean, 1 drift
# found, 2+ could not run at all
# ---------------------------------------------------------------------------


def test_verify_exit_code_is_1_for_drift_not_2(installed_root: tuple[Path, Path, Path]) -> None:
    root, fake_accounts, _uv_source = installed_root
    tampered = root / "etc" / "tmpfiles.d" / "palmimo.conf"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")

    result = _run_install(root, fake_accounts, Path(), command="verify")

    assert result.returncode == 1


def test_verify_exit_code_is_2_when_it_cannot_run() -> None:
    result = subprocess.run(
        [sys.executable, str(VERIFY_TOOL), "/no/such/manifest.json", "/no/such/files", "/no/such/root"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# check_platform_version.py: a content change to platform/ must come with a
# manifest.json version bump, or the Portal's version-number comparison
# never notices the change exists.
# ---------------------------------------------------------------------------

CHECK_VERSION_TOOL = REPO_ROOT / "tools" / "check_platform_version.py"


def _make_platform_dir(root: Path, *, version: int, extra: str = "unchanged") -> Path:
    platform_dir = root / "platform"
    (platform_dir / "files").mkdir(parents=True)
    (platform_dir / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (platform_dir / "files" / "a.txt").write_text(extra, encoding="utf-8")
    return platform_dir


@pytest.mark.parametrize(
    "base_version, head_version, base_extra, head_extra, expected_returncode",
    [
        (1, 1, "same", "same", 0),
        (1, 2, "same", "different", 0),
        (1, 1, "same", "different", 1),
    ],
    ids=["identical_content_same_version", "changed_content_bumped", "changed_content_not_bumped"],
)
def test_check_platform_version_returncode(
    tmp_path: Path,
    base_version: int,
    head_version: int,
    base_extra: str,
    head_extra: str,
    expected_returncode: int,
) -> None:
    base_dir = _make_platform_dir(tmp_path / "base", version=base_version, extra=base_extra)
    head_dir = _make_platform_dir(tmp_path / "head", version=head_version, extra=head_extra)

    result = subprocess.run(
        ["python3", str(CHECK_VERSION_TOOL), str(base_dir), str(head_dir)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == expected_returncode, result.stdout + result.stderr
    if expected_returncode == 1:
        assert str(base_version) in result.stderr
        assert str(head_version) in result.stderr


def test_check_platform_version_ignores_manifest_formatting_and_version_field(tmp_path: Path) -> None:
    base_dir = _make_platform_dir(tmp_path / "base", version=1)
    head_dir = _make_platform_dir(tmp_path / "head", version=1)
    # Reformat head's manifest.json (different whitespace, different version
    # value) without touching any other file -- content is unchanged.
    head_manifest = head_dir / "manifest.json"
    head_manifest.write_text(json.dumps({"version": 1}, indent=2) + "\n", encoding="utf-8")

    result = subprocess.run(
        ["python3", str(CHECK_VERSION_TOOL), str(base_dir), str(head_dir)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "relpath",
    ["README.md", "files/__pycache__/a.cpython-312.pyc", "__pycache__/verify_platform.cpython-312.pyc"],
)
def test_check_platform_version_ignores_files_the_bundle_does_not_ship(tmp_path: Path, relpath: str) -> None:
    base_dir = _make_platform_dir(tmp_path / "base", version=1)
    head_dir = _make_platform_dir(tmp_path / "head", version=1)
    extra = head_dir / relpath
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"not shipped")

    result = subprocess.run(
        ["python3", str(CHECK_VERSION_TOOL), str(base_dir), str(head_dir)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_check_platform_version_exits_2_on_missing_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        ["python3", str(CHECK_VERSION_TOOL), str(tmp_path / "no-such-base"), str(tmp_path / "no-such-head")],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
