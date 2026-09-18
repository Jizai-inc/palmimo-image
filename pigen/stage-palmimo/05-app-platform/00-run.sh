#!/bin/bash -e
# App execution platform (palmimo-platform bundle, see platform/). Runs
# after 03-portal (needs the "user" account and Portal's own service unit
# already in place) so the platform bundle's polkit/journald drop-ins land
# on top of them. No platform-specific paths belong in this file or in
# apply-pi.sh's equivalent step -- both just call the bundle's installer,
# see doc/design.md and platform/README section for why.

: "${PALMIMO_IMAGE_DIR:?PALMIMO_IMAGE_DIR is unset -- see pigen/README.md (PIGEN_DOCKER_OPTS bind mount)}"

"${PALMIMO_IMAGE_DIR}/platform/install.sh" install --root "${ROOTFS_DIR}"

# bundle_sha256 identifies exactly which bundle tarball this came from (see
# platform/README section, "existing device" case: it downloads and hashes
# the released tarball). Building the same tarball here, from the same
# platform/ tree, keeps a fresh image's record comparable to an existing
# device's -- see doc/design/palmimo-app-platform.md 2.8 (palmimo-devkit
# monorepo), "収束の保証".
BUNDLE_TAR="$(mktemp -u).tar.gz"
BUNDLE_SHA256="$(python3 "${PALMIMO_IMAGE_DIR}/tools/build_platform_bundle.py" --out "${BUNDLE_TAR}")"
rm -f "${BUNDLE_TAR}"
"${PALMIMO_IMAGE_DIR}/platform/install.sh" record --root "${ROOTFS_DIR}" --sha "${BUNDLE_SHA256}"
