#!/bin/bash -e
# App execution platform (palmimo-platform bundle, see platform/). Runs
# after 03-portal (needs the "user" account and Portal's own service unit
# already in place) so the platform bundle's polkit/journald drop-ins land
# on top of them. No platform-specific paths belong in this file or in
# apply-pi.sh's equivalent step -- both just call the bundle's installer,
# see doc/design.md and platform/README section for why.
#
# The installer runs inside the chroot with --root /, the same mode a field
# update uses: the pi-gen build container has no python3, and inside the
# chroot every account name resolves against the image's own passwd/group.

: "${PALMIMO_IMAGE_DIR:?PALMIMO_IMAGE_DIR is unset -- see pigen/README.md (PIGEN_DOCKER_OPTS bind mount)}"

BUNDLE_TMP="${ROOTFS_DIR}/tmp/palmimo-platform-src"

cleanup() {
	rm -rf "${BUNDLE_TMP}"
}
trap cleanup EXIT
cleanup

# tools/build_platform_bundle.py locates platform/ relative to itself, so
# the copy keeps the repository layout.
mkdir -p "${BUNDLE_TMP}/tools"
cp -a "${PALMIMO_IMAGE_DIR}/platform" "${BUNDLE_TMP}/platform"
cp -a "${PALMIMO_IMAGE_DIR}/tools/build_platform_bundle.py" "${BUNDLE_TMP}/tools/"

# bundle_sha256 identifies exactly which bundle tarball this came from (see
# platform/README section, "existing device" case: it downloads and hashes
# the released tarball). Building the same tarball here, from the same
# platform/ tree, keeps a fresh image's record comparable to an existing
# device's -- see doc/design/palmimo-app-platform.md 2.8 (palmimo-devkit
# monorepo), "収束の保証". verify fails the build on any drift the install
# left behind.
on_chroot <<- 'EOF'
	set -e
	src=/tmp/palmimo-platform-src
	"$src/platform/install.sh" install --root /
	sha="$(python3 "$src/tools/build_platform_bundle.py" --out "$src/bundle.tar.gz")"
	"$src/platform/install.sh" record --root / --sha "$sha"
	"$src/platform/install.sh" verify --root /
EOF
