#!/usr/bin/env python3
"""Collect GPL/LGPL corresponding source and third-party license metadata
onto the Palmimo image, at pi-gen build time, inside the chroot.

Runs the same way lib/patch_comitup_nm.py does: copied into the chroot as a
single stdlib-only file and invoked as `python3 collect_oss_compliance.py`
by pigen/stage-palmimo/04-oss-compliance/00-run.sh, then deleted -- the
shipped image never keeps this script. See README.md ("Licenses and
corresponding source") and doc/design.md ("対応ソースとライセンス全文の同梱")
for why this exists and what it satisfies.

Policy (GPLv2 section 3(a) / GPLv3 section 6(a)): Palmimo DevKit is sold,
not distributed at no charge, so the image ships the corresponding source
for every apt source package alongside the binaries, on the same medium --
not a written offer. The machine-readable license metadata on an apt
package is not trustworthy enough to filter by license family, so every
source package is collected except the explicit, hand-reviewed exclusion
list in oss-source-exclude.txt (non-free firmware blobs that carry no
source-provision obligation).

Every I/O boundary is a parameter -- the `run` callable that shells out to
apt-get, the rootfs path, and every output path -- so this module's logic
is unit-testable on a host with no dpkg database and no network (see
tests/test_collect_oss_compliance.py). Only main() wires those parameters
to the real subprocess/filesystem.

Final layout this script produces:

  <root>/usr/share/palmimo/sources/MANIFEST.txt
  <root>/usr/share/palmimo/sources/debian/<source>_<version>/*.dsc + tarballs
  <root>/boot/firmware/licenses/pi/common-licenses/
  <root>/boot/firmware/licenses/pi/<binary-package>/copyright
  <root>/boot/firmware/licenses/portal/python/<dist>-<version>/LICENSE-METADATA.txt (+ License-File copies)
  <root>/boot/firmware/licenses/portal/THIRD_PARTY_NOTICES.md
  <root>/boot/firmware/licenses/portal/THIRD_PARTY_LICENSES.txt (if the portal build produced one)
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path


DPKG_QUERY_FORMAT = "${binary:Package}\t${source:Package}\t${source:Version}\n"

# Read by copy_portal_licenses(): the METADATA header fields worth keeping.
# Everything else in a METADATA file is package body text (long description,
# author, project URLs, ...) that is not a license declaration and must not
# leak into LICENSE-METADATA.txt.
_METADATA_HEADER_PREFIXES = ("License-Expression:", "License:", "Classifier: License ::")
_LICENSE_FILE_PREFIX = "License-File:"

RunFunc = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


# ---------------------------------------------------------------------------
# dpkg-query output -> (source, version) pairs
# ---------------------------------------------------------------------------


def list_source_packages(dpkg_query_output: str) -> set[tuple[str, str]]:
    """Parse `dpkg-query -W -f='${binary:Package}\\t${source:Package}\\t${source:Version}\\n'`
    output into the set of (source package, source version) pairs to fetch.

    Multiple binary packages built from the same source collapse to one
    pair (dedup is the point: apt-get source only needs to run once per
    source package)."""
    pairs: set[tuple[str, str]] = set()
    for line in dpkg_query_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValueError(f"unexpected dpkg-query line (expected 3 tab-separated fields): {line!r}")
        _binary, source, version = parts
        pairs.add((source, version))
    return pairs


# ---------------------------------------------------------------------------
# oss-source-exclude.txt
# ---------------------------------------------------------------------------


def parse_exclude_file(text: str) -> dict[str, str]:
    """Parse oss-source-exclude.txt: one source package name per line, with
    an optional `# reason` trailing comment. Full-line comments and blank
    lines are ignored. Returns {name: reason} (reason is "" if none given)."""
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


# ---------------------------------------------------------------------------
# fetch_sources: apt-get source --download-only --only-source <src>=<ver>
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FetchResult:
    source: str
    version: str
    status: str  # "fetched" | "excluded" | "failed"
    dsc_path: Path | None
    reason: str | None


def _find_dsc(dest_dir: Path) -> Path | None:
    matches = sorted(dest_dir.glob("*.dsc"))
    return matches[0] if matches else None


def fetch_sources(
    pairs: Iterable[tuple[str, str]],
    out_dir: Path,
    run: RunFunc,
    exclude: Mapping[str, str],
) -> list[FetchResult]:
    """Fetch every (source, version) pair's corresponding source into
    out_dir/<source>_<version>/ via `apt-get source --download-only
    --only-source <source>=<version>`, run through the injected `run`
    callable (so this is testable without a real apt-get).

    Every pair is attempted, even after an earlier one fails -- callers
    decide what to do with the failures (see failed_results /
    format_failure_report) rather than this function aborting early, so a
    single bad mirror entry does not hide failures in the rest of the
    fetch."""
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
            results.append(FetchResult(source, version, "fetched", dsc_path, None))
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


# ---------------------------------------------------------------------------
# write_manifest
# ---------------------------------------------------------------------------


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
    portal_third_party_licenses_present: bool,
) -> None:
    """Write MANIFEST.txt. Deterministic: fetch_results is sorted by source
    name regardless of input order, so a re-run over the same inputs
    produces a byte-identical file (useful for diffing images)."""
    lines: list[str] = []
    if skip_corresponding_source:
        lines.append("STATUS: INCOMPLETE -- corresponding source was skipped (development build, NOT shippable)")
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
    lines.append("source\tversion\tstatus\tfile\tsha256")

    for result in sorted(fetch_results, key=lambda r: r.source):
        if result.status == "fetched":
            assert result.dsc_path is not None
            file_field = f"debian/{result.source}_{result.version}/{result.dsc_path.name}"
            sha_field = sha256_file(result.dsc_path)
        elif result.status == "excluded":
            file_field = "-"
            sha_field = f"excluded: {result.reason}"
        else:
            file_field = "-"
            sha_field = f"FAILED: {result.reason}"
        lines.append(f"{result.source}\t{result.version}\t{result.status}\t{file_field}\t{sha_field}")

    lines.append("")
    lines.append("apt package copyright files missing from /usr/share/doc/<package>/copyright:")
    missing = sorted(pi_missing_copyright)
    if missing:
        for name in missing:
            lines.append(f"  {name}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Palmimo Portal THIRD_PARTY_LICENSES.txt (generated npm dependency license file):")
    lines.append("  present" if portal_third_party_licenses_present else "  absent")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# copy_pi_licenses: /usr/share/common-licenses + per-package copyright
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PiLicenseReport:
    copied: list[str]
    missing: list[str]


def copy_pi_licenses(rootfs: Path, out_dir: Path, packages: Iterable[str]) -> PiLicenseReport:
    """Copy /usr/share/common-licenses/ and each installed package's
    /usr/share/doc/<package>/copyright into out_dir. A copyright file that
    is itself a symlink (common when several binary packages share one
    source's copyright file) is copied by its resolved content, not as a
    dangling/relative symlink -- shutil.copyfile follows symlinks by
    default, which is exactly the behavior wanted here."""
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


# ---------------------------------------------------------------------------
# copy_portal_licenses: dist-info METADATA + License-File + repo-level notices
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PortalLicenseReport:
    dists: list[str]
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
    """Process every *.dist-info/ in venv_site_packages: extract the
    License-Expression / License / Classifier: License :: header lines from
    METADATA into <out_dir>/python/<dist>-<version>/LICENSE-METADATA.txt,
    and copy each file named by a License-File: header (relative to the
    dist-info directory, commonly directly inside it or under a licenses/
    subdirectory) alongside it.

    Also copies portal_root/THIRD_PARTY_NOTICES.md and, if it exists,
    palmimo_portal/static/THIRD_PARTY_LICENSES.txt (generated by a parallel
    portal-side PR) -- its absence is only a warning, never a hard failure,
    since a portal build without it should still produce an image (the
    manifest is where "absent" gets recorded so it isn't silently lost)."""
    python_out = out_dir / "python"
    dists: list[str] = []

    if venv_site_packages.is_dir():
        for dist_info in sorted(venv_site_packages.glob("*.dist-info")):
            metadata_path = dist_info / "METADATA"
            if not metadata_path.is_file():
                continue
            header_lines, license_files = _extract_license_metadata(
                metadata_path.read_text(encoding="utf-8", errors="replace")
            )

            dist_name = dist_info.name.removesuffix(".dist-info")
            dist_dir = python_out / dist_name
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "LICENSE-METADATA.txt").write_text(
                "\n".join(header_lines) + ("\n" if header_lines else ""), encoding="utf-8"
            )

            for relative in license_files:
                src = dist_info / relative
                if not src.is_file():
                    continue
                dest = dist_dir / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)

            dists.append(dist_name)

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
            "checkout -- continuing without it (recorded as absent in MANIFEST.txt). This is "
            "expected until the portal-side generation PR lands.\n"
        )

    return PortalLicenseReport(
        dists=sorted(dists),
        third_party_notices_present=notices_present,
        third_party_licenses_present=licenses_txt_present,
    )


# ---------------------------------------------------------------------------
# main: wires the pure functions above to the real chroot filesystem/apt-get
# ---------------------------------------------------------------------------


def _real_run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _dpkg_installed_packages(run_query: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> list[str]:
    proc = run_query(["dpkg-query", "-W", "-f=${Package}\n"])
    if proc.returncode != 0:
        raise RuntimeError(f"dpkg-query -W failed: {proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="/", help="Rootfs root to operate under (default: /, i.e. the chroot).")
    parser.add_argument(
        "--exclude-file",
        default=None,
        help="Path to oss-source-exclude.txt (default: <root's mounted image dir is not known here -- "
        "pass explicitly>).",
    )
    parser.add_argument(
        "--venv-site-packages",
        default="/home/user/palmimo-portal/.venv/lib/python3.12/site-packages",
        help="Palmimo Portal venv's site-packages directory.",
    )
    parser.add_argument(
        "--portal-root",
        default="/home/user/palmimo-portal",
        help="Palmimo Portal repository checkout root.",
    )
    parser.add_argument("--img-date", default=None, help="Build date recorded in MANIFEST.txt (default: today).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root = Path(args.root)
    sources_out = root / "usr" / "share" / "palmimo" / "sources"
    licenses_out = root / "boot" / "firmware" / "licenses"
    img_date = args.img_date or datetime.date.today().isoformat()
    skip = os.environ.get("PALMIMO_SKIP_CORRESPONDING_SOURCE", "") == "1"

    exclude: dict[str, str] = {}
    if args.exclude_file:
        exclude_path = Path(args.exclude_file)
        if exclude_path.is_file():
            exclude = parse_exclude_file(exclude_path.read_text(encoding="utf-8"))

    def run_query(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    fetch_results: list[FetchResult] = []
    if skip:
        sys.stderr.write(
            "WARNING: PALMIMO_SKIP_CORRESPONDING_SOURCE=1 -- corresponding source was NOT "
            "collected. This is a development build and MUST NOT ship.\n"
        )
    else:
        dpkg_proc = run_query(["dpkg-query", "-W", "-f=" + DPKG_QUERY_FORMAT])
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

    packages = _dpkg_installed_packages(run_query)
    pi_report = copy_pi_licenses(root, licenses_out / "pi", packages)
    for name in pi_report.missing:
        print(f"WARNING: no /usr/share/doc/{name}/copyright found -- recorded as missing", file=sys.stderr)

    portal_report = copy_portal_licenses(Path(args.venv_site_packages), Path(args.portal_root), licenses_out / "portal")
    if not portal_report.third_party_notices_present:
        print(
            f"WARNING: {args.portal_root}/THIRD_PARTY_NOTICES.md not found -- continuing without it",
            file=sys.stderr,
        )

    write_manifest(
        sources_out / "MANIFEST.txt",
        img_date=img_date,
        fetch_results=fetch_results,
        skip_corresponding_source=skip,
        pi_missing_copyright=pi_report.missing,
        portal_third_party_licenses_present=portal_report.third_party_licenses_present,
    )

    print("collect_oss_compliance.py: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
