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

# 1. Enable deb-src temporarily. apt-get source needs deb-src entries to
#    resolve a source package; the shipped image must not carry them (a
#    purchaser's device has no business fetching Debian source packages),
#    so this sources file is written, used, and removed within this one
#    stage script -- never present when 01-palmimo-core's rsync or any
#    later stage runs.
if [ "${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" != "1" ]; then
	install -m 0644 "files/palmimo-src.sources" \
		"${ROOTFS_DIR}/etc/apt/sources.list.d/palmimo-src.sources"
	sed -i "s/RELEASE/${RELEASE}/g" "${ROOTFS_DIR}/etc/apt/sources.list.d/palmimo-src.sources"
	on_chroot <<- 'EOF'
		apt-get update
	EOF
fi

# 2. Run the collector. Same "copy in, run, delete" contract as
#    lib/patch_comitup_nm.py in 01-palmimo-core: the shipped image never
#    keeps this script or the exclusion list.
install -m 0644 "${PALMIMO_IMAGE_DIR}/lib/collect_oss_compliance.py" "${ROOTFS_DIR}/tmp/collect_oss_compliance.py"
install -m 0644 "${PALMIMO_IMAGE_DIR}/oss-source-exclude.txt" "${ROOTFS_DIR}/tmp/oss-source-exclude.txt"

IMG_DATE="$(date +%Y-%m-%d)"
on_chroot <<- EOF
	PALMIMO_SKIP_CORRESPONDING_SOURCE="${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" \
		python3 /tmp/collect_oss_compliance.py \
		--root / \
		--exclude-file /tmp/oss-source-exclude.txt \
		--img-date "${IMG_DATE}"
EOF

rm -f "${ROOTFS_DIR}/tmp/collect_oss_compliance.py" "${ROOTFS_DIR}/tmp/oss-source-exclude.txt"

# 3. Disable deb-src again -- never ship it. Re-running apt-get update
#    afterward keeps the package index consistent with the sources that
#    actually remain (matches the "leave apt in a state matching what's on
#    disk" contract the rest of this stage relies on).
if [ "${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" != "1" ]; then
	rm -f "${ROOTFS_DIR}/etc/apt/sources.list.d/palmimo-src.sources"
	on_chroot <<- 'EOF'
		apt-get update
	EOF
fi
