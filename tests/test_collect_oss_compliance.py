"""Unit tests for lib/collect_oss_compliance.py, exercised via tmp_path
fixtures against the pure logic and file-copying helpers -- never a real
dpkg database or apt-get."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from collections.abc import Callable
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
# list_source_packages: dpkg-query -W
#   -f='${binary:Package}\t${source:Package}\t${source:Version}\t${db:Status-Status}\n'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param(
            "comitup\tcomitup\t1.43-1\tinstalled\navahi-daemon\tavahi\t0.8-10\tinstalled\n",
            {("comitup", "1.43-1"), ("avahi", "0.8-10")},
            id="basic_lines",
        ),
        pytest.param(
            # avahi-daemon/avahi-utils/avahi-dnsconfd all built from "avahi" --
            # one line per binary package, but (source, version) must dedupe.
            "avahi-daemon\tavahi\t0.8-10\tinstalled\n"
            "avahi-utils\tavahi\t0.8-10\tinstalled\n"
            "avahi-dnsconfd\tavahi\t0.8-10\tinstalled\n",
            {("avahi", "0.8-10")},
            id="dedupes_multiple_binaries_from_one_source",
        ),
        pytest.param(
            "raspi-firmware\traspi-firmware\t1:6.18.39-1+rpt1\tinstalled\n",
            {("raspi-firmware", "1:6.18.39-1+rpt1")},
            id="handles_epoch_versions",
        ),
        pytest.param(
            # ${source:Version} can diverge from the binary package's ${Version}.
            "libfoo2\tfoo\t2.1-3\tinstalled\n",
            {("foo", "2.1-3")},
            id="handles_source_version_diverging_from_binary_version",
        ),
        pytest.param(
            "comitup\tcomitup\t1.43-1\tinstalled\n\n\navahi-daemon\tavahi\t0.8-10\tinstalled\n",
            {("comitup", "1.43-1"), ("avahi", "0.8-10")},
            id="ignores_blank_lines",
        ),
        pytest.param(
            # a purged-but-config-remaining package must never trigger a fetch.
            "comitup\tcomitup\t1.43-1\tinstalled\n"
            "old-pkg\told-pkg\t0.1-1\tconfig-files\n"
            "half-installed\thalf-installed\t0.2-1\thalf-installed\n",
            {("comitup", "1.43-1")},
            id="excludes_non_installed_status",
        ),
    ],
)
def test_list_source_packages_parses(output: str, expected: set[tuple[str, str]]) -> None:
    assert coc.list_source_packages(output) == expected


def test_list_source_packages_rejects_malformed_line() -> None:
    with pytest.raises(ValueError):
        coc.list_source_packages("comitup\tcomitup\tinstalled\n")


# parse_exclude_file (shared by oss-source-exclude.txt / oss-copyright-missing-allow.txt)
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


def _dsc_text(files: list[tuple[str, str, int]]) -> str:
    lines = ["Format: 1.0", "Source: pkg", "Checksums-Sha256:"]
    for fname, sha, size in files:
        lines.append(f" {sha} {size} {fname}")
    return "\n".join(lines) + "\n"


class _FakeRun:
    """Records every invocation; returns canned results keyed by source name.

    A successful run writes a .dsc with a Checksums-Sha256 field pointing at
    one companion tarball, and writes that tarball too, unless
    `corrupt_tarball` or `omit_tarball` says otherwise -- fetch_sources must
    verify these checksums, not just trust a zero exit code and a present
    .dsc.
    """

    def __init__(
        self,
        outcomes: dict[str, int],
        corrupt_tarball: frozenset[str] = frozenset(),
        omit_tarball: frozenset[str] = frozenset(),
        omit_checksums_field: frozenset[str] = frozenset(),
    ) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[list[str], Path]] = []
        self.corrupt_tarball = corrupt_tarball
        self.omit_tarball = omit_tarball
        self.omit_checksums_field = omit_checksums_field

    def __call__(self, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, cwd))
        source = cmd[-1].split("=")[0]
        returncode = self.outcomes.get(source, 0)
        if returncode == 0:
            tar_name = f"{source}_1.0.tar.xz"
            tar_content = b"tarball content for " + source.encode()
            if source not in self.omit_tarball:
                data = b"corrupted!" if source in self.corrupt_tarball else tar_content
                (cwd / tar_name).write_bytes(data)
            sha = hashlib.sha256(tar_content).hexdigest()
            if source in self.omit_checksums_field:
                dsc_text = "Format: 1.0\nSource: pkg\n"
            else:
                dsc_text = _dsc_text([(tar_name, sha, len(tar_content))])
            (cwd / f"{source}_1.0.dsc").write_text(dsc_text, encoding="utf-8")
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


def test_fetch_sources_records_dsc_and_companion_file_with_sha256_and_size(tmp_path: Path) -> None:
    fake_run = _FakeRun({})
    results = coc.fetch_sources({("comitup", "1.0")}, tmp_path, run=fake_run, exclude={})
    files_by_name = {name: (sha, size) for name, sha, size in results[0].files}
    assert "comitup_1.0.dsc" in files_by_name
    assert "comitup_1.0.tar.xz" in files_by_name
    expected_sha = hashlib.sha256(b"tarball content for comitup").hexdigest()
    assert files_by_name["comitup_1.0.tar.xz"] == (expected_sha, len(b"tarball content for comitup"))


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


@pytest.mark.parametrize(
    ("source", "fake_run_kwarg", "expected_substring"),
    [
        ("nofield", "omit_checksums_field", "Checksums-Sha256"),
        ("missingtar", "omit_tarball", "missing"),
        ("corrupt", "corrupt_tarball", "sha256"),
    ],
)
def test_fetch_sources_fails_dsc_verification(
    tmp_path: Path, source: str, fake_run_kwarg: str, expected_substring: str
) -> None:
    fake_run = _FakeRun({}, **{fake_run_kwarg: frozenset({source})})
    results = coc.fetch_sources({(source, "1.0")}, tmp_path, run=fake_run, exclude={})
    assert results[0].status == "failed"
    assert expected_substring.lower() in results[0].reason.lower()


# parse_dsc_checksums_sha256
def test_parse_dsc_checksums_sha256_extracts_entries() -> None:
    text = _dsc_text([("a_1.0.tar.xz", "abc123", 42), ("a_1.0.debian.tar.xz", "def456", 7)])
    entries = coc.parse_dsc_checksums_sha256(text)
    assert entries == [("a_1.0.tar.xz", "abc123", 42), ("a_1.0.debian.tar.xz", "def456", 7)]


# write_manifest: deterministic ordering, status header, exclusion records
def _write_manifest_defaults(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "img_date": "2026-09-01",
        "fetch_results": [],
        "skip_corresponding_source": False,
        "pi_missing_copyright": [],
        "portal_dists_count": 0,
        "portal_dists_without_license_text": [],
        "portal_third_party_licenses_present": True,
        "portal_python_runtime_dirs": [],
    }
    defaults.update(overrides)
    return defaults


def test_write_manifest_is_deterministic_and_sorted(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    dsc_z = tmp_path / "zeta_1.0.dsc"
    dsc_z.write_text("x", encoding="utf-8")
    dsc_a = tmp_path / "alpha_2.0.dsc"
    dsc_a.write_text("y", encoding="utf-8")
    results = [
        coc.FetchResult("zeta", "1.0", "fetched", dsc_z, None, files=(("zeta_1.0.dsc", "sha-z", 1),)),
        coc.FetchResult("alpha", "2.0", "fetched", dsc_a, None, files=(("alpha_2.0.dsc", "sha-a", 1),)),
        coc.FetchResult("excluded-pkg", "3.0", "excluded", None, "non-free blob"),
    ]

    coc.write_manifest(out, **_write_manifest_defaults(fetch_results=results))
    text1 = out.read_text(encoding="utf-8")

    coc.write_manifest(out, **_write_manifest_defaults(fetch_results=list(reversed(results))))
    text2 = out.read_text(encoding="utf-8")

    assert text1 == text2
    # alpha sorts before excluded-pkg sorts before zeta
    assert text1.index("alpha") < text1.index("excluded-pkg") < text1.index("zeta")
    assert "STATUS: OK" in text1
    assert "GPLv2" in text1 and "GPLv3" in text1


def test_write_manifest_sorts_same_source_name_by_version_then_status_then_filename(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    results = [
        coc.FetchResult("dup", "2.0", "fetched", None, None, files=(("dup_2.0.dsc", "sha2", 1),)),
        coc.FetchResult("dup", "1.0", "fetched", None, None, files=(("dup_1.0.dsc", "sha1", 1),)),
    ]
    coc.write_manifest(out, **_write_manifest_defaults(fetch_results=results))
    text = out.read_text(encoding="utf-8")
    assert text.index("dup_1.0.dsc") < text.index("dup_2.0.dsc")


def test_write_manifest_records_skip_status_first_line(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    coc.write_manifest(
        out,
        **_write_manifest_defaults(
            skip_corresponding_source=True,
            portal_third_party_licenses_present=False,
        ),
    )
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == (
        "STATUS: INCOMPLETE -- corresponding source was skipped (development build, NOT shippable)"
    )


def test_write_manifest_records_missing_copyright_and_absent_portal_licenses(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    coc.write_manifest(
        out,
        **_write_manifest_defaults(
            pi_missing_copyright=["some-pkg", "other-pkg"],
            portal_third_party_licenses_present=False,
        ),
    )
    text = out.read_text(encoding="utf-8")
    assert "some-pkg" in text
    assert "other-pkg" in text
    assert "THIRD_PARTY_LICENSES.txt" in text
    assert "absent" in text


def test_write_manifest_records_portal_dists_count(tmp_path: Path) -> None:
    out = tmp_path / "MANIFEST.txt"
    coc.write_manifest(out, **_write_manifest_defaults(portal_dists_count=42))
    text = out.read_text(encoding="utf-8")
    assert "portal python dists: 42" in text


@pytest.mark.parametrize(
    ("overrides", "expected_substrings"),
    [
        pytest.param(
            {"portal_third_party_licenses_present": False},
            ["STATUS: INCOMPLETE", "frontend THIRD_PARTY_LICENSES.txt absent (portal tag predates it)"],
            id="npm_licenses_absent",
        ),
        pytest.param(
            {"portal_dists_without_license_text": ["some-internal-pkg"]},
            ["STATUS: INCOMPLETE", "some-internal-pkg", "needs manual review"],
            id="dists_without_license_text",
        ),
        pytest.param(
            {"portal_python_runtime_dirs": ["cpython-3.13.0-linux-aarch64-gnu"]},
            ["STATUS: INCOMPLETE", "cpython-3.13.0-linux-aarch64-gnu", "Uncovered binaries"],
            id="uncovered_uv_python_runtimes",
        ),
    ],
)
def test_write_manifest_marks_incomplete(
    tmp_path: Path, overrides: dict[str, object], expected_substrings: list[str]
) -> None:
    out = tmp_path / "MANIFEST.txt"
    coc.write_manifest(out, **_write_manifest_defaults(**overrides))
    text = out.read_text(encoding="utf-8")
    for substring in expected_substrings:
        assert substring in text


# copy_pi_licenses: common-licenses + per-package copyright, symlinks followed
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


def test_copy_pi_licenses_copies_common_and_package_copyright_following_symlinks(tmp_path: Path) -> None:
    rootfs = _make_fake_rootfs(tmp_path)
    out = tmp_path / "out"
    report = coc.copy_pi_licenses(rootfs, out, packages=["comitup", "avahi-utils"])

    assert (out / "common-licenses" / "GPL-2").read_text(encoding="utf-8") == "GPLv2 text\n"
    assert (out / "common-licenses" / "GPL-3").read_text(encoding="utf-8") == "GPLv3 text\n"
    assert (out / "comitup" / "copyright").read_text(encoding="utf-8") == "Comitup copyright\n"
    assert report.copied == ["avahi-utils", "comitup"]

    # avahi-utils symlinks its copyright to avahi-daemon's -- content must be
    # copied by resolved value, not as a dangling/relative symlink.
    symlinked = out / "avahi-utils" / "copyright"
    assert not symlinked.is_symlink()
    assert symlinked.read_text(encoding="utf-8") == "Avahi copyright\n"


def test_copy_pi_licenses_records_missing_copyright(tmp_path: Path) -> None:
    rootfs = _make_fake_rootfs(tmp_path)
    out = tmp_path / "out"
    report = coc.copy_pi_licenses(rootfs, out, packages=["comitup", "git"])
    assert report.missing == ["git"]
    assert not (out / "git").exists()


# copy_portal_licenses: dist-info METADATA parsing + License-File collection
def _make_fake_venv(tmp_path: Path, python_minor: str = "python3.12") -> Path:
    site_packages = tmp_path / "venv" / "lib" / python_minor / "site-packages"
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


def test_copy_portal_licenses_writes_license_metadata_txt_and_copies_license_files(tmp_path: Path) -> None:
    site_packages = _make_fake_venv(tmp_path)
    out = tmp_path / "boot-out"
    coc.copy_portal_licenses(site_packages, tmp_path / "portal-repo", out)

    dist_out = out / "python" / "fastapi-0.110.0"
    meta = (dist_out / "LICENSE-METADATA.txt").read_text(encoding="utf-8")
    assert "License-Expression: MIT" in meta
    assert "Classifier: License :: OSI Approved :: MIT License" in meta
    assert "this line is body text" not in meta
    assert (dist_out / "LICENSE").read_text(encoding="utf-8") == "MIT license text\n"
    assert (dist_out / "licenses" / "AUTHORS.md").read_text(encoding="utf-8") == "Author list\n"


def test_copy_portal_licenses_fails_when_license_file_declared_but_missing(tmp_path: Path) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    dist_info = site_packages / "brokenpkg-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: brokenpkg\nLicense: MIT\nLicense-File: LICENSE\n\nbody\n",
        encoding="utf-8",
    )
    out = tmp_path / "boot-out"
    report = coc.copy_portal_licenses(site_packages, tmp_path / "portal-repo", out)
    assert report.missing_license_files == [("brokenpkg-1.0", "LICENSE")]


def test_copy_portal_licenses_records_dist_with_no_license_info_without_writing_empty_file(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    dist_info = site_packages / "nolicense-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: nolicense\nVersion: 1.0\n\nbody\n",
        encoding="utf-8",
    )
    out = tmp_path / "boot-out"
    report = coc.copy_portal_licenses(site_packages, tmp_path / "portal-repo", out)
    assert report.dists_without_license_text == ["nolicense-1.0"]
    assert not (out / "python" / "nolicense-1.0" / "LICENSE-METADATA.txt").exists()


def test_copy_portal_licenses_copies_third_party_notices_and_warns_when_licenses_txt_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site_packages = _make_fake_venv(tmp_path)
    portal_root = tmp_path / "portal-repo"
    portal_root.mkdir()
    (portal_root / "THIRD_PARTY_NOTICES.md").write_text("notices\n", encoding="utf-8")
    out = tmp_path / "boot-out"

    report = coc.copy_portal_licenses(site_packages, portal_root, out)

    assert (out / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8") == "notices\n"
    assert report.third_party_licenses_present is False
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "THIRD_PARTY_LICENSES.txt" in captured.err


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


# find_portal_venv_site_packages: glob instead of a hardcoded python3.12
def test_find_portal_venv_site_packages_globs_any_python_minor(tmp_path: Path) -> None:
    portal_root = tmp_path / "portal-repo"
    _make_fake_venv_at(portal_root, "python3.13")
    found = coc.find_portal_venv_site_packages(portal_root)
    assert found == portal_root / ".venv" / "lib" / "python3.13" / "site-packages"


def _make_fake_venv_at(portal_root: Path, python_minor: str) -> Path:
    site_packages = portal_root / ".venv" / "lib" / python_minor / "site-packages"
    dist_info = site_packages / "somepkg-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text("Name: somepkg\nLicense: MIT\n\nbody\n", encoding="utf-8")
    return site_packages


def test_find_portal_venv_site_packages_raises_when_venv_directory_absent(tmp_path: Path) -> None:
    portal_root = tmp_path / "portal-repo"
    portal_root.mkdir()
    with pytest.raises(RuntimeError) as excinfo:
        coc.find_portal_venv_site_packages(portal_root)
    assert str(portal_root) in str(excinfo.value)


def test_find_portal_venv_site_packages_raises_when_zero_dist_info(tmp_path: Path) -> None:
    portal_root = tmp_path / "portal-repo"
    empty_site_packages = portal_root / ".venv" / "lib" / "python3.12" / "site-packages"
    empty_site_packages.mkdir(parents=True)
    with pytest.raises(RuntimeError) as excinfo:
        coc.find_portal_venv_site_packages(portal_root)
    assert str(empty_site_packages) in str(excinfo.value)


# find_uv_managed_pythons / copy_uv_python_runtime_licenses
def test_find_uv_managed_pythons_lists_subdirectories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    uv_python = home / ".local" / "share" / "uv" / "python"
    (uv_python / "cpython-3.13.0-linux-aarch64-gnu").mkdir(parents=True)
    (uv_python / "cpython-3.12.0-linux-aarch64-gnu").mkdir(parents=True)
    found = coc.find_uv_managed_pythons(home)
    assert [p.name for p in found] == ["cpython-3.12.0-linux-aarch64-gnu", "cpython-3.13.0-linux-aarch64-gnu"]


def test_copy_uv_python_runtime_licenses_copies_licenses_subdir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    py_dir = home / ".local" / "share" / "uv" / "python" / "cpython-3.13.0-linux-aarch64-gnu"
    licenses_dir = py_dir / "licenses"
    licenses_dir.mkdir(parents=True)
    (licenses_dir / "LICENSE").write_text("PSF license\n", encoding="utf-8")

    out = tmp_path / "boot-out" / "python-runtime"
    copied = coc.copy_uv_python_runtime_licenses([py_dir], out)

    assert copied == ["cpython-3.13.0-linux-aarch64-gnu"]
    dest = out / "cpython-3.13.0-linux-aarch64-gnu" / "licenses" / "LICENSE"
    assert dest.read_text(encoding="utf-8") == "PSF license\n"


# main(): full integration, subprocess.run monkeypatched, tmp rootfs.
def _make_integration_rootfs(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "usr" / "share" / "common-licenses").mkdir(parents=True)
    doc = root / "usr" / "share" / "doc"
    doc.mkdir(parents=True)
    (doc / "comitup").mkdir()
    (doc / "comitup" / "copyright").write_text("Comitup copyright\n", encoding="utf-8")
    return root


def _make_exclude_and_allow_files(tmp_path: Path) -> tuple[Path, Path]:
    exclude_file = tmp_path / "oss-source-exclude.txt"
    exclude_file.write_text("# exclude list\n", encoding="utf-8")
    allow_file = tmp_path / "oss-copyright-missing-allow.txt"
    allow_file.write_text("# allow list\n", encoding="utf-8")
    return exclude_file, allow_file


def _patch_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[..., subprocess.CompletedProcess[str]]
) -> None:
    monkeypatch.setattr(coc.subprocess, "run", handler)


def _make_dpkg_fake_run(
    binary_stdout: str,
    installed_stdout: str,
    apt_result: Callable[[list[str], Path], subprocess.CompletedProcess[str]]
    | subprocess.CompletedProcess[str]
    | None = None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a subprocess.run stand-in answering the two dpkg-query calls
    main() makes and, if given, apt-get source (a callable) or a canned
    apt-get failure (a CompletedProcess)."""

    def fake_run(cmd: list[str], cwd: object = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "dpkg-query":
            if "${binary:Package}" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 0, stdout=binary_stdout, stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=installed_stdout, stderr="")
        if cmd[:2] == ["apt-get", "source"]:
            if callable(apt_result):
                return apt_result(cmd, Path(str(cwd)))
            if apt_result is not None:
                return apt_result
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


def _run_main(root: Path, exclude_file: Path, allow_file: Path, portal_root: Path, img_date: str = "2026-09-01") -> int:
    return coc.main(
        [
            "--root",
            str(root),
            "--exclude-file",
            str(exclude_file),
            "--copyright-allow-file",
            str(allow_file),
            "--portal-root",
            str(portal_root),
            "--img-date",
            img_date,
        ]
    )


def test_main_fails_and_writes_no_manifest_when_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_integration_rootfs(tmp_path)
    exclude_file, allow_file = _make_exclude_and_allow_files(tmp_path)
    portal_root = tmp_path / "portal"
    _make_fake_venv_at(portal_root, "python3.12")

    apt_failure = subprocess.CompletedProcess(["apt-get"], 1, stdout="", stderr="404 not found")
    _patch_subprocess_run(
        monkeypatch,
        _make_dpkg_fake_run("comitup\tcomitup\t1.0\tinstalled\n", "comitup\tinstalled\n", apt_failure),
    )

    rc = _run_main(root, exclude_file, allow_file, portal_root)
    assert rc != 0
    assert not (root / "usr" / "share" / "palmimo" / "sources" / "MANIFEST.txt").exists()
    assert "comitup" in capsys.readouterr().err


def test_main_fails_when_venv_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_integration_rootfs(tmp_path)
    exclude_file, allow_file = _make_exclude_and_allow_files(tmp_path)
    portal_root = tmp_path / "portal"
    portal_root.mkdir()

    _patch_subprocess_run(
        monkeypatch, _make_dpkg_fake_run("comitup\tcomitup\t1.0\tinstalled\n", "comitup\tinstalled\n")
    )

    monkeypatch.setenv("PALMIMO_SKIP_CORRESPONDING_SOURCE", "1")
    rc = _run_main(root, exclude_file, allow_file, portal_root)
    assert rc != 0
    assert "venv" in capsys.readouterr().err.lower()


def test_main_fails_on_unallowed_missing_copyright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    (root / "usr" / "share" / "common-licenses").mkdir(parents=True)
    (root / "usr" / "share" / "doc").mkdir(parents=True)  # no copyright for git
    exclude_file, allow_file = _make_exclude_and_allow_files(tmp_path)
    portal_root = tmp_path / "portal"
    _make_fake_venv_at(portal_root, "python3.12")

    _patch_subprocess_run(monkeypatch, _make_dpkg_fake_run("git\tgit\t1.0\tinstalled\n", "git\tinstalled\n"))

    monkeypatch.setenv("PALMIMO_SKIP_CORRESPONDING_SOURCE", "1")
    rc = _run_main(root, exclude_file, allow_file, portal_root)
    assert rc != 0
    assert "git" in capsys.readouterr().err


def test_main_warns_on_unused_exclusion_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _make_integration_rootfs(tmp_path)
    exclude_file = tmp_path / "oss-source-exclude.txt"
    exclude_file.write_text("not-installed-pkg\n", encoding="utf-8")
    allow_file = tmp_path / "oss-copyright-missing-allow.txt"
    allow_file.write_text("# allow list\n", encoding="utf-8")
    portal_root = tmp_path / "portal"
    _make_fake_venv_at(portal_root, "python3.12")

    _patch_subprocess_run(
        monkeypatch,
        _make_dpkg_fake_run("comitup\tcomitup\t1.0\tinstalled\n", "comitup\tinstalled\n", _FakeRun({})),
    )

    rc = _run_main(root, exclude_file, allow_file, portal_root)
    assert rc == 0
    assert "not-installed-pkg" in capsys.readouterr().err


def test_main_skip_flag_marks_manifest_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_integration_rootfs(tmp_path)
    exclude_file, allow_file = _make_exclude_and_allow_files(tmp_path)
    portal_root = tmp_path / "portal"
    _make_fake_venv_at(portal_root, "python3.12")

    _patch_subprocess_run(
        monkeypatch, _make_dpkg_fake_run("comitup\tcomitup\t1.0\tinstalled\n", "comitup\tinstalled\n")
    )

    monkeypatch.setenv("PALMIMO_SKIP_CORRESPONDING_SOURCE", "1")
    rc = _run_main(root, exclude_file, allow_file, portal_root)
    assert rc == 0
    manifest = (root / "usr" / "share" / "palmimo" / "sources" / "MANIFEST.txt").read_text(encoding="utf-8")
    assert manifest.splitlines()[0].startswith("STATUS: INCOMPLETE")


def test_main_happy_path_writes_ok_manifest_and_boot_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_integration_rootfs(tmp_path)
    exclude_file, allow_file = _make_exclude_and_allow_files(tmp_path)
    portal_root = tmp_path / "portal"
    _make_fake_venv_at(portal_root, "python3.12")
    (portal_root / "THIRD_PARTY_NOTICES.md").write_text("notices\n", encoding="utf-8")
    static_dir = portal_root / "palmimo_portal" / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "THIRD_PARTY_LICENSES.txt").write_text("npm licenses\n", encoding="utf-8")

    _patch_subprocess_run(
        monkeypatch,
        _make_dpkg_fake_run("comitup\tcomitup\t1.0\tinstalled\n", "comitup\tinstalled\n", _FakeRun({})),
    )

    rc = _run_main(root, exclude_file, allow_file, portal_root)
    assert rc == 0
    sources_manifest = root / "usr" / "share" / "palmimo" / "sources" / "MANIFEST.txt"
    boot_manifest = root / "boot" / "firmware" / "licenses" / "MANIFEST.txt"
    assert sources_manifest.is_file()
    assert boot_manifest.is_file()
    assert sources_manifest.read_text(encoding="utf-8") == boot_manifest.read_text(encoding="utf-8")
    assert sources_manifest.read_text(encoding="utf-8").splitlines()[0] == "STATUS: OK"


def test_main_cleans_previous_build_output_directories_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_integration_rootfs(tmp_path)
    exclude_file, allow_file = _make_exclude_and_allow_files(tmp_path)
    portal_root = tmp_path / "portal"
    _make_fake_venv_at(portal_root, "python3.12")

    stale = root / "usr" / "share" / "palmimo" / "sources" / "debian" / "stale-pkg_0.1"
    stale.mkdir(parents=True)
    (stale / "stale-pkg_0.1.dsc").write_text("stale\n", encoding="utf-8")
    stale_pi = root / "boot" / "firmware" / "licenses" / "pi" / "some-old-pkg"
    stale_pi.mkdir(parents=True)

    _patch_subprocess_run(
        monkeypatch, _make_dpkg_fake_run("comitup\tcomitup\t1.0\tinstalled\n", "comitup\tinstalled\n")
    )

    monkeypatch.setenv("PALMIMO_SKIP_CORRESPONDING_SOURCE", "1")
    rc = _run_main(root, exclude_file, allow_file, portal_root)
    assert rc == 0
    assert not stale.exists()
    assert not stale_pi.exists()


def test_main_requires_exclude_file_to_exist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_integration_rootfs(tmp_path)
    allow_file = tmp_path / "oss-copyright-missing-allow.txt"
    allow_file.write_text("# allow list\n", encoding="utf-8")

    rc = _run_main(root, tmp_path / "does-not-exist.txt", allow_file, tmp_path / "portal")
    assert rc != 0
    assert "does-not-exist.txt" in capsys.readouterr().err


def test_main_requires_copyright_allow_file_to_exist(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _make_integration_rootfs(tmp_path)
    exclude_file = tmp_path / "oss-source-exclude.txt"
    exclude_file.write_text("# exclude list\n", encoding="utf-8")

    rc = _run_main(root, exclude_file, tmp_path / "does-not-exist.txt", tmp_path / "portal")
    assert rc != 0
    assert "does-not-exist.txt" in capsys.readouterr().err
