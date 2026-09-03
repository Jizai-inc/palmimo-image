#!/usr/bin/env python3
"""Collect GPL/LGPL corresponding source and third-party license metadata
onto the Palmimo image, at pi-gen build time, inside the chroot.

Copied into the chroot as a single stdlib-only file and run by
pigen/stage-palmimo/04-oss-compliance/00-run.sh, which deletes it before
the image ships. See README.md ("Licenses and corresponding source") and
doc/design.md for the policy this satisfies.

Every I/O boundary is a parameter (the `run` callable, rootfs path, output
paths), so this module is unit-testable without dpkg or network; only
main() wires it to the real subprocess/filesystem.

Final layout: <root>/usr/share/palmimo/sources/ (fetched source +
MANIFEST.txt, copied to <root>/boot/firmware/licenses/) and
<root>/boot/firmware/licenses/{pi,portal}/ (copyright/license text).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import glob
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path


DPKG_QUERY_SOURCE_FORMAT = "${binary:Package}\t${source:Package}\t${source:Version}\t${db:Status-Status}\n"
DPKG_QUERY_INSTALLED_FORMAT = "${Package}\t${db:Status-Status}\n"

_INSTALLED_STATUS = "installed"

# METADATA header fields worth keeping; everything else is body text.
_METADATA_HEADER_PREFIXES = ("License-Expression:", "License:", "Classifier: License ::")
_LICENSE_FILE_PREFIX = "License-File:"

RunFunc = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


# dpkg-query output -> (source, version) pairs / installed package names
def _parse_dpkg_query_lines(dpkg_query_output: str, field_count: int) -> list[tuple[str, ...]]:
    """Split dpkg-query output into stripped, tab-separated field tuples,
    skipping blank lines. Raises ValueError on a line with the wrong field
    count."""
    rows: list[tuple[str, ...]] = []
    for line in dpkg_query_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = tuple(line.split("\t"))
        if len(parts) != field_count:
            raise ValueError(f"unexpected dpkg-query line (expected {field_count} tab-separated fields): {line!r}")
        rows.append(parts)
    return rows


def list_source_packages(dpkg_query_output: str) -> set[tuple[str, str]]:
    """Parse dpkg-query output (DPKG_QUERY_SOURCE_FORMAT) into the set of
    installed (source package, source version) pairs to fetch."""
    return {
        (source, version)
        for _binary, source, version, status in _parse_dpkg_query_lines(dpkg_query_output, 4)
        if status == _INSTALLED_STATUS
    }


def list_installed_packages(dpkg_query_output: str) -> list[str]:
    """Parse dpkg-query output (DPKG_QUERY_INSTALLED_FORMAT) into the sorted
    list of installed package names."""
    return sorted(
        {name for name, status in _parse_dpkg_query_lines(dpkg_query_output, 2) if status == _INSTALLED_STATUS}
    )


# oss-source-exclude.txt / oss-copyright-missing-allow.txt (same format)
def parse_exclude_file(text: str) -> dict[str, str]:
    """Parse a name-list file (one name per line, optional `# reason`
    trailing comment; full-line comments and blanks ignored) into
    {name: reason}."""
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            name_part, _, reason_part = line.partition("#")
            name = name_part.strip()
            reason = reason_part.strip()
        else:
            name = line
            reason = ""
        if name:
            result[name] = reason
    return result


# .dsc Checksums-Sha256 parsing/verification
def parse_dsc_checksums_sha256(dsc_text: str) -> list[tuple[str, str, int]]:
    """Return (filename, sha256, size) tuples from a .dsc's Checksums-Sha256
    field. Returns [] if the field is absent."""
    lines = dsc_text.splitlines()
    entries: list[tuple[str, str, int]] = []
    in_section = False
    for line in lines:
        if line.startswith("Checksums-Sha256:"):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith(" ") or line.startswith("\t"):
            parts = line.split()
            if len(parts) == 3:
                sha, size, fname = parts
                entries.append((fname, sha, int(size)))
            continue
        break
    return entries


# fetch_sources: apt-get source --download-only --only-source <src>=<ver>
@dataclasses.dataclass(frozen=True)
class FetchResult:
    source: str
    version: str
    status: str  # "fetched" | "excluded" | "failed"
    dsc_path: Path | None
    reason: str | None
    # (filename, sha256, size) for the .dsc itself and every file it
    # declares via Checksums-Sha256 -- populated only for "fetched" results,
    # after every declared file's presence and checksum have been verified.
    files: tuple[tuple[str, str, int], ...] = ()


def _find_dsc(dest_dir: Path) -> Path | None:
    matches = sorted(dest_dir.glob("*.dsc"))
    return matches[0] if matches else None


def _verify_dsc(dsc_path: Path) -> tuple[list[str], list[tuple[str, str, int]]]:
    """Verify a fetched .dsc's Checksums-Sha256 field against the files on
    disk. Returns (problems, files); files includes the .dsc itself."""
    problems: list[str] = []
    dsc_sha = sha256_file(dsc_path)
    dsc_size = dsc_path.stat().st_size
    files: list[tuple[str, str, int]] = [(dsc_path.name, dsc_sha, dsc_size)]

    entries = parse_dsc_checksums_sha256(dsc_path.read_text(encoding="utf-8", errors="replace"))
    if not entries:
        problems.append(f"{dsc_path.name}: no Checksums-Sha256 field found")
        return problems, files

    for fname, expected_sha, expected_size in entries:
        fpath = dsc_path.parent / fname
        if not fpath.is_file():
            problems.append(f"{fname}: declared in Checksums-Sha256 but missing on disk")
            continue
        actual_sha = sha256_file(fpath)
        actual_size = fpath.stat().st_size
        if actual_sha != expected_sha:
            problems.append(f"{fname}: sha256 mismatch (expected {expected_sha}, got {actual_sha})")
            continue
        if actual_size != expected_size:
            problems.append(f"{fname}: size mismatch (expected {expected_size}, got {actual_size})")
            continue
        files.append((fname, actual_sha, actual_size))

    return problems, files


def fetch_sources(
    pairs: Iterable[tuple[str, str]],
    out_dir: Path,
    run: RunFunc,
    exclude: Mapping[str, str],
) -> list[FetchResult]:
    """Fetch every (source, version) pair into out_dir/<source>_<version>/
    via `apt-get source`, through the injected `run` callable. A fetch
    counts as failed unless every file the .dsc declares is present and
    hash-matches. Every pair is attempted even after an earlier failure;
    callers inspect failed_results / format_failure_report."""
    results: list[FetchResult] = []
    for source, version in sorted(pairs):
        if source in exclude:
            results.append(FetchResult(source, version, "excluded", None, exclude[source]))
            continue

        dest = out_dir / f"{source}_{version}"
        dest.mkdir(parents=True, exist_ok=True)
        proc = run(["apt-get", "source", "--download-only", "--only-source", f"{source}={version}"], dest)
        dsc_path = _find_dsc(dest)
        if proc.returncode == 0 and dsc_path is not None:
            problems, files = _verify_dsc(dsc_path)
            if problems:
                results.append(FetchResult(source, version, "failed", None, "; ".join(problems)))
            else:
                results.append(FetchResult(source, version, "fetched", dsc_path, None, files=tuple(files)))
        else:
            reason = proc.stderr.strip() or f"apt-get source exited {proc.returncode} and produced no .dsc"
            results.append(FetchResult(source, version, "failed", None, reason))
    return results


def failed_results(results: Iterable[FetchResult]) -> list[FetchResult]:
    return [r for r in results if r.status == "failed"]


def format_failure_report(failures: Iterable[FetchResult]) -> str:
    lines = ["failed to fetch corresponding source for the following package(s):"]
    for r in failures:
        lines.append(f"  {r.source}={r.version}: {r.reason}")
    return "\n".join(lines)


# write_manifest
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(
    out_path: Path,
    *,
    img_date: str,
    fetch_results: Iterable[FetchResult],
    skip_corresponding_source: bool,
    pi_missing_copyright: Iterable[str],
    portal_dists_count: int,
    portal_dists_without_license_text: Iterable[str],
    portal_third_party_licenses_present: bool,
    portal_python_runtime_dirs: Iterable[str],
) -> None:
    """Write MANIFEST.txt deterministically (sorted rows) and atomically
    (tmp file + os.replace). STATUS is OK unless a listed condition holds,
    in which case it is INCOMPLETE with every applicable reason listed."""
    dists_without_license_text = sorted(set(portal_dists_without_license_text))
    python_runtime_dirs = sorted(set(portal_python_runtime_dirs))

    status_reasons: list[str] = []
    if skip_corresponding_source:
        status_reasons.append("corresponding source was skipped (development build, NOT shippable)")
    if dists_without_license_text:
        status_reasons.append(
            "Python dists with no license text present (needs manual review): " + ", ".join(dists_without_license_text)
        )
    if not portal_third_party_licenses_present:
        status_reasons.append("frontend THIRD_PARTY_LICENSES.txt absent (portal tag predates it)")
    if python_runtime_dirs:
        status_reasons.append(
            "uv-managed Python runtime(s) found under .local/share/uv/python (needs a licensing decision): "
            + ", ".join(python_runtime_dirs)
        )

    lines: list[str] = []
    if status_reasons:
        lines.append(f"STATUS: INCOMPLETE -- {status_reasons[0]}")
        for reason in status_reasons[1:]:
            lines.append(f"STATUS reason: {reason}")
        lines.append("")
    else:
        lines.append("STATUS: OK")
        lines.append("")

    lines.append("Palmimo DevKit -- OSS corresponding source manifest")
    lines.append(f"Image build date: {img_date}")
    lines.append("Generated by: lib/collect_oss_compliance.py")
    lines.append(
        "Policy: GPLv2 section 3(a) / GPLv3 section 6(a) -- corresponding source for every "
        "apt source package shipped on this image is collected at build time and included "
        "on the same medium (Palmimo DevKit is sold, not distributed at no charge, so the "
        "written-offer fallback in GPLv2 3(b) / GPLv3 6(b) is not used)."
    )
    lines.append(
        "Exclusion policy: source packages listed in oss-source-exclude.txt are excluded -- "
        "hand-reviewed, non-free firmware blobs that carry no source-provision obligation. "
        "Every other source package on the image is included; apt metadata's license field is "
        "not trusted to filter by license family."
    )
    lines.append("")
    lines.append("source\tversion\tstatus\tfile\tsha256\tsize")

    rows: list[tuple[str, str, str, str, str, str]] = []
    for result in fetch_results:
        if result.status == "fetched":
            for fname, sha, size in result.files:
                rows.append(
                    (
                        result.source,
                        result.version,
                        result.status,
                        f"debian/{result.source}_{result.version}/{fname}",
                        sha,
                        str(size),
                    )
                )
        elif result.status == "excluded":
            rows.append((result.source, result.version, result.status, "-", f"excluded: {result.reason}", "-"))
        else:
            rows.append((result.source, result.version, result.status, "-", f"FAILED: {result.reason}", "-"))

    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    for row in rows:
        lines.append("\t".join(row))

    lines.append("")
    lines.append("apt package copyright files missing from /usr/share/doc/<package>/copyright:")
    lines.append("(allowed by oss-copyright-missing-allow.txt -- an unallowed miss fails the build)")
    missing = sorted(set(pi_missing_copyright))
    if missing:
        for name in missing:
            lines.append(f"  {name}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"portal python dists: {portal_dists_count}")

    lines.append("")
    lines.append("Python dists with no license text (needs manual review):")
    if dists_without_license_text:
        for name in dists_without_license_text:
            lines.append(f"  {name}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Palmimo Portal THIRD_PARTY_LICENSES.txt (generated npm dependency license file):")
    lines.append("  present" if portal_third_party_licenses_present else "  absent")

    lines.append("")
    lines.append("Uncovered binaries (needs a decision):")
    if python_runtime_dirs:
        lines.append("  uv-managed Python runtime(s) under ~/.local/share/uv/python/:")
        for name in python_runtime_dirs:
            lines.append(f"    {name}")
    else:
        lines.append("  (none)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_path, out_path)


# copy_pi_licenses: /usr/share/common-licenses + per-package copyright
@dataclasses.dataclass(frozen=True)
class PiLicenseReport:
    copied: list[str]
    missing: list[str]


def copy_pi_licenses(rootfs: Path, out_dir: Path, packages: Iterable[str]) -> PiLicenseReport:
    """Copy /usr/share/common-licenses/ and each installed package's
    /usr/share/doc/<package>/copyright into out_dir (symlinks resolved,
    via shutil.copyfile)."""
    common_src = rootfs / "usr" / "share" / "common-licenses"
    common_dst = out_dir / "common-licenses"
    if common_src.is_dir():
        common_dst.mkdir(parents=True, exist_ok=True)
        for entry in sorted(common_src.iterdir()):
            if entry.is_file():
                shutil.copyfile(entry, common_dst / entry.name)

    copied: list[str] = []
    missing: list[str] = []
    for package in sorted(set(packages)):
        copyright_src = rootfs / "usr" / "share" / "doc" / package / "copyright"
        if copyright_src.is_file():
            dest_dir = out_dir / package
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(copyright_src, dest_dir / "copyright")
            copied.append(package)
        else:
            missing.append(package)

    return PiLicenseReport(copied=copied, missing=missing)


# copy_portal_licenses: dist-info METADATA + License-File + repo-level notices
@dataclasses.dataclass(frozen=True)
class PortalLicenseReport:
    dists: list[str]
    dists_without_license_text: list[str]
    # (dist_name, relative_path) for every License-File: header whose target
    # did not actually exist in the dist-info directory.
    missing_license_files: list[tuple[str, str]]
    third_party_notices_present: bool
    third_party_licenses_present: bool


def _extract_license_metadata(metadata_text: str) -> tuple[list[str], list[str]]:
    """Return (header_lines_to_keep, license_file_relative_paths)."""
    header_lines: list[str] = []
    license_files: list[str] = []
    for line in metadata_text.splitlines():
        if line.startswith(_LICENSE_FILE_PREFIX):
            license_files.append(line[len(_LICENSE_FILE_PREFIX) :].strip())
            continue
        if any(line.startswith(prefix) for prefix in _METADATA_HEADER_PREFIXES):
            header_lines.append(line)
            continue
        if line == "":
            # Metadata headers end at the first blank line (RFC822-style);
            # everything after that is the long description body.
            break
    return header_lines, license_files


def copy_portal_licenses(venv_site_packages: Path, portal_root: Path, out_dir: Path) -> PortalLicenseReport:
    """For every *.dist-info/ in venv_site_packages, extract METADATA's
    license header lines into LICENSE-METADATA.txt and copy each
    License-File: target alongside it. A declared License-File that does
    not exist is fatal (see missing_license_files); a dist with neither a
    header nor a License-File is recorded in dists_without_license_text.
    Also copies THIRD_PARTY_NOTICES.md and, if present,
    THIRD_PARTY_LICENSES.txt from portal_root (absence is only a warning)."""
    python_out = out_dir / "python"
    dists: list[str] = []
    dists_without_license_text: list[str] = []
    missing_license_files: list[tuple[str, str]] = []

    if venv_site_packages.is_dir():
        for dist_info in sorted(venv_site_packages.glob("*.dist-info")):
            metadata_path = dist_info / "METADATA"
            if not metadata_path.is_file():
                continue
            header_lines, license_files = _extract_license_metadata(
                metadata_path.read_text(encoding="utf-8", errors="replace")
            )

            dist_name = dist_info.name.removesuffix(".dist-info")
            dists.append(dist_name)
            dist_dir = python_out / dist_name

            dist_missing = [relative for relative in license_files if not (dist_info / relative).is_file()]
            if dist_missing:
                for relative in dist_missing:
                    missing_license_files.append((dist_name, relative))
                continue

            if not header_lines and not license_files:
                dists_without_license_text.append(dist_name)
                continue

            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "LICENSE-METADATA.txt").write_text(
                "\n".join(header_lines) + ("\n" if header_lines else ""), encoding="utf-8"
            )

            for relative in license_files:
                src = dist_info / relative
                dest = dist_dir / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)

    notices_src = portal_root / "THIRD_PARTY_NOTICES.md"
    notices_present = notices_src.is_file()
    if notices_present:
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(notices_src, out_dir / "THIRD_PARTY_NOTICES.md")

    licenses_txt_src = portal_root / "palmimo_portal" / "static" / "THIRD_PARTY_LICENSES.txt"
    licenses_txt_present = licenses_txt_src.is_file()
    if licenses_txt_present:
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(licenses_txt_src, out_dir / "THIRD_PARTY_LICENSES.txt")
    else:
        sys.stderr.write(
            "WARNING: palmimo_portal/static/THIRD_PARTY_LICENSES.txt not found in the portal "
            "checkout -- continuing without it (recorded as absent in MANIFEST.txt, which marks "
            "STATUS INCOMPLETE). This is expected until the portal-side generation PR lands.\n"
        )

    return PortalLicenseReport(
        dists=sorted(dists),
        dists_without_license_text=sorted(dists_without_license_text),
        missing_license_files=missing_license_files,
        third_party_notices_present=notices_present,
        third_party_licenses_present=licenses_txt_present,
    )


# find_portal_venv_site_packages: glob, never guess a Python minor version
def find_portal_venv_site_packages(portal_root: Path) -> Path:
    """Locate the Portal venv's site-packages by globbing
    <portal_root>/.venv/lib/python*/site-packages (never hardcode the
    minor version). Raises RuntimeError if no candidate exists or every
    candidate has zero *.dist-info directories."""
    pattern = str(portal_root / ".venv" / "lib" / "python*" / "site-packages")
    candidates = sorted(Path(p) for p in glob.glob(pattern))
    existing = [c for c in candidates if c.is_dir()]
    if not existing:
        raise RuntimeError(f"no Portal venv site-packages found; looked for {pattern} (found nothing)")

    for candidate in existing:
        if any(candidate.glob("*.dist-info")):
            return candidate

    raise RuntimeError(
        "Portal venv site-packages found but every candidate has zero *.dist-info directories: "
        + ", ".join(str(c) for c in existing)
    )


# uv-managed Python runtime detection (portal_home/.local/share/uv/python/)
def find_uv_managed_pythons(portal_home: Path) -> list[Path]:
    """List python-build-standalone runtime dirs under
    <portal_home>/.local/share/uv/python/ (not license-reviewed by this
    script). Returns [] if the directory does not exist."""
    uv_python_dir = portal_home / ".local" / "share" / "uv" / "python"
    if not uv_python_dir.is_dir():
        return []
    return sorted(p for p in uv_python_dir.iterdir() if p.is_dir())


def copy_uv_python_runtime_licenses(uv_pythons: Iterable[Path], out_dir: Path) -> list[str]:
    """Copy each uv-managed runtime's licenses/ subdirectory (if present)
    to out_dir/<runtime-dir-name>/licenses/. Returns the names copied."""
    copied: list[str] = []
    for py_dir in uv_pythons:
        licenses_src = py_dir / "licenses"
        if not licenses_src.is_dir():
            continue
        dest = out_dir / py_dir.name / "licenses"
        shutil.copytree(licenses_src, dest, dirs_exist_ok=True)
        copied.append(py_dir.name)
    return copied


# main: wires the pure functions above to the real chroot filesystem/apt-get
def _real_run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """The one subprocess wrapper main() uses."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="/", help="Rootfs root to operate under (default: /, i.e. the chroot).")
    parser.add_argument(
        "--exclude-file",
        required=True,
        help="Path to oss-source-exclude.txt. Required: fails immediately if the path does not exist.",
    )
    parser.add_argument(
        "--copyright-allow-file",
        required=True,
        help="Path to oss-copyright-missing-allow.txt. Required: fails immediately if the path does not exist.",
    )
    parser.add_argument(
        "--portal-root",
        default="/home/user/palmimo-portal",
        help="Palmimo Portal repository checkout root (its venv is found by globbing "
        "<portal-root>/.venv/lib/python*/site-packages).",
    )
    parser.add_argument("--img-date", default=None, help="Build date recorded in MANIFEST.txt (default: today).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    exclude_path = Path(args.exclude_file)
    if not exclude_path.is_file():
        print(f"error: --exclude-file does not exist: {exclude_path}", file=sys.stderr)
        return 1
    copyright_allow_path = Path(args.copyright_allow_file)
    if not copyright_allow_path.is_file():
        print(f"error: --copyright-allow-file does not exist: {copyright_allow_path}", file=sys.stderr)
        return 1

    root = Path(args.root)
    portal_root = Path(args.portal_root)
    sources_out = root / "usr" / "share" / "palmimo" / "sources"
    licenses_out = root / "boot" / "firmware" / "licenses"
    img_date = args.img_date or datetime.date.today().isoformat()
    skip = os.environ.get("PALMIMO_SKIP_CORRESPONDING_SOURCE", "") == "1"

    exclude = parse_exclude_file(exclude_path.read_text(encoding="utf-8"))
    copyright_allow = parse_exclude_file(copyright_allow_path.read_text(encoding="utf-8"))

    # Wipe stale output from a previous interrupted/re-run build first.
    shutil.rmtree(sources_out, ignore_errors=True)
    shutil.rmtree(licenses_out / "pi", ignore_errors=True)
    shutil.rmtree(licenses_out / "portal", ignore_errors=True)

    try:
        venv_site_packages = find_portal_venv_site_packages(portal_root)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    fetch_results: list[FetchResult] = []
    if skip:
        sys.stderr.write(
            "WARNING: PALMIMO_SKIP_CORRESPONDING_SOURCE=1 -- corresponding source was NOT "
            "collected. This is a development build and MUST NOT ship.\n"
        )
    else:
        dpkg_proc = _real_run(["dpkg-query", "-W", "-f=" + DPKG_QUERY_SOURCE_FORMAT], root)
        if dpkg_proc.returncode != 0:
            print(f"error: dpkg-query failed: {dpkg_proc.stderr.strip()}", file=sys.stderr)
            return 1
        pairs = list_source_packages(dpkg_proc.stdout)

        unmatched_exclusions = set(exclude) - {source for source, _ in pairs}
        for name in sorted(unmatched_exclusions):
            print(f"WARNING: oss-source-exclude.txt lists {name!r}, which is not installed", file=sys.stderr)

        sources_out.mkdir(parents=True, exist_ok=True)
        fetch_results = fetch_sources(pairs, sources_out / "debian", run=_real_run, exclude=exclude)
        failures = failed_results(fetch_results)
        if failures:
            print(f"error: {format_failure_report(failures)}", file=sys.stderr)
            return 1

    installed_proc = _real_run(["dpkg-query", "-W", "-f=" + DPKG_QUERY_INSTALLED_FORMAT], root)
    if installed_proc.returncode != 0:
        print(f"error: dpkg-query failed: {installed_proc.stderr.strip()}", file=sys.stderr)
        return 1
    packages = list_installed_packages(installed_proc.stdout)

    pi_report = copy_pi_licenses(root, licenses_out / "pi", packages)
    unallowed_missing = sorted(set(pi_report.missing) - set(copyright_allow))
    if unallowed_missing:
        print(
            "error: apt package copyright missing and not in oss-copyright-missing-allow.txt: "
            + ", ".join(unallowed_missing),
            file=sys.stderr,
        )
        return 1
    for name in pi_report.missing:
        print(f"WARNING: no /usr/share/doc/{name}/copyright found -- recorded as missing (allowed)", file=sys.stderr)

    portal_report = copy_portal_licenses(venv_site_packages, portal_root, licenses_out / "portal")
    if portal_report.missing_license_files:
        details = ", ".join(f"{dist}: {path}" for dist, path in portal_report.missing_license_files)
        print(
            f"error: Portal Python dist(s) declare a License-File that does not exist: {details}",
            file=sys.stderr,
        )
        return 1
    if not portal_report.third_party_notices_present:
        print(
            f"WARNING: {portal_root}/THIRD_PARTY_NOTICES.md not found -- continuing without it",
            file=sys.stderr,
        )

    portal_home = portal_root.parent
    uv_pythons = find_uv_managed_pythons(portal_home)
    copy_uv_python_runtime_licenses(uv_pythons, licenses_out / "portal" / "python-runtime")

    sources_manifest = sources_out / "MANIFEST.txt"
    write_manifest(
        sources_manifest,
        img_date=img_date,
        fetch_results=fetch_results,
        skip_corresponding_source=skip,
        pi_missing_copyright=pi_report.missing,
        portal_dists_count=len(portal_report.dists),
        portal_dists_without_license_text=portal_report.dists_without_license_text,
        portal_third_party_licenses_present=portal_report.third_party_licenses_present,
        portal_python_runtime_dirs=[p.name for p in uv_pythons],
    )
    licenses_out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sources_manifest, licenses_out / "MANIFEST.txt")

    print("collect_oss_compliance.py: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
