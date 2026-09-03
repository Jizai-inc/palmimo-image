"""Contracts for this repository -- the out-of-band inputs to the shipped
image.

See doc/design.md for the design; this module pins only the parts a static
check can verify (unit contents, script syntax, the shared device_id regex
between firstboot.sh and make_identity.py, the identity file's round-trip
shape). On-device behavior (V1-V9 in the design doc) is not and cannot be
covered here.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIR = REPO_ROOT
FILES_DIR = IMAGE_DIR / "files"

PORTAL_UNIT = FILES_DIR / "etc" / "systemd" / "system" / "palmimo-portal.service"
FIRSTBOOT_UNIT = FILES_DIR / "etc" / "systemd" / "system" / "palmimo-firstboot.service"
POLKIT_RULES = FILES_DIR / "etc" / "polkit-1" / "rules.d" / "50-palmimo-portal.rules"
COMITUP_CONF = FILES_DIR / "etc" / "comitup.conf"
COMITUP_WEB_UNIT = FILES_DIR / "etc" / "systemd" / "system" / "comitup-web.service"
FIRSTBOOT_SCRIPT = FILES_DIR / "usr" / "local" / "lib" / "palmimo" / "firstboot.sh"
APPLY_SCRIPT = IMAGE_DIR / "apply-pi.sh"
MAKE_IDENTITY_SCRIPT = IMAGE_DIR / "tools" / "make_identity.py"
PACKAGES_TXT = IMAGE_DIR / "packages.txt"
PATCH_NM_PY_LIB = IMAGE_DIR / "lib" / "patch_comitup_nm.py"
ROOT_README = IMAGE_DIR / "README.md"

PIGEN_DIR = IMAGE_DIR / "pigen"
PIGEN_README = PIGEN_DIR / "README.md"
PIGEN_CONFIG = PIGEN_DIR / "config"
STAGE_DIR = PIGEN_DIR / "stage-palmimo"
STAGE_EXPORT_IMAGE = STAGE_DIR / "EXPORT_IMAGE"
STAGE_PRERUN = STAGE_DIR / "prerun.sh"
STAGE_CORE_RUN = STAGE_DIR / "01-palmimo-core" / "00-run.sh"
STAGE_ACCOUNT_RUN = STAGE_DIR / "02-account-ssh" / "00-run.sh"
STAGE_SUDOERS_FILE = STAGE_DIR / "02-account-ssh" / "files" / "010-palmimo-user"
STAGE_SSHD_DROPIN = STAGE_DIR / "02-account-ssh" / "files" / "50-palmimo-key-only.conf"
STAGE_PORTAL_RUN = STAGE_DIR / "03-portal" / "00-run.sh"

PIGEN_SHELL_SCRIPTS = [STAGE_PRERUN, STAGE_CORE_RUN, STAGE_ACCOUNT_RUN, STAGE_PORTAL_RUN]


def _text(path: Path) -> str:
    assert path.is_file(), f"expected file missing: {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# palmimo-portal.service
# ---------------------------------------------------------------------------


def test_portal_unit_pins_the_confirmed_contract() -> None:
    text = _text(PORTAL_UNIT)
    assert "RequiresMountsFor=/boot/firmware" in text
    assert "Environment=PALMIMO_PORT=80" in text
    assert "Environment=PALMIMO_ADAPTERS=real" in text
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in text
    assert "ExecStart=/home/user/palmimo-portal/.venv/bin/python -m palmimo_portal" in text
    assert "User=user" in text


# ---------------------------------------------------------------------------
# palmimo-firstboot.service
# ---------------------------------------------------------------------------


def test_firstboot_unit_has_both_conditions_and_before_ordering() -> None:
    text = _text(FIRSTBOOT_UNIT)
    assert "ConditionPathExists=/boot/firmware/palmimo-identity.json" in text
    assert "ConditionPathExists=!/var/lib/palmimo/firstboot-done" in text

    before_match = re.search(r"^Before=(.+)$", text, re.MULTILINE)
    assert before_match is not None, "no Before= line in palmimo-firstboot.service"
    before_units = set(before_match.group(1).split())
    assert before_units == {
        "comitup.service",
        "avahi-daemon.service",
        "palmimo-portal.service",
    }


# ---------------------------------------------------------------------------
# device_id regex agreement between firstboot.sh and make_identity.py
# ---------------------------------------------------------------------------

_FIRSTBOOT_DEVICE_ID_REGEX_LINE = re.compile(r"""^DEVICE_ID_REGEX=(['"])(?P<pattern>.*)\1\s*$""", re.MULTILINE)
_MAKE_IDENTITY_DEVICE_ID_REGEX_LINE = re.compile(r'^DEVICE_ID_PATTERN\s*=\s*r"(?P<pattern>.*)"\s*$', re.MULTILINE)
_FIRSTBOOT_PASSWORD_REGEX_LINE = re.compile(r"""^PASSWORD_REGEX=(['"])(?P<pattern>.*)\1\s*$""", re.MULTILINE)
_MAKE_IDENTITY_PASSWORD_REGEX_LINE = re.compile(r'^PASSWORD_PATTERN\s*=\s*r"(?P<pattern>.*)"\s*$', re.MULTILINE)


def _extract_firstboot_device_id_pattern() -> str:
    match = _FIRSTBOOT_DEVICE_ID_REGEX_LINE.search(_text(FIRSTBOOT_SCRIPT))
    assert match is not None, "could not find DEVICE_ID_REGEX=... in firstboot.sh"
    return match.group("pattern")


def _extract_make_identity_device_id_pattern() -> str:
    match = _MAKE_IDENTITY_DEVICE_ID_REGEX_LINE.search(_text(MAKE_IDENTITY_SCRIPT))
    assert match is not None, 'could not find DEVICE_ID_PATTERN = r"..." in make_identity.py'
    return match.group("pattern")


def _extract_firstboot_password_pattern() -> str:
    match = _FIRSTBOOT_PASSWORD_REGEX_LINE.search(_text(FIRSTBOOT_SCRIPT))
    assert match is not None, "could not find PASSWORD_REGEX=... in firstboot.sh"
    return match.group("pattern")


def _extract_make_identity_password_pattern() -> str:
    match = _MAKE_IDENTITY_PASSWORD_REGEX_LINE.search(_text(MAKE_IDENTITY_SCRIPT))
    assert match is not None, 'could not find PASSWORD_PATTERN = r"..." in make_identity.py'
    return match.group("pattern")


def test_device_id_regex_matches_between_firstboot_and_make_identity() -> None:
    firstboot_pattern = _extract_firstboot_device_id_pattern()
    make_identity_pattern = _extract_make_identity_device_id_pattern()
    assert firstboot_pattern == make_identity_pattern, (
        f"firstboot.sh's DEVICE_ID_REGEX ({firstboot_pattern!r}) and "
        f"make_identity.py's DEVICE_ID_PATTERN ({make_identity_pattern!r}) "
        "have drifted apart"
    )
    # Sanity: the shared pattern is the one the design doc specifies.
    assert firstboot_pattern == r"^[a-z0-9-]{1,32}$"


def test_password_regex_matches_between_firstboot_and_make_identity() -> None:
    firstboot_pattern = _extract_firstboot_password_pattern()
    make_identity_pattern = _extract_make_identity_password_pattern()
    assert firstboot_pattern == make_identity_pattern, (
        f"firstboot.sh's PASSWORD_REGEX ({firstboot_pattern!r}) and "
        f"make_identity.py's PASSWORD_PATTERN ({make_identity_pattern!r}) "
        "have drifted apart"
    )
    # Sanity: the shared pattern is the manufacturing sticker alphabet the
    # design doc specifies (WPA2-PSK length bound, alphanumeric only).
    assert firstboot_pattern == r"^[A-Za-z0-9]{8,63}$"


# ---------------------------------------------------------------------------
# make_identity.py round-trip
# ---------------------------------------------------------------------------


def test_make_identity_writes_v2_identity_file_with_restrictive_perms() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "palmimo-identity.json"
        result = subprocess.run(
            [
                "uv",
                "run",
                str(MAKE_IDENTITY_SCRIPT),
                "--device-id",
                "405",
                "--password",
                "s3cr3tplain9",
                "--out",
                str(out_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"make_identity.py failed: {result.stderr}"
        assert out_path.is_file()

        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data == {"device_id": "405", "initial_password": "s3cr3tplain9"}

        mode = out_path.stat().st_mode & 0o777
        assert mode == 0o600, f"expected mode 0600, got {oct(mode)}"


def test_make_identity_rejects_invalid_device_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "palmimo-identity.json"
        result = subprocess.run(
            [
                "uv",
                "run",
                str(MAKE_IDENTITY_SCRIPT),
                "--device-id",
                "Not Valid!",
                "--password",
                "validpass1",
                "--out",
                str(out_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0
        assert not out_path.exists()


@pytest.mark.parametrize(
    "password",
    ["short7x", "has|pipe1"],
    ids=["too_short", "contains_pipe"],
)
def test_make_identity_rejects_invalid_password(password: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "palmimo-identity.json"
        result = subprocess.run(
            [
                "uv",
                "run",
                str(MAKE_IDENTITY_SCRIPT),
                "--device-id",
                "405",
                "--password",
                password,
                "--out",
                str(out_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode != 0, f"expected rejection of password {password!r}"
        assert not out_path.exists()


# ---------------------------------------------------------------------------
# apply-pi.sh: shell syntax
# ---------------------------------------------------------------------------


def test_apply_pi_sh_passes_bash_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(APPLY_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_firstboot_sh_passes_bash_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(FIRSTBOOT_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


@pytest.mark.parametrize("script", PIGEN_SHELL_SCRIPTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_pigen_stage_script_passes_bash_syntax_check(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# shellcheck (best-effort — skipped when the tool isn't installed locally)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize(
    "script",
    [APPLY_SCRIPT, FIRSTBOOT_SCRIPT, *PIGEN_SHELL_SCRIPTS],
    ids=lambda p: p.name if p.name != "00-run.sh" else f"{p.parent.name}/{p.name}",
)
def test_shellcheck_clean(script: Path) -> None:
    result = subprocess.run(
        ["shellcheck", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"shellcheck found issues in {script.name}:\n{result.stdout}"


# ---------------------------------------------------------------------------
# comitup.conf
# ---------------------------------------------------------------------------


def test_comitup_conf_has_hostname_placeholder_and_nuke_enabled() -> None:
    text = _text(COMITUP_CONF)
    assert "ap_name: <hostname>" in text
    assert "enable_nuke: true" in text
    # ap_password must NOT be baked into the shared file as an active key —
    # firstboot sets it per device from the identity file (identity file spec
    # v2). Only the explanatory comment may mention the key name.
    assert not re.search(r"^\s*ap_password\s*:", text, re.MULTILINE)


# ---------------------------------------------------------------------------
# comitup-web.service: no-op replacement, not a mask
# ---------------------------------------------------------------------------


def test_comitup_web_unit_is_a_noop_replacement() -> None:
    text = _text(COMITUP_WEB_UNIT)
    assert "ExecStart=/bin/true" in text
    assert "RemainAfterExit=yes" in text
    assert "Type=oneshot" in text


# ---------------------------------------------------------------------------
# apply-pi.sh: on-device-verified hotspot fixes
# (dnsmasq, comitup-web unmask-not-mask, the nm.py WPA2/PMF patch)
# ---------------------------------------------------------------------------


def test_apply_pi_sh_installs_dnsmasq_and_disables_the_system_service() -> None:
    text = _text(APPLY_SCRIPT)
    assert "dnsmasq" in text
    assert "systemctl disable --now dnsmasq" in text


def test_apply_pi_sh_unmasks_comitup_web_and_does_not_mask_it() -> None:
    text = _text(APPLY_SCRIPT)
    assert "systemctl unmask comitup-web" in text
    assert "systemctl mask comitup-web" not in text


# ---------------------------------------------------------------------------
# packages.txt: the single apt package list, shared by apply-pi.sh and the
# pi-gen stage (no inline list left in either consumer)
# ---------------------------------------------------------------------------


def test_packages_txt_is_the_one_list_apply_pi_sh_references() -> None:
    packages_text = _text(PACKAGES_TXT)
    packages = [line.strip() for line in packages_text.splitlines() if line.strip()]
    assert packages == ["comitup", "avahi-daemon", "git", "dnsmasq"]

    apply_text = _text(APPLY_SCRIPT)
    assert 'PACKAGES_FILE="${IMAGE_DIR}/packages.txt"' in apply_text
    assert "$PACKAGES_FILE" in apply_text
    # No inline apt package list left over in apply-pi.sh.
    assert "apt-get install -y comitup" not in apply_text


# ---------------------------------------------------------------------------
# lib/patch_comitup_nm.py: the shared WPA2/PMF nm.py patch, referenced (not
# duplicated) by apply-pi.sh
# ---------------------------------------------------------------------------


def test_patch_comitup_nm_py_has_all_four_security_keys_and_fails_loud() -> None:
    text = _text(PATCH_NM_PY_LIB)
    for key in ('"proto"', '"pairwise"', '"group"', '"pmf"'):
        assert key in text, f"patch_comitup_nm.py is missing the {key} key"
    assert "dbus.Int32(1)" in text
    # Idempotency key is now the GPLv2 section 2(a) modification notice, not
    # the bare presence of "pmf" (which the notice's own prose would satisfy
    # too, defeating the point of a dedicated marker).
    assert 'NOTICE_MARKER = "Modified by Jizai Inc."' in text
    assert "if NOTICE_MARKER in text:" in text
    assert "sys.exit(0)" in text
    assert "GPLv2 section 2(a)" in text
    # The fail-loud path: a missing anchor must exit non-zero with a message
    # naming the comitup version this patch targets, not fail silently.
    assert "FAIL: expected anchor lines not found" in text
    assert "comitup 1.43" in text
    assert "sys.exit(1)" in text


def test_patch_comitup_nm_py_passes_py_compile() -> None:
    result = subprocess.run(
        ["python3", "-m", "py_compile", str(PATCH_NM_PY_LIB)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"py_compile failed: {result.stderr}"


_NM_PY_ANCHOR = (
    '        settings["802-11-wireless-security"]["key-mgmt"] = "wpa-psk"\n'
    '        settings["802-11-wireless-security"]["psk"] = password\n'
)

_NM_PY_OLD_PATCHED_LINES = (
    '        settings["802-11-wireless-security"]["proto"] = ["rsn"]\n'
    '        settings["802-11-wireless-security"]["pairwise"] = ["ccmp"]\n'
    '        settings["802-11-wireless-security"]["group"] = ["ccmp"]\n'
    '        settings["802-11-wireless-security"]["pmf"] = dbus.Int32(1)\n'
)


def _fake_nm_py_text(*, with_old_patch: bool = False) -> str:
    header = (
        "def make_hotspot(self, ssid, password):\n"
        "    with self.make_settings() as settings:\n"
        '        settings["connection"]["id"] = ssid\n'
    )
    body = _NM_PY_ANCHOR
    if with_old_patch:
        body += _NM_PY_OLD_PATCHED_LINES
    footer = "        return settings\n"
    return header + body + footer


def test_patch_comitup_nm_py_adds_notice_and_keys_when_fresh(tmp_path: Path) -> None:
    nm_py = tmp_path / "nm.py"
    nm_py.write_text(_fake_nm_py_text(), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PATCH_NM_PY_LIB), str(nm_py)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "patched:" in result.stdout

    text = nm_py.read_text(encoding="utf-8")
    today = datetime.date.today().isoformat()
    notice_line = f"# Modified by Jizai Inc. on {today}:"

    anchor_end = text.index(_NM_PY_ANCHOR) + len(_NM_PY_ANCHOR)
    notice_idx = text.index(notice_line)
    gplv2_idx = text.index("(GPLv2 section 2(a) notice)")
    proto_idx = text.index('["proto"]')
    pairwise_idx = text.index('["pairwise"]')
    group_idx = text.index('["group"]')
    pmf_idx = text.index('["pmf"]')

    assert anchor_end <= notice_idx < gplv2_idx < proto_idx < pairwise_idx < group_idx < pmf_idx

    for key in ('["proto"]', '["pairwise"]', '["group"]', '["pmf"]'):
        assert text.count(key) == 1, f"{key} should appear exactly once"


def test_patch_comitup_nm_py_is_a_noop_when_run_twice(tmp_path: Path) -> None:
    nm_py = tmp_path / "nm.py"
    nm_py.write_text(_fake_nm_py_text(), encoding="utf-8")

    first = subprocess.run(
        [sys.executable, str(PATCH_NM_PY_LIB), str(nm_py)],
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    patched_bytes = nm_py.read_bytes()

    second = subprocess.run(
        [sys.executable, str(PATCH_NM_PY_LIB), str(nm_py)],
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    assert "already patched" in second.stdout
    assert nm_py.read_bytes() == patched_bytes


def test_patch_comitup_nm_py_upgrades_old_patch_when_notice_missing(
    tmp_path: Path,
) -> None:
    nm_py = tmp_path / "nm.py"
    nm_py.write_text(_fake_nm_py_text(with_old_patch=True), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PATCH_NM_PY_LIB), str(nm_py)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "upgraded" in result.stdout

    text = nm_py.read_text(encoding="utf-8")
    assert "Modified by Jizai Inc." in text
    for key in ('["proto"]', '["pairwise"]', '["group"]', '["pmf"]'):
        assert text.count(key) == 1, f"{key} should appear exactly once"


def test_patch_comitup_nm_py_fails_loud_when_anchor_missing(tmp_path: Path) -> None:
    nm_py = tmp_path / "nm.py"
    nm_py.write_text("def make_hotspot(self, ssid, password):\n    pass\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PATCH_NM_PY_LIB), str(nm_py)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "comitup 1.43" in result.stderr


def test_apply_pi_sh_pipes_the_shared_patch_script_over_ssh() -> None:
    text = _text(APPLY_SCRIPT)
    assert 'PATCH_NM_PY_SCRIPT="${IMAGE_DIR}/lib/patch_comitup_nm.py"' in text
    assert "'sudo python3 -' <\"$PATCH_NM_PY_SCRIPT\"" in text
    # No inline heredoc copy of the patch left over in apply-pi.sh (the old
    # heredoc delimiter -- distinct from the PATCH_NM_PY_SCRIPT variable name
    # this refactor introduces).
    assert '<<"PATCH_NM_PY"' not in text


def test_tag_validation_regex_literal_present_in_apply_pi_and_portal_stage() -> None:
    # Same regex literal, same hardening, in both consumers of a
    # user-supplied tag (apply-pi.sh over SSH, the pi-gen chroot stage).
    assert "[A-Za-z0-9._-]" in _text(APPLY_SCRIPT)
    assert "[A-Za-z0-9._-]" in _text(STAGE_PORTAL_RUN)


def test_apply_pi_sh_rejects_a_shell_injection_attempt_in_portal_tag(
    tmp_path: Path,
) -> None:
    # A malicious PORTAL_TAG must be rejected before apply-pi.sh ever
    # reaches ssh -- prove it by putting a fake `ssh` on PATH that leaves a
    # marker file, and asserting the marker is never created.
    marker = tmp_path / "ssh-was-invoked"
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        f'#!/bin/sh\ntouch "{marker}"\nexit 0\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    env = dict(**{"PATH": f"{tmp_path}:{Path('/usr/bin')}:{Path('/bin')}"})
    result = subprocess.run(
        ["bash", str(APPLY_SCRIPT)],
        cwd=IMAGE_DIR,
        env={
            **env,
            "PI_HOST": "x",
            "PORTAL_TAG": "v1'; echo pwned",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "PORTAL_TAG" in combined
    assert not marker.exists()


# ---------------------------------------------------------------------------
# polkit rules
# ---------------------------------------------------------------------------


def test_polkit_rules_grant_only_the_documented_actions() -> None:
    text = _text(POLKIT_RULES)
    assert "org.freedesktop.login1.reboot" in text
    assert "org.freedesktop.login1.power-off" in text
    assert "org.freedesktop.systemd1.manage-units" in text
    assert "palmimo-portal.service" in text
    assert "palmimo-set-wifi-country@" in text
    # comitup is explicitly not granted yet (see the design doc's 未決 note).
    assert "org.freedesktop.DBus" not in text or "comitup" in text.lower()


# ---------------------------------------------------------------------------
# files/ tree: every entry maps to an absolute path, none world-writable
# ---------------------------------------------------------------------------


def test_files_tree_entries_are_not_world_writable() -> None:
    assert FILES_DIR.is_dir()
    offenders = []
    for path in FILES_DIR.rglob("*"):
        if path.is_file():
            mode = path.stat().st_mode
            if mode & 0o002:
                offenders.append(str(path.relative_to(FILES_DIR)))
    assert not offenders, f"world-writable files under files/: {offenders}"


def test_apply_pi_sh_rsyncs_the_files_tree_to_root() -> None:
    text = _text(APPLY_SCRIPT)
    assert '"$FILES_SRC" "${PI_HOST}:/"' in text
    assert 'FILES_SRC="${IMAGE_DIR}/files/"' in text


# ---------------------------------------------------------------------------
# NM dispatcher hook: re-register avahi's records after the AP->STA switch
# ---------------------------------------------------------------------------

AVAHI_DISPATCHER = FILES_DIR / "etc/NetworkManager/dispatcher.d/50-palmimo-avahi"


def test_avahi_dispatcher_restart_is_sta_only_and_never_propagates() -> None:
    # #683: on HOTSPOT -> CONNECTED, avahi's IPv4 re-registration can fail
    # with "Local name collision", leaving <hostname>.local IPv6-only right
    # when the post-setup handoff needs it; a restart after the address
    # settles re-registers cleanly. Two hard-won constraints (on-device,
    # 2026-08-21): (1) comitup.service has Requires=avahi-daemon.service,
    # so a plain restart propagates a comitup kill, and on the hotspot
    # every comitup start re-fires this hook -- a loop that leaves comitup
    # start-limit-dead; --job-mode=ignore-dependencies queues no dependent
    # jobs, so comitup is never touched. (2) There is no sound local
    # "is the record broken" detector: getent/avahi on the machine itself
    # answer from the loopback record, not the wlan0 record clients need,
    # so the restart is unconditional on the STA side instead.
    text = _text(AVAHI_DISPATCHER)
    assert text.startswith("#!/bin/sh\n")
    assert '[ "$1" = "wlan0" ] || exit 0' in text
    assert '[ "$2" = "up" ] || exit 0' in text
    hotspot_guard = text.index('case "$IP4_ADDRESS_0" in 10.41.*) exit 0 ;; esac')
    restart = text.index("systemctl restart --job-mode=ignore-dependencies avahi-daemon.service")
    assert hotspot_guard < restart
    assert "systemctl restart avahi-daemon" not in text.replace(
        "systemctl restart --job-mode=ignore-dependencies avahi-daemon", ""
    )


def test_avahi_dispatcher_is_executable() -> None:
    # NetworkManager silently skips non-executable dispatcher scripts.
    assert AVAHI_DISPATCHER.stat().st_mode & 0o111 == 0o111


# ---------------------------------------------------------------------------
# pi-gen custom stage (pigen/)
# ---------------------------------------------------------------------------


def test_pigen_readme_and_config_exist() -> None:
    assert PIGEN_README.is_file()
    assert PIGEN_CONFIG.is_file()


def test_pigen_config_pins_the_required_keys() -> None:
    text = _text(PIGEN_CONFIG)
    assert "IMG_NAME=palmimo" in text
    assert "RELEASE=trixie" in text
    assert "DEPLOY_COMPRESSION=xz" in text
    assert "FIRST_USER_NAME=user" in text
    assert "ENABLE_SSH=1" in text
    assert "WPA_COUNTRY=JP" in text
    assert "PALMIMO_IMAGE_DIR=" in text
    stage_list_match = re.search(r'^STAGE_LIST="(?P<value>.+)"\s*$', text, re.MULTILINE)
    assert stage_list_match is not None, 'no STAGE_LIST="..." line in pigen/config'
    stages = stage_list_match.group("value").split()
    assert stages[-1] == "stage-palmimo", "stage-palmimo must be the last stage run"
    assert "stage2" in stages
    # FIRST_USER_PASS is a throwaway that 02-account-ssh locks away; its
    # presence is required by DISABLE_FIRST_BOOT_USER_RENAME=1 (see
    # test_config_disables_first_boot_rename_with_a_throwaway_password).
    # No baked-in authorized_keys: Portal is the key-registration path.
    assert not re.search(r"^\s*PUBKEY_SSH_FIRST_USER=", text, re.MULTILINE)


def test_pigen_config_pins_the_portal_tag_default() -> None:
    text = _text(PIGEN_CONFIG)
    assert "${PALMIMO_PORTAL_TAG:-v0.1.0}" in text


def test_pigen_stage_marks_export_image() -> None:
    assert STAGE_EXPORT_IMAGE.is_file()


def test_pigen_prerun_generates_00_packages_from_the_shared_list() -> None:
    text = _text(STAGE_PRERUN)
    assert "packages.txt" in text
    assert '"${STAGE_DIR}/00-packages/00-packages"' in text
    # Not checked in: the generated file itself must not exist in the tree.
    assert not (STAGE_DIR / "00-packages" / "00-packages").exists()


def test_pigen_core_step_references_the_shared_patch_script_and_files_tree() -> None:
    text = _text(STAGE_CORE_RUN)
    assert "patch_comitup_nm.py" in text
    assert 'rsync -a "${PALMIMO_IMAGE_DIR}/files/" "${ROOTFS_DIR}/"' in text


def test_pigen_core_step_enables_exactly_the_four_units_and_never_touches_comitup_web_state() -> None:
    text = _text(STAGE_CORE_RUN)
    enable_match = re.search(r"^\s*systemctl enable (?P<units>.+)$", text, re.MULTILINE)
    assert enable_match is not None, "no `systemctl enable ...` line in the pi-gen core step"
    enabled_units = set(enable_match.group("units").split())
    assert enabled_units == {
        "comitup",
        "avahi-daemon",
        "palmimo-portal",
        "palmimo-firstboot",
    }
    assert "comitup-web" not in enable_match.group("units")
    assert "systemctl mask comitup-web" not in text
    assert "systemctl unmask comitup-web" in text
    assert "systemctl disable dnsmasq" in text


def test_pigen_account_step_locks_password_and_installs_sudoers_and_sshd_dropins() -> None:
    text = _text(STAGE_ACCOUNT_RUN)
    assert 'passwd -l "${FIRST_USER_NAME}"' in text
    assert "visudo -c -f /etc/sudoers.d/010-palmimo-user" in text
    assert "/etc/sudoers.d/010-palmimo-user" in text
    assert "/etc/ssh/sshd_config.d/50-palmimo-key-only.conf" in text

    sudoers_text = _text(STAGE_SUDOERS_FILE)
    assert "NOPASSWD: ALL" in sudoers_text
    # The checked-in source file's own mode doesn't matter -- `install -m
    # 0440` sets the deployed mode explicitly at copy time (checked below).
    assert "install -m 0440" in text

    sshd_dropin_text = _text(STAGE_SSHD_DROPIN)
    assert "PasswordAuthentication no" in sshd_dropin_text
    assert "KbdInteractiveAuthentication no" in sshd_dropin_text


def test_pigen_portal_step_matches_apply_pi_sh_fetch_static_invocation() -> None:
    text = _text(STAGE_PORTAL_RUN)
    assert "${PALMIMO_PORTAL_TAG:-v0.1.0}" in text
    assert "https://github.com/Jizai-inc/palmimo-portal.git" in text
    assert "sync --frozen --no-dev" in text
    assert 'UV_BIN="${PORTAL_HOME}/.local/bin/uv"' in text
    assert "python -m palmimo_portal.fetch_static --tag" in text
    # Same ExecStart target the design doc pins for palmimo-portal.service.
    assert 'PORTAL_DEST="${PORTAL_HOME}/palmimo-portal"' in text


def test_account_stage_sets_a_real_shell_before_locking() -> None:
    # pi-gen leaves the passwordless first user on /usr/sbin/nologin
    # (stage1 only sets bash when FIRST_USER_PASS is set) -- without this
    # both device SSH and the portal stage's su calls break.
    text = _text(PIGEN_DIR / "stage-palmimo/02-account-ssh/00-run.sh")
    assert 'usermod -s /bin/bash "${FIRST_USER_NAME}"' in text


def test_cloud_init_is_kept_away_from_accounts_and_hostname() -> None:
    # cloud-init's default_user (pi) would otherwise be created at first
    # boot, and hostname management would fight palmimo-firstboot.
    text = _text(FILES_DIR / "etc/cloud/cloud.cfg.d/99-palmimo.cfg")
    assert "users: []" in text
    assert "preserve_hostname: true" in text


def test_every_enabled_unit_ships_an_install_section() -> None:
    # `systemctl enable` on a unit with no [Install] is a silent no-op, and
    # `is-enabled` exits 0 for the resulting "static" -- palmimo-firstboot
    # shipped exactly this way and would never have run on a virgin image.
    for unit in ("palmimo-portal.service", "palmimo-firstboot.service"):
        text = _text(FILES_DIR / "etc/systemd/system" / unit)
        assert "[Install]" in text and "WantedBy=multi-user.target" in text, unit


def test_apply_pi_self_check_string_compares_is_enabled() -> None:
    text = _text(APPLY_SCRIPT)
    assert """!= "enabled" ]""" in text
    assert "is-enabled --quiet" not in text


def test_pigen_stage_run_scripts_are_executable() -> None:
    # pi-gen silently skips a non-executable NN-run.sh ("Skip ... not
    # executable") -- a mode regression would drop a whole substage.
    for script in [*sorted(STAGE_DIR.glob("*/*-run.sh")), STAGE_DIR / "prerun.sh"]:
        assert script.stat().st_mode & 0o111 == 0o111, script


def test_account_stage_disables_the_first_boot_user_wizard() -> None:
    # userconfig.service is the interactive tty8 account create/rename
    # wizard; the shipped account is fully provisioned at build time.
    text = _text(STAGE_DIR / "02-account-ssh/00-run.sh")
    assert "systemctl disable userconfig.service" in text


def test_sudoers_dropin_passes_visudo_when_available() -> None:
    import shutil
    import subprocess

    visudo = shutil.which("visudo") or "/usr/sbin/visudo"
    if not Path(visudo).exists():
        pytest.skip("visudo not available on this host")
    path = STAGE_DIR / "02-account-ssh/files/010-palmimo-user"
    result = subprocess.run([visudo, "-c", "-f", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_config_disables_first_boot_rename_with_a_throwaway_password() -> None:
    # pi-gen's export-image re-arms userconfig.service AFTER every stage
    # unless DISABLE_FIRST_BOOT_USER_RENAME=1, and that flag demands a
    # non-empty FIRST_USER_PASS; 02-account-ssh locks the account after
    # stage1 applies it, so the throwaway value never survives.
    text = _text(PIGEN_CONFIG)
    assert "DISABLE_FIRST_BOOT_USER_RENAME=1" in text
    assert re.search(r"^FIRST_USER_PASS=.+$", text, re.MULTILINE)
    stage = _text(STAGE_DIR / "02-account-ssh/00-run.sh")
    assert 'passwd -l "${FIRST_USER_NAME}"' in stage


def test_dist_images_are_gitignored() -> None:
    # A 600MB image must never be committable; the dist/ convention only
    # works if the ignore rule survives.
    text = _text(REPO_ROOT / ".gitignore")
    assert "dist/" in text


# ---------------------------------------------------------------------------
# tools/make_image.py's gitignored workspace
# ---------------------------------------------------------------------------

WORKSPACE_GITIGNORE = PIGEN_DIR / ".workspace" / ".gitignore"


def test_workspace_gitignore_is_exactly_the_self_ignoring_pattern() -> None:
    # The *only* ignore mechanism for pigen/.workspace/ is this
    # self-ignoring file: "*" ignores everything the directory will ever
    # hold (the pi-gen clone, build.log, ...) while "!.gitignore" keeps this
    # one file trackable so the ignore rule itself ships.
    assert WORKSPACE_GITIGNORE.is_file()
    lines = _text(WORKSPACE_GITIGNORE).splitlines()
    assert lines == ["*", "!.gitignore"]


# ---------------------------------------------------------------------------
# /boot/firmware/licenses/: static third-party license texts and notices
# (MIT/BSD/Apache/OFL binary-distribution requirement -- see README.md,
# "Licenses and corresponding source")
# ---------------------------------------------------------------------------

BOOT_LICENSES_DIR = FILES_DIR / "boot" / "firmware" / "licenses"
DISPLAY_FW_LICENSES_DIR = BOOT_LICENSES_DIR / "display-firmware"
DISPLAY_FW_NOTICE = DISPLAY_FW_LICENSES_DIR / "NOTICE"

_STATIC_LICENSE_FILES = [
    BOOT_LICENSES_DIR / "README.txt",
    DISPLAY_FW_NOTICE,
    DISPLAY_FW_LICENSES_DIR / "licenses" / "Apache-2.0.txt",
    DISPLAY_FW_LICENSES_DIR / "licenses" / "BSD-3-Clause.txt",
    DISPLAY_FW_LICENSES_DIR / "licenses" / "BSD-3-Clause-STMicroelectronics.txt",
    DISPLAY_FW_LICENSES_DIR / "licenses" / "OFL-1.1.txt",
    DISPLAY_FW_LICENSES_DIR / "licenses" / "MIT-TinyUSB.txt",
    BOOT_LICENSES_DIR / "tools" / "uv" / "LICENSE-APACHE",
    BOOT_LICENSES_DIR / "tools" / "uv" / "LICENSE-MIT",
]


@pytest.mark.parametrize("path", _STATIC_LICENSE_FILES, ids=lambda p: p.relative_to(BOOT_LICENSES_DIR).as_posix())
def test_boot_licenses_tree_carries_the_static_notices(path: Path) -> None:
    assert path.is_file(), f"expected license file missing: {path}"
    assert path.stat().st_size > 0, f"license file is empty: {path}"


def test_display_firmware_notice_names_the_binary_linked_components() -> None:
    text = _text(DISPLAY_FW_NOTICE)
    for name in ("TinyUSB", "pico-sdk", "STMicroelectronics", "Noto Emoji", "Waveshare"):
        assert name in text, f"NOTICE does not mention {name}"

    for filename in (
        "Apache-2.0.txt",
        "BSD-3-Clause.txt",
        "BSD-3-Clause-STMicroelectronics.txt",
        "OFL-1.1.txt",
        "MIT-TinyUSB.txt",
    ):
        assert filename in text, f"NOTICE does not reference licenses/{filename}"


def test_apply_script_first_rsync_excludes_the_whole_boot_directory() -> None:
    # Excluding only boot/firmware still lets -a's local uid/gid/mode land
    # on the /boot directory entry itself (rsync applies -a's directory
    # metadata to every directory it descends into, excluded contents or
    # not). Excluding /boot outright keeps -a away from that entry too.
    text = _text(APPLY_SCRIPT)
    assert "--exclude /boot" in text
    assert "--exclude boot/firmware" not in text
    assert "--no-perms" in text


def test_apply_script_second_rsync_preserves_mtime_without_unix_metadata() -> None:
    # vfat has ~2s mtime granularity: -t (mtime preservation) plus
    # --modify-window=2 stops a re-run from treating every file as changed
    # and resending it. -a must not be used here (vfat can't hold Unix
    # ownership/mode bits), so this checks the exact invocation shape.
    text = _text(APPLY_SCRIPT)
    idx = text.index('"${FILES_SRC}boot/firmware/" "${PI_HOST}:/boot/firmware/"')
    start = text.rindex("rsync -", 0, idx)
    block = text[start:idx]
    assert block.startswith("rsync -rtz")
    assert "-az" not in block
    assert " -a " not in block
    assert "--modify-window=2" in block
    assert "--no-perms --no-owner --no-group" in block


def test_licenses_readme_states_the_gpl_bundling_option_correctly() -> None:
    # §3(a)/§6(a) is "source accompanies the binaries"; §3(b)/§6(b) is
    # "written offer, on request". The README previously had these
    # backwards.
    text = _text(BOOT_LICENSES_DIR / "README.txt")
    assert "GPLv2 §3(a) / GPLv3 §6(a)" in text
    assert "same-medium, on request only" not in text


def test_licenses_readme_does_not_assert_source_completeness_unconditionally() -> None:
    # The apt-package source collection happens at build time in a
    # follow-up PR; a purchaser-facing document should not assert it is
    # already complete, and must not mention pull requests at all.
    text = _text(BOOT_LICENSES_DIR / "README.txt")
    assert "pull request" not in text
    assert "MANIFEST.txt" in text
    assert "STATUS: OK" in text


def test_licenses_readme_uv_install_timing_is_build_time_not_first_boot() -> None:
    # uv is installed by the 03-portal pi-gen stage at image-build time,
    # not by anything that runs on first boot.
    text = _text(BOOT_LICENSES_DIR / "README.txt")
    assert "on first boot" not in text
    assert "installed into the image at build time" in text


def test_licenses_readme_points_at_the_source_tree_path() -> None:
    text = _text(BOOT_LICENSES_DIR / "README.txt")
    assert "/usr/share/palmimo/sources/" in text


def test_stmicro_license_file_is_verbatim_license_text_only() -> None:
    # The license file purchasers see must be the copyright line + license
    # body only -- no Jizai-authored provenance prose referencing paths
    # that are not on the SD card (vendor/Fonts/*.c lives in the private
    # firmware source tree, not this image). That prose belongs in NOTICE.
    text = _text(DISPLAY_FW_LICENSES_DIR / "licenses" / "BSD-3-Clause-STMicroelectronics.txt")
    assert text.startswith("Copyright (c) 2014 STMicroelectronics\n")
    assert "vendor/Fonts" not in text
    assert "NOTICE" not in text
    assert "Palmimo face display firmware" not in text


def test_notice_section_2_carries_the_stmicro_file_provenance() -> None:
    text = _text(DISPLAY_FW_NOTICE)
    section2 = text.split("2. STMicroelectronics", 1)[1].split("3. Raspberry Pi board header", 1)[0]
    assert "font8.c" in section2
    assert "font12CN.c" in section2
    assert "extracted" in section2
    assert "verbatim" in section2


def test_notice_does_not_point_at_the_private_repository_readme() -> None:
    # firmware/display/README.md lives in the private monorepo, not this
    # published image repository -- a purchaser reading NOTICE off the SD
    # card cannot follow that pointer.
    text = _text(DISPLAY_FW_NOTICE)
    assert "README.md build notes" not in text


def test_readme_corresponding_source_caveats_the_apt_only_scope() -> None:
    text = _text(ROOT_README)
    assert "tools/uv/" in text
    assert "`portal/`" in text
    assert "(L)GPL" in text
    assert "open question" in text


def test_readme_notes_uv_static_rust_crates_are_an_open_todo() -> None:
    text = _text(ROOT_README)
    assert "Rust crates" in text
    assert "pin the exact" in text


def test_readme_notes_gplv3_user_product_installation_information_not_required() -> None:
    text = _text(ROOT_README)
    assert "Installation Information" in text
    assert "User Product" in text
    assert "root account" in text


def test_readme_firmware_notice_symlink_described_as_future_not_current() -> None:
    text = _text(ROOT_README)
    assert "not published" in text
    assert "symlink" in text
    assert "diverge" in text


def test_readme_over_inclusion_claim_is_measured_not_asserted_harmless() -> None:
    text = _text(ROOT_README)
    assert "over-inclusion is harmless" not in text
    assert "measured" in text


def test_root_gitignore_does_not_also_ignore_workspace() -> None:
    # Pinning the anti-pattern: a top-level ".workspace" or
    # "pigen/.workspace" entry in the root .gitignore would stop
    # git from descending into the directory at all, which silently breaks
    # the inner "!.gitignore" negation above (git never even looks inside an
    # excluded directory). The self-ignoring pattern must stay the only
    # mechanism.
    text = _text(REPO_ROOT / ".gitignore")
    assert ".workspace" not in text
