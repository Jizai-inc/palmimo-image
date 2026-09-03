#!/bin/bash -e
# Corresponding source (GPLv2 3(a) / GPLv3 6(a)) and third-party license
# metadata, collected at build time from the packages actually installed in
# this chroot -- see README.md ("Licenses and corresponding source") and
# doc/design.md ("対応ソースとライセンス全文の同梱"). Runs after 03-portal so
# the Portal venv (and its dist-info license metadata) already exists.
#
# PALMIMO_SKIP_CORRESPONDING_SOURCE=1 skips only the apt-get source step
# (a slow full source-package download) for fast dev-loop rebuilds -- the
# apt/Portal license copying always runs, and the manifest is stamped
# STATUS: INCOMPLETE so a build made this way cannot be mistaken for a
# shippable one. See pigen/README.md and README.md for the flag.

: "${PALMIMO_IMAGE_DIR:?PALMIMO_IMAGE_DIR is unset -- see pigen/README.md (PIGEN_DOCKER_OPTS bind mount)}"

SOURCES_FILE="${ROOTFS_DIR}/etc/apt/sources.list.d/palmimo-src.sources"
COLLECTOR_TMP="${ROOTFS_DIR}/tmp/collect_oss_compliance.py"
EXCLUDE_TMP="${ROOTFS_DIR}/tmp/oss-source-exclude.txt"
COPYRIGHT_ALLOW_TMP="${ROOTFS_DIR}/tmp/oss-copyright-missing-allow.txt"

# Unconditional cleanup of everything this stage writes outside the final
# image: the temporarily-enabled deb-src entry and the scratch
# collector/exclude-list/allow-list copies. Wired up two ways so a
# previous interrupted build's leftovers or this run's own failure never
# survive to the next stage or the next build:
#   - `trap cleanup EXIT` fires no matter how this script exits (normal
#     completion, `set -e` aborting on an error, or the
#     PALMIMO_SKIP_CORRESPONDING_SOURCE=1 dev-loop path that never touches
#     deb-src at all) -- never put this only inside the `if [ ... != 1 ]`
#     branch below, or a skip-flag build would skip cleanup entirely.
#   - the explicit call right after defining it also cleans up anything a
#     previous run left behind (e.g. this script was killed mid-stage)
#     before this run writes anything new.
cleanup() {
	rm -f "${SOURCES_FILE}" "${COLLECTOR_TMP}" "${EXCLUDE_TMP}" "${COPYRIGHT_ALLOW_TMP}"
}
trap cleanup EXIT
cleanup

# 1. Enable deb-src temporarily. apt-get source needs deb-src entries to
#    resolve a source package; the shipped image must not carry them (a
#    purchaser's device has no business fetching Debian source packages),
#    so this sources file is written, used, and removed within this one
#    stage script -- never present when 01-palmimo-core's rsync or any
#    later stage runs.
if [ "${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" != "1" ]; then
	install -m 0644 "files/palmimo-src.sources" "${SOURCES_FILE}"
	sed -i "s/RELEASE/${RELEASE}/g" "${SOURCES_FILE}"
	on_chroot <<- 'EOF'
		apt-get update
	EOF
fi

# 2. Run the collector. Same "copy in, run, delete" contract as
#    lib/patch_comitup_nm.py in 01-palmimo-core: the shipped image never
#    keeps this script, the exclusion list, or the copyright-allow list.
install -m 0644 "${PALMIMO_IMAGE_DIR}/lib/collect_oss_compliance.py" "${COLLECTOR_TMP}"
install -m 0644 "${PALMIMO_IMAGE_DIR}/oss-source-exclude.txt" "${EXCLUDE_TMP}"
install -m 0644 "${PALMIMO_IMAGE_DIR}/oss-copyright-missing-allow.txt" "${COPYRIGHT_ALLOW_TMP}"

IMG_DATE="$(date +%Y-%m-%d)"
on_chroot <<- EOF
	PALMIMO_SKIP_CORRESPONDING_SOURCE="${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" \
		python3 /tmp/collect_oss_compliance.py \
		--root / \
		--exclude-file /tmp/oss-source-exclude.txt \
		--copyright-allow-file /tmp/oss-copyright-missing-allow.txt \
		--img-date "${IMG_DATE}"
EOF

# 3. Disable deb-src again -- never ship it. The `trap` above removes the
#    sources file itself on exit either way; here we only need to undo the
#    side effect of step 1's index refresh having indexed it. Rather than
#    refreshing the index a second time (a redundant network round-trip
#    the build does not otherwise need), delete just the Sources indexes
#    that step 1 wrote for the deb-src entry -- the on-disk apt state ends
#    up matching "deb-src never existed" without depending on the network
#    again.
if [ "${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" != "1" ]; then
	on_chroot <<- 'EOF'
		rm -f /var/lib/apt/lists/*_Sources /var/lib/apt/lists/*_source_*
	EOF
fi
