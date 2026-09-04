#!/bin/bash -e
# Corresponding source (GPLv2 3(a) / GPLv3 6(a)) and third-party license
# metadata, collected from the packages installed in this chroot -- see
# README.md and doc/design.md. Runs after 03-portal (Portal venv must exist).
# PALMIMO_SKIP_CORRESPONDING_SOURCE=1 skips the slow apt-get source step for
# dev-loop rebuilds; see pigen/README.md and README.md.

: "${PALMIMO_IMAGE_DIR:?PALMIMO_IMAGE_DIR is unset -- see pigen/README.md (PIGEN_DOCKER_OPTS bind mount)}"

SOURCES_FILE="${ROOTFS_DIR}/etc/apt/sources.list.d/palmimo-src.sources"
COLLECTOR_TMP="${ROOTFS_DIR}/tmp/collect_oss_compliance.py"
EXCLUDE_TMP="${ROOTFS_DIR}/tmp/oss-source-exclude.txt"
COPYRIGHT_ALLOW_TMP="${ROOTFS_DIR}/tmp/oss-copyright-missing-allow.txt"

# trap fires on every exit path, skip flag included; always cleaned up here.
cleanup() {
	rm -f "${SOURCES_FILE}" "${COLLECTOR_TMP}" "${EXCLUDE_TMP}" "${COPYRIGHT_ALLOW_TMP}"
}
trap cleanup EXIT
cleanup

# 1. Enable deb-src temporarily -- apt-get source needs it, the shipped
#    image must not carry it.
if [ "${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" != "1" ]; then
	install -m 0644 "files/palmimo-src.sources" "${SOURCES_FILE}"
	sed -i "s/RELEASE/${RELEASE}/g" "${SOURCES_FILE}"
	on_chroot <<- 'EOF'
		apt-get update
	EOF
fi

# 2. Run the collector (copy in, run, delete -- same contract as
#    lib/patch_comitup_nm.py in 01-palmimo-core).
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

# 3. Disable deb-src again: delete just the Sources indexes step 1 wrote,
#    instead of refreshing the whole apt cache a second time.
if [ "${PALMIMO_SKIP_CORRESPONDING_SOURCE:-}" != "1" ]; then
	on_chroot <<- 'EOF'
		rm -f /var/lib/apt/lists/*_Sources /var/lib/apt/lists/*_source_*
	EOF
fi
