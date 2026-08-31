#!/bin/bash -e
# Standard pi-gen prerun: bring the previous stage's rootfs forward.
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi

# 00-packages/00-packages is generated here, not checked in, so
# packages.txt (shared with apply-pi.sh) is the only copy of this list --
# see pigen/README.md, "Why symlink stage-palmimo in but
# bind-mount everything else".
: "${PALMIMO_IMAGE_DIR:?PALMIMO_IMAGE_DIR is unset -- see pigen/README.md (PIGEN_DOCKER_OPTS bind mount)}"
if [ ! -f "${PALMIMO_IMAGE_DIR}/packages.txt" ]; then
	echo "PALMIMO_IMAGE_DIR (${PALMIMO_IMAGE_DIR}) does not contain packages.txt -- is the bind mount set up?" >&2
	exit 1
fi
mkdir -p "${STAGE_DIR}/00-packages"
cp "${PALMIMO_IMAGE_DIR}/packages.txt" "${STAGE_DIR}/00-packages/00-packages"
