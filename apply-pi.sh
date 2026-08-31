#!/usr/bin/env bash
# Apply the Palmimo image layer to a stock Raspberry Pi OS Lite (64-bit,
# trixie) over SSH. Development-loop tool: shares the files/ tree and the
# unmask/rsync/enable logic with the pi-gen custom stage (see
# doc/design.md, "pi-gen イメージビルドと焼き込み CLI") so the
# two never diverge on where things go.
#
# Prereqs (one-time, on the target Pi):
#   - SSH key auth (`ssh "$PI_HOST"` works without a password)
#   - Passwordless sudo for the Pi user (stock Raspberry Pi OS default —
#     the `user` account is in group `sudo` with a NOPASSWD sudoers drop-in)
#
# Usage:
#   PI_HOST=user@<addr> PORTAL_TAG=v0.1.0-rc1 apply-pi.sh
#   PI_HOST=user@<addr> PORTAL_TAG=v0.1.0-rc1 apply-pi.sh --identity ./palmimo-identity.json
#   PI_HOST=user@<addr> PORTAL_TAG=v0.1.0-rc1 apply-pi.sh --no-apt
#
# The Pi clones https://github.com/Jizai-inc/palmimo-portal.git (public)
# anonymously over HTTPS. This script does not bake in or accept a token.
set -euo pipefail

: "${PI_HOST:?PI_HOST=<pi-user>@<addr> を指定してください}"
: "${PORTAL_TAG:?PORTAL_TAG=<tag> を指定してください（例: v0.1.0-rc1）}"

if ! [[ "$PORTAL_TAG" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "PORTAL_TAG に使用できない文字が含まれています（許可: [A-Za-z0-9._-]）: ${PORTAL_TAG}" >&2
  exit 2
fi

IMAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_SRC="${IMAGE_DIR}/files/"
PACKAGES_FILE="${IMAGE_DIR}/packages.txt"
PATCH_NM_PY_SCRIPT="${IMAGE_DIR}/lib/patch_comitup_nm.py"

PORTAL_REPO_URL="https://github.com/Jizai-inc/palmimo-portal.git"
PORTAL_DEST="/home/user/palmimo-portal"

identity_path=""
no_apt=0

while [ $# -gt 0 ]; do
  case "$1" in
    --identity)
      identity_path="${2:?--identity requires a path}"
      shift 2
      ;;
    --no-apt)
      no_apt=1
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ -n "$identity_path" ] && [ ! -f "$identity_path" ]; then
  echo "--identity file not found: $identity_path" >&2
  exit 1
fi

ssh_run() {
  # -o BatchMode=yes: never fall back to an interactive password prompt —
  # a hung apply run is worse than a fast, loud failure here.
  ssh -o BatchMode=yes "$PI_HOST" "$@"
}

# One package per line in packages.txt — the single source of truth shared
# with the pi-gen custom stage's 00-packages step (see
# doc/design.md, "pi-gen イメージビルドと焼き込み CLI").
PACKAGES="$(tr '\n' ' ' <"$PACKAGES_FILE")"
echo "==> [1/9] apt packages ($PACKAGES)"
if [ "$no_apt" = 1 ]; then
  echo "    --no-apt: skipped"
else
  # idempotent: apt install on an already-installed package is a no-op.
  # dnsmasq: comitup spawns its own dnsmasq instance for hotspot DHCP/DNS
  # (cdns.py) but does not declare it as a hard apt dependency — without the
  # binary present, clients associate to the AP but never get a DHCP lease
  # (verified on-device). The system dnsmasq.service must not hold port 53,
  # since comitup's own instance needs it, so disable the service right after
  # install; only the binary is needed.
  # shellcheck disable=SC2086
  ssh_run "sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y $PACKAGES"
  # idempotent: disable --now is a no-op if already disabled/stopped.
  ssh_run "sudo systemctl disable --now dnsmasq"
fi

echo "==> [2/9] comitup nm.py: WPA2/PMF hotspot-security patch"
# lib/patch_comitup_nm.py is the single source of truth for this
# patch, shared with the pi-gen custom stage (which copies the same file
# into the chroot and runs it there -- see doc/design.md,
# "pi-gen イメージビルドと焼き込み CLI"). Piped over stdin into a remote
# `sudo python3 -` so no copy of the script is left behind on the Pi.
ssh -o BatchMode=yes "$PI_HOST" 'sudo python3 -' <"$PATCH_NM_PY_SCRIPT"

echo "==> [3/9] uv"
# idempotent: only installs if ~/.local/bin/uv is missing. Single-quoted on
# purpose: $HOME must expand on the remote (Pi) shell, not here.
# shellcheck disable=SC2016
ssh_run 'test -x "$HOME/.local/bin/uv" || curl -LsSf https://astral.sh/uv/install.sh | sh'

echo "==> [4/9] palmimo-portal @ ${PORTAL_TAG}"
# Idempotent clone-or-update: a fresh Pi gets a tagged clone; a Pi that
# already has the repo gets `fetch` + `checkout --detach` to the same tag,
# which also converges a Pi that was previously applied at a different tag.
ssh_run "set -euo pipefail; \
  if [ -d '${PORTAL_DEST}/.git' ]; then \
    cd '${PORTAL_DEST}' && git fetch --tags origin && git checkout --detach 'tags/${PORTAL_TAG}'; \
  else \
    git clone --branch '${PORTAL_TAG}' --depth 1 '${PORTAL_REPO_URL}' '${PORTAL_DEST}'; \
  fi"
# uv sync --frozen: install exactly what the tagged lockfile pins, never
# resolve a new one on the device.
ssh_run "cd '${PORTAL_DEST}' && \$HOME/.local/bin/uv sync --frozen"
# Same code path the Updater uses to fetch/verify the static frontend asset
# for a given tag, so apply-pi.sh and the Updater can never disagree about
# what "the static assets for this tag" means.
ssh_run "cd '${PORTAL_DEST}' && .venv/bin/python -m palmimo_portal.fetch_static --tag '${PORTAL_TAG}'"

echo "==> [5/9] unmask comitup-web"
# Heals devices applied with a previous version of this script, which masked
# comitup-web. Masking makes /etc/systemd/system/comitup-web.service a
# symlink to /dev/null — the same path the no-op replacement unit in files/
# needs to occupy — so this must run before the files/ rsync below, or the
# rsync would try to write through (or be shadowed by) the /dev/null symlink.
# Idempotent: unmask on an already-unmasked (or never-masked) unit is a no-op.
ssh_run "sudo systemctl unmask comitup-web"

echo "==> [6/9] files/ -> / (sudo rsync)"
# --rsync-path='sudo rsync': the Pi user has no write access to /etc or
# /usr/local outside sudo. -a preserves the file modes files/ was checked in
# with (services/rules should not be group/world-writable — see
# tests/test_image_contracts.py). No --delete: this tree only ever
# adds/overwrites known paths, never deletes files a hand-applied Pi might
# have added under the same directories for other reasons.
rsync -az \
  --rsync-path='sudo rsync' \
  -e 'ssh -o BatchMode=yes' \
  "$FILES_SRC" "${PI_HOST}:/"

echo "==> [7/9] systemd: daemon-reload, enable units"
# comitup-web is intentionally NOT masked or enabled here: files/ just placed
# the no-op replacement unit at its path (see files and
# doc/design.md, comitup 設定). comitup's webmgr.py
# unconditionally starts comitup-web on HOTSPOT entry; a masked unit raises
# DBusException there and aborts the rest of hotspot setup (dnsmasq
# included) — verified on-device. The no-op unit lets that start succeed
# while structurally keeping the real comitup-web off port 80.
ssh_run "set -euo pipefail; \
  sudo systemctl daemon-reload; \
  sudo systemctl enable comitup avahi-daemon palmimo-portal palmimo-firstboot"

echo "==> [8/9] self-checks"
# Wi-Fi definition in /etc/network/interfaces: FAIL, do not auto-fix. A
# hand-set Wi-Fi definition there is a deliberate choice on a hand-flashed
# Pi and comitup expects to own the interface; silently deleting someone's
# config is worse than telling them to remove it themselves.
if ssh_run "grep -Eq '^[[:space:]]*(iface|wpa-|wireless-)' /etc/network/interfaces 2>/dev/null || grep -Eq 'wlan' /etc/network/interfaces 2>/dev/null"; then
  echo "    FAIL: /etc/network/interfaces defines Wi-Fi — comitup cannot manage the interface." >&2
  echo "    Remove the Wi-Fi stanza by hand and re-run apply-pi.sh." >&2
  exit 1
fi
echo "    ok: no Wi-Fi definition in /etc/network/interfaces"

# Wi-Fi country code: set if not already JP. raspi-config nonint is idempotent.
ssh_run "sudo raspi-config nonint do_wifi_country JP"
echo "    ok: Wi-Fi country set to JP"

expected_enabled="comitup avahi-daemon palmimo-portal palmimo-firstboot"
for unit in $expected_enabled; do
  # String-compare, not exit code: is-enabled exits 0 for "static" too,
  # which silently passed a unit whose [Install] section was missing.
  if [ "$(ssh_run "systemctl is-enabled '$unit'" || true)" != "enabled" ]; then
    echo "    FAIL: $unit is not enabled" >&2
    exit 1
  fi
done
echo "    ok: units enabled: $expected_enabled"

if ! ssh_run "curl -fsS -o /dev/null -w '%{http_code}' http://localhost:80/api/v1/system/status" | grep -q '^200$'; then
  echo "    FAIL: http://localhost:80/api/v1/system/status did not return 200" >&2
  exit 1
fi
echo "    ok: Portal responding on :80"

if ! ssh_run "command -v dnsmasq"; then
  echo "    FAIL: dnsmasq binary not present — comitup hotspot DHCP/DNS will not come up" >&2
  exit 1
fi
echo "    ok: dnsmasq binary present"

if ! ssh_run "systemctl cat comitup-web" | grep -q '^ExecStart=/bin/true$'; then
  echo "    FAIL: comitup-web is not the no-op replacement (ExecStart=/bin/true not found)" >&2
  exit 1
fi
echo "    ok: comitup-web unit is the no-op replacement"

if ! ssh_run "sudo grep -q pmf /usr/share/comitup/comitup/nm.py"; then
  echo "    FAIL: nm.py does not contain the pmf line — the WPA2/PMF hotspot patch did not apply" >&2
  exit 1
fi
echo "    ok: nm.py contains the pmf line"

echo "==> [9/9] identity"
if [ -n "$identity_path" ]; then
  scp -o BatchMode=yes "$identity_path" "${PI_HOST}:/tmp/palmimo-identity.json"
  ssh_run "sudo mv /tmp/palmimo-identity.json /boot/firmware/palmimo-identity.json && sudo chmod 0600 /boot/firmware/palmimo-identity.json"
  ssh_run "sudo systemctl start palmimo-firstboot"
  echo "    identity applied, palmimo-firstboot started"
else
  echo "    --identity not given: skipped (device stays a DIY/unindividualized 'palmimo')"
fi

echo "==> done"
