#!/bin/bash -e
# palmimo-portal install. Same steps as apply-pi.sh's step [3/9]-[4/9], run
# in the chroot instead of over SSH; see doc/design.md for the
# unit contract this venv/repo layout satisfies (palmimo-portal.service's
# ExecStart is /home/user/palmimo-portal/.venv/bin/python -m palmimo_portal).

PALMIMO_PORTAL_TAG="${PALMIMO_PORTAL_TAG:-v0.1.0}"
PORTAL_REPO_URL="https://github.com/Jizai-inc/palmimo-portal.git"
PORTAL_HOME="/home/${FIRST_USER_NAME}"
PORTAL_DEST="${PORTAL_HOME}/palmimo-portal"
UV_BIN="${PORTAL_HOME}/.local/bin/uv"

# All paths below are resolved here (host side, at pi-gen build time), not
# re-derived from $HOME inside the chroot -- avoids the multi-level heredoc
# escaping a chroot-time ${HOME} expansion would need.
on_chroot <<- EOF
	su - "${FIRST_USER_NAME}" -c "test -x '${UV_BIN}' || curl -LsSf https://astral.sh/uv/install.sh | sh"
	su - "${FIRST_USER_NAME}" -c "git clone --branch '${PALMIMO_PORTAL_TAG}' --depth 1 '${PORTAL_REPO_URL}' '${PORTAL_DEST}'"
	su - "${FIRST_USER_NAME}" -c "cd '${PORTAL_DEST}' && '${UV_BIN}' sync --frozen --no-dev"
	su - "${FIRST_USER_NAME}" -c "cd '${PORTAL_DEST}' && .venv/bin/python -m palmimo_portal.fetch_static --tag '${PALMIMO_PORTAL_TAG}'"
EOF
