"""Unit tests for lib/collect_oss_compliance.py -- the chroot-side script
that collects GPL/LGPL corresponding source and copies apt/Portal license
metadata onto the shipped image.

This module is designed to run inside a pi-gen chroot with stdlib only, so
every I/O boundary (subprocess execution, rootfs path, output path) is a
parameter -- these tests exercise the pure logic and the file-copying
helpers directly against tmp_path fixtures, without ever touching a real
dpkg database or calling apt-get.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "lib" / "collect_oss_compliance.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collect_oss_compliance", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coc = _load_module()


# ---------------------------------------------------------------------------
# list_source_packages: dpkg-query -W -f='${binary:Package}\t${source:Package}\t${source:Version}\n'
# ---------------------------------------------------------------------------


def test_list_source_packages_parses_basic_lines() -> None:
    output = "comitup\tcomitup\t1.43-1\navahi-daemon\tavahi\t0.8-10\n"
    assert coc.list_source_packages(output) == {("comitup", "1.43-1"), ("avahi", "0.8-10")}


def test_list_source_packages_dedupes_multiple_binaries_from_one_source() -> None:
    # avahi-daemon and avahi-utils are both built from the "avahi" source --
    # dpkg-query emits one line per binary package, but the (source, version)
    # pair must collapse to a single entry.
    output = "avahi-daemon\tavahi\t0.8-10\navahi-utils\tavahi\t0.8-10\navahi-dnsconfd\tavahi\t0.8-10\n"
    assert coc.list_source_packages(output) == {("avahi", "0.8-10")}


def test_list_source_packages_handles_epoch_versions() -> None:
    output = "raspi-firmware\traspi-firmware\t1:6.18.39-1+rpt1\n"
    assert coc.list_source_packages(output) == {("raspi-firmware", "1:6.18.39-1+rpt1")}


def test_list_source_packages_handles_source_version_diverging_from_binary_version() -> None:
    # dpkg-query emits the *source* version in ${source:Version}, which can
    # differ from ${Version} of the binary package it was built alongside.
    output = "libfoo2\tfoo\t2.1-3\n"
    assert coc.list_source_packages(output) == {("foo", "2.1-3")}


def test_list_source_packages_ignores_blank_lines() -> None:
    output = "comitup\tcomitup\t1.43-1\n\n\navahi-daemon\tavahi\t0.8-10\n"
    assert coc.list_source_packages(output) == {("comitup", "1.43-1"), ("avahi", "0.8-10")}


def test_list_source_packages_rejects_malformed_line() -> None:
    with pytest.raises(ValueError):
        coc.list_source_packages("comitup\tcomitup\n")


# ---------------------------------------------------------------------------
# parse_exclude_file
# ---------------------------------------------------------------------------


def test_parse_exclude_file_reads_name_and_inline_reason() -> None:
    text = (
        "# comment line, ignored\n"
        "firmware-nonfree  # non-free blob, no source obligation\n"
        "\n"
        "raspi-firmware # bootloader blobs\n"
        "bluez-firmware\n"
    )
    result = coc.parse_exclude_file(text)
    assert result["firmware-nonfree"] == "non-free blob, no source obligation"
    assert result["raspi-firmware"] == "bootloader blobs"
    assert result["bluez-firmware"] == ""


def test_parse_exclude_file_on_empty_text_is_empty_dict() -> None:
    assert coc.parse_exclude_file("") == {}
    assert coc.parse_exclude_file("# only comments\n\n") == {}


# ---------------------------------------------------------------------------
# fetch_sources: aggregates failures instead of stopping at the first one,
# and marks excluded pairs without invoking `run` at all.
# ---------------------------------------------------------------------------


class _FakeRun:
    """Records every invocation; returns canned results keyed by source name."""

    def __init__(self, outcomes: dict[str, int]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, cwd))
        source = cmd[-1].split("=")[0]
        returncode = self.outcomes.get(source, 0)
        # A successful apt-get source --download-only leaves a .dsc behind;
        # simulate that here since fetch_sources checks for its presence.
        if returncode == 0:
            (cwd / f"{source}_1.0.dsc").write_text("Format: 1.0\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom" if returncode else "")


def test_fetch_sources_marks_excluded_pairs_without_calling_run(tmp_path: Path) -> None:
    fake_run = _FakeRun({})
    results = coc.fetch_sources(
        {("firmware-nonfree", "1.0")},
        tmp_path,
        run=fake_run,
        exclude={"firmware-nonfree": "non-free blob"},
    )
    assert len(results) == 1
    assert results[0].status == "excluded"
    assert results[0].reason == "non-free blob"
    assert fake_run.calls == []


def test_fetch_sources_records_success_with_dsc_path(tmp_path: Path) -> None:
    fake_run = _FakeRun({})
    results = coc.fetch_sources({("comitup", "1.0")}, tmp_path, run=fake_run, exclude={})
    assert len(results) == 1
    assert results[0].status == "fetched"
    assert results[0].dsc_path is not None
    assert results[0].dsc_path.name == "comitup_1.0.dsc"


def test_fetch_sources_tries_every_pair_even_after_a_failure(tmp_path: Path) -> None:
    fake_run = _FakeRun({"bad-one": 1})
    pairs = {("bad-one", "1.0"), ("good-one", "1.0"), ("also-good", "1.0")}
    results = coc.fetch_sources(pairs, tmp_path, run=fake_run, exclude={})
    assert len(results) == 3
    statuses = {r.source: r.status for r in results}
    assert statuses["bad-one"] == "failed"
    assert statuses["good-one"] == "fetched"
    assert statuses["also-good"] == "fetched"
    # every pair was attempted -- not short-circuited at the first failure
    assert {c[0][-1].split("=")[0] for c in fake_run.calls} == {"bad-one", "good-one", "also-good"}


def test_fetch_sources_fails_when_dsc_is_missing_despite_zero_exit(tmp_path: Path) -> None:
    def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        # zero exit, but no .dsc written -- must still count as a failure.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    results = coc.fetch_sources({("sneaky", "1.0")}, tmp_path, run=run, exclude={})
    assert results[0].status == "failed"


def test_collect_failures_and_report_is_nonempty_iff_any_failed() -> None:
    ok = coc.FetchResult("good", "1.0", "fetched", Path("good_1.0.dsc"), None)
    bad = coc.FetchResult("bad", "1.0", "failed", None, "boom")
    excluded = coc.FetchResult("skip", "1.0", "excluded", None, "reason")

    assert coc.failed_results([ok, excluded]) == []
    failures = coc.failed_results([ok, bad, excluded])
    assert failures == [bad]

    report = coc.format_failure_report(failures)
    assert "bad" in report
    assert "boom" in report


# ---------------------------------------------------------------------------
# write_manifest: deterministic ordering, status header, exclusion records
# ---------------------------------------------------------------------------


def test_write_manifest_is_deterministic_and_sorted(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    dsc_z = tmp_path / "zeta_1.0.dsc"
    dsc_z.write_text("x", encoding="utf-8")
    dsc_a = tmp_path / "alpha_2.0.dsc"
    dsc_a.write_text("y", encoding="utf-8")
    results = [
        coc.FetchResult("zeta", "1.0", "fetched", dsc_z, None),
        coc.FetchResult("alpha", "2.0", "fetched", dsc_a, None),
        coc.FetchResult("excluded-pkg", "3.0", "excluded", None, "non-free blob"),
    ]

    coc.write_manifest(
        out,
        img_date="2026-09-01",
        fetch_results=results,
        skip_corresponding_source=False,
        pi_missing_copyright=[],
        portal_third_party_licenses_present=True,
    )
    text1 = out.read_text(encoding="utf-8")

    coc.write_manifest(
        out,
        img_date="2026-09-01",
        fetch_results=list(reversed(results)),
        skip_corresponding_source=False,
        pi_missing_copyright=[],
        portal_third_party_licenses_present=True,
    )
    text2 = out.read_text(encoding="utf-8")

    assert text1 == text2
    # alpha sorts before excluded-pkg sorts before zeta
    assert text1.index("alpha") < text1.index("excluded-pkg") < text1.index("zeta")
    assert "STATUS: OK" in text1
    assert "GPLv2" in text1 and "GPLv3" in text1


def test_write_manifest_records_skip_status_first_line(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    coc.write_manifest(
        out,
        img_date="2026-09-01",
        fetch_results=[],
        skip_corresponding_source=True,
        pi_missing_copyright=[],
        portal_third_party_licenses_present=False,
    )
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == (
        "STATUS: INCOMPLETE -- corresponding source was skipped (development build, NOT shippable)"
    )


def test_write_manifest_records_missing_copyright_and_absent_portal_licenses(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    coc.write_manifest(
        out,
        img_date="2026-09-01",
        fetch_results=[],
        skip_corresponding_source=False,
        pi_missing_copyright=["some-pkg", "other-pkg"],
        portal_third_party_licenses_present=False,
    )
    text = out.read_text(encoding="utf-8")
    assert "some-pkg" in text
    assert "other-pkg" in text
    assert "THIRD_PARTY_LICENSES.txt" in text
    assert "absent" in text


def test_write_manifest_includes_sha256_of_each_fetched_dsc(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    dsc = tmp_path / "comitup_1.0.dsc"
    dsc.write_bytes(b"Format: 1.0\n")
    import hashlib

    expected = hashlib.sha256(b"Format: 1.0\n").hexdigest()

    coc.write_manifest(
        out,
        img_date="2026-09-01",
        fetch_results=[coc.FetchResult("comitup", "1.0", "fetched", dsc, None)],
        skip_corresponding_source=False,
        pi_missing_copyright=[],
        portal_third_party_licenses_present=True,
    )
    assert expected in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# copy_pi_licenses: common-licenses + per-package copyright, symlinks
# followed, missing copyright recorded
# ---------------------------------------------------------------------------


def _make_fake_rootfs(tmp_path: Path) -> Path:
    rootfs = tmp_path / "rootfs"
    common = rootfs / "usr" / "share" / "common-licenses"
    common.mkdir(parents=True)
    (common / "GPL-2").write_text("GPLv2 text\n", encoding="utf-8")
    (common / "GPL-3").write_text("GPLv3 text\n", encoding="utf-8")

    doc = rootfs / "usr" / "share" / "doc"
    doc.mkdir(parents=True)

    (doc / "comitup").mkdir()
    (doc / "comitup" / "copyright").write_text("Comitup copyright\n", encoding="utf-8")

    # avahi-utils symlinks its copyright file to avahi-daemon's -- the real
    # content must be followed, not a dangling/empty copy.
    (doc / "avahi-daemon").mkdir()
    (doc / "avahi-daemon" / "copyright").write_text("Avahi copyright\n", encoding="utf-8")
    (doc / "avahi-utils").mkdir()
    (doc / "avahi-utils" / "copyright").symlink_to("../avahi-daemon/copyright")

    # git has a doc dir but no copyright file at all.
    (doc / "git").mkdir()

    return rootfs


def test_copy_pi_licenses_copies_common_licenses(tmp_path: Path) -> None:
    rootfs = _make_fake_rootfs(tmp_path)
    out = tmp_path / "out"
    coc.copy_pi_licenses(rootfs, out, packages=["comitup"])
    assert (out / "common-licenses" / "GPL-2").read_text(encoding="utf-8") == "GPLv2 text\n"
    assert (out / "common-licenses" / "GPL-3").read_text(encoding="utf-8") == "GPLv3 text\n"


def test_copy_pi_licenses_copies_each_package_copyright(tmp_path: Path) -> None:
    rootfs = _make_fake_rootfs(tmp_path)
    out = tmp_path / "out"
    report = coc.copy_pi_licenses(rootfs, out, packages=["comitup", "avahi-utils"])
    assert (out / "comitup" / "copyright").read_text(encoding="utf-8") == "Comitup copyright\n"
    assert report.copied == ["avahi-utils", "comitup"]


def test_copy_pi_licenses_follows_symlinked_copyright(tmp_path: Path) -> None:
    rootfs = _make_fake_rootfs(tmp_path)
    out = tmp_path / "out"
    coc.copy_pi_licenses(rootfs, out, packages=["avahi-utils"])
    copied = out / "avahi-utils" / "copyright"
    assert not copied.is_symlink()
    assert copied.read_text(encoding="utf-8") == "Avahi copyright\n"


def test_copy_pi_licenses_records_missing_copyright(tmp_path: Path) -> None:
    rootfs = _make_fake_rootfs(tmp_path)
    out = tmp_path / "out"
    report = coc.copy_pi_licenses(rootfs, out, packages=["comitup", "git"])
    assert report.missing == ["git"]
    assert not (out / "git").exists()


# ---------------------------------------------------------------------------
# copy_portal_licenses: dist-info METADATA parsing + License-File collection
# ---------------------------------------------------------------------------


def _make_fake_venv(tmp_path: Path) -> Path:
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    dist_info = site_packages / "fastapi-0.110.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\n"
        "Name: fastapi\n"
        "Version: 0.110.0\n"
        "License-Expression: MIT\n"
        "Classifier: License :: OSI Approved :: MIT License\n"
        "License-File: LICENSE\n"
        "License-File: licenses/AUTHORS.md\n"
        "\n"
        "Long description body, not a header.\n"
        "License: this line is body text, not a header, must not leak in\n",
        encoding="utf-8",
    )
    (dist_info / "LICENSE").write_text("MIT license text\n", encoding="utf-8")
    licenses_dir = dist_info / "licenses"
    licenses_dir.mkdir()
    (licenses_dir / "AUTHORS.md").write_text("Author list\n", encoding="utf-8")
    return site_packages


def test_copy_portal_licenses_writes_license_metadata_txt(tmp_path: Path) -> None:
    site_packages = _make_fake_venv(tmp_path)
    out = tmp_path / "boot-out"
    coc.copy_portal_licenses(site_packages, tmp_path / "portal-repo", out)

    meta = (out / "python" / "fastapi-0.110.0" / "LICENSE-METADATA.txt").read_text(encoding="utf-8")
    assert "License-Expression: MIT" in meta
    assert "Classifier: License :: OSI Approved :: MIT License" in meta
    assert "this line is body text" not in meta


def test_copy_portal_licenses_copies_license_files(tmp_path: Path) -> None:
    site_packages = _make_fake_venv(tmp_path)
    out = tmp_path / "boot-out"
    coc.copy_portal_licenses(site_packages, tmp_path / "portal-repo", out)

    dist_out = out / "python" / "fastapi-0.110.0"
    assert (dist_out / "LICENSE").read_text(encoding="utf-8") == "MIT license text\n"
    assert (dist_out / "licenses" / "AUTHORS.md").read_text(encoding="utf-8") == "Author list\n"


def test_copy_portal_licenses_copies_third_party_notices(tmp_path: Path) -> None:
    site_packages = _make_fake_venv(tmp_path)
    portal_root = tmp_path / "portal-repo"
    portal_root.mkdir()
    (portal_root / "THIRD_PARTY_NOTICES.md").write_text("notices\n", encoding="utf-8")
    out = tmp_path / "boot-out"

    report = coc.copy_portal_licenses(site_packages, portal_root, out)

    assert (out / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8") == "notices\n"
    assert report.third_party_licenses_present is False


def test_copy_portal_licenses_copies_third_party_licenses_txt_when_present(tmp_path: Path) -> None:
    site_packages = _make_fake_venv(tmp_path)
    portal_root = tmp_path / "portal-repo"
    static_dir = portal_root / "palmimo_portal" / "static"
    static_dir.mkdir(parents=True)
    (portal_root / "THIRD_PARTY_NOTICES.md").write_text("notices\n", encoding="utf-8")
    (static_dir / "THIRD_PARTY_LICENSES.txt").write_text("npm licenses\n", encoding="utf-8")
    out = tmp_path / "boot-out"

    report = coc.copy_portal_licenses(site_packages, portal_root, out)

    assert (out / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8") == "npm licenses\n"
    assert report.third_party_licenses_present is True


def test_copy_portal_licenses_warns_and_continues_when_third_party_licenses_txt_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site_packages = _make_fake_venv(tmp_path)
    portal_root = tmp_path / "portal-repo"
    portal_root.mkdir()
    (portal_root / "THIRD_PARTY_NOTICES.md").write_text("notices\n", encoding="utf-8")
    out = tmp_path / "boot-out"

    report = coc.copy_portal_licenses(site_packages, portal_root, out)

    assert report.third_party_licenses_present is False
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "THIRD_PARTY_LICENSES.txt" in captured.err


# ---------------------------------------------------------------------------
# Sanity: the script parses as valid Python 3 (chroot runs it with plain
# python3, not through pytest's import machinery).
# ---------------------------------------------------------------------------


def test_script_compiles_standalone() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
