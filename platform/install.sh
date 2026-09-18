#!/usr/bin/env bash
# Platform bundle installer (palmimo-image, platform v1). Runs as root.
#
# install() and verify() both read manifest.json's "owns" section (via
# manifest_tool.py) rather than hardcoding paths, so the two can never
# drift apart from each other or from what the pi-gen/apply-pi contract
# test asserts is enumerated.
#
# Idempotent and declarative: install() converges --root to this bundle's
# desired state regardless of what version (or lack of one) was there
# before. There is no per-version migration path -- see
# doc/design/palmimo-app-platform.md chapter 2.8 (palmimo-devkit monorepo)
# for why.
#
# Account creation (useradd/groupadd/usermod) cannot run against a real
# account database in tests without root. PALMIMO_FAKE_ACCOUNTS=<file>
# redirects those three calls to append-only lines in that file instead, so
# tests can assert intent without mutating any account database.
#
# On a live system (--root / with systemd running), install also SIGUSR1s
# systemd-journald to flush its journal to the persistent storage
# journald.conf.d/palmimo.conf just enabled -- journald only re-reads
# Storage=persistent on its own restart, so without this a device updated
# via the Portal keeps a volatile journal until its next reboot.
#
# verify's exit code distinguishes "ran and found drift" (1) from "could
# not run" (2+, e.g. a bad manifest path or a crashed subprocess) -- the
# Portal's platform-ready check depends on that distinction, not just
# "zero or nonzero".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${SCRIPT_DIR}/manifest.json"
FILES_DIR="${SCRIPT_DIR}/files"
MANIFEST_TOOL="${SCRIPT_DIR}/manifest_tool.py"
VERIFY_TOOL="${SCRIPT_DIR}/verify_platform.py"

usage() {
  cat >&2 <<'EOF'
Usage:
  install.sh install [--root DIR]
  install.sh verify  [--root DIR]
  install.sh record  --root DIR --sha SHA256
EOF
  exit 2
}

log() {
  echo "install.sh: $*"
}

# --- account helpers ---------------------------------------------------------

record_account_intent() {
  local line="$1"
  grep -qxF "$line" "${PALMIMO_FAKE_ACCOUNTS}" 2>/dev/null && return 0
  echo "$line" >>"${PALMIMO_FAKE_ACCOUNTS}"
}

ensure_group() {
  local name="$1"
  if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
    record_account_intent "groupadd --system ${name}"
    return 0
  fi
  grep -q "^${name}:" "${ROOT%/}/etc/group" 2>/dev/null && return 0
  groupadd --system -R "${ROOT}" "${name}"
}

ensure_user() {
  local name="$1" supplementary_groups="$2"
  if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
    record_account_intent \
      "useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --gid palmimo-apps --groups ${supplementary_groups} ${name}"
    return 0
  fi
  grep -q "^${name}:" "${ROOT%/}/etc/passwd" 2>/dev/null && return 0
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
    --gid palmimo-apps --groups "${supplementary_groups}" -R "${ROOT}" "${name}"
}

add_user_to_group() {
  local user="$1" group="$2"
  # Never consult or mutate the host's account DB: with --root pointing at
  # a pi-gen rootfs, `id`/`usermod` without -R either fail (no such host
  # user) or -- worse -- silently succeed against the host instead of the
  # target tree. Membership is read straight from the target's own
  # /etc/group line instead of `id -nG`, in both fake and real mode, so a
  # device that already has this membership (from a previous install)
  # never re-records/re-runs usermod for it.
  local group_line
  group_line="$(grep "^${group}:" "${ROOT%/}/etc/group" 2>/dev/null || true)"
  case ",${group_line##*:}," in
    *",${user},"*) return 0 ;;
  esac
  if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
    record_account_intent "usermod -aG ${group} ${user}"
    return 0
  fi
  # "user" is the image's default account, provisioned by an earlier
  # pi-gen stage / already present on a real device -- a bare test root
  # legitimately has no such account yet, and that is not an error here.
  grep -q "^${user}:" "${ROOT%/}/etc/passwd" 2>/dev/null || return 0
  usermod -R "${ROOT}" -aG "${group}" "${user}"
}

# --- file installation -------------------------------------------------------

install_owned_files() {
  while IFS=$'\t' read -r path mode owner group; do
    [ -n "$path" ] || continue
    # mkdir -p first rather than relying on GNU install's -D: BSD install
    # (macOS, used for local dev/test runs) has no -D equivalent.
    mkdir -p "$(dirname "${ROOT%/}/${path}")"
    # A bare test root has no accounts for -o/-g to resolve; fake-accounts
    # mode asserts ownership intent elsewhere instead (see ensure_user).
    if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
      install -m "${mode}" "${FILES_DIR}/${path}" "${ROOT%/}/${path}"
    else
      install -m "${mode}" -o "${owner}" -g "${group}" "${FILES_DIR}/${path}" "${ROOT%/}/${path}"
    fi
  done < <(python3 "${MANIFEST_TOOL}" "${MANIFEST}" files)
}

install_managed_directories() {
  while IFS=$'\t' read -r path mode owner group; do
    [ -n "$path" ] || continue
    mkdir -p "${ROOT%/}/${path}"
    # This directory is exclusively ours (nothing else ships into
    # usr/lib/palmimo), so a full rsync --delete is safe here -- it is
    # exactly what converges a stray leftover file from a retired
    # v(N-1) layout to this bundle's files/ tree. -rlptD (no -o -g) instead
    # of -a: as root, -a would preserve the staged tree's owner (whoever
    # extracted the bundle), not the manifest's.
    rsync -rlptD --delete "${FILES_DIR}/${path}/" "${ROOT%/}/${path}/"
    if [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
      chown -R "${owner}:${group}" "${ROOT%/}/${path}"
    fi
    chmod "${mode}" "${ROOT%/}/${path}"
  done < <(python3 "${MANIFEST_TOOL}" "${MANIFEST}" managed-directories)
}

install_state_directories() {
  while IFS=$'\t' read -r path mode owner group; do
    [ -n "$path" ] || continue
    mkdir -p "${ROOT%/}/${path}"
    # A bare test root has no "user"/"palmimo-apps" accounts to chown to;
    # PALMIMO_FAKE_ACCOUNTS mode asserts ownership intent instead of the
    # filesystem, so skip chown/chgrp on a name that would not resolve.
    if [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
      chown "${owner}:${group}" "${ROOT%/}/${path}"
    fi
    chmod "${mode}" "${ROOT%/}/${path}"
  done < <(python3 "${MANIFEST_TOOL}" "${MANIFEST}" state-directories)
}

install_bundle_cache() {
  # The Portal later runs `install.sh verify` from this cached copy (not
  # from the live platform/ checkout, which only exists at build/apply
  # time) -- see manifest owns.bundle_cache. Built in a .tmp sibling and
  # swapped in so a concurrent/interrupted verify never sees a half-copied
  # tree.
  local dest="${ROOT%/}/var/lib/palmimo/platform/current"
  local tmp="${ROOT%/}/var/lib/palmimo/platform/current.tmp"
  rm -rf "$tmp"
  mkdir -p "$tmp"
  cp -a "${SCRIPT_DIR}/manifest.json" "${SCRIPT_DIR}/install.sh" "${SCRIPT_DIR}/manifest_tool.py" \
    "${SCRIPT_DIR}/verify_platform.py" "$tmp/"
  cp -a "${FILES_DIR}" "$tmp/files"
  find "$tmp" -type d -exec chmod 0755 {} +
  find "$tmp" -type f -exec chmod 0644 {} +
  chmod 0755 "$tmp/install.sh"
  if [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ] && grep -q "^user:" "${ROOT%/}/etc/passwd" 2>/dev/null; then
    chown -R user:user "$tmp"
  fi
  if [ -d "$dest" ]; then
    rm -rf "${dest}.old"
    mv "$dest" "${dest}.old"
  fi
  mv "$tmp" "$dest"
  rm -rf "${dest}.old"
}

remove_retired_paths() {
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    rm -rf "${ROOT%/}/${path:?}"
  done < <(python3 "${MANIFEST_TOOL}" "${MANIFEST}" retired)
}

install_uv() {
  local dest="${ROOT%/}/usr/local/bin/uv"
  [ -x "$dest" ] && return 0
  mkdir -p "$(dirname "$dest")"
  if [ -n "${PALMIMO_UV_SOURCE:-}" ]; then
    install -m 0755 "${PALMIMO_UV_SOURCE}" "$dest"
    return 0
  fi
  local home_uv="${ROOT%/}/home/user/.local/bin/uv"
  if [ -x "$home_uv" ]; then
    install -m 0755 "$home_uv" "$dest"
    return 0
  fi
  # Same download method as pigen/stage-palmimo/03-portal/00-run.sh's uv
  # bootstrap -- see that script for why (astral.sh installer).
  local tmp
  tmp="$(mktemp -d)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$tmp" sh
  install -m 0755 "$tmp/uv" "$dest"
  rm -rf "$tmp"
}

# --- subcommands --------------------------------------------------------------

do_install() {
  ensure_group palmimo-apps
  ensure_user palmimo-app "video,audio,dialout,palmimo-apps"
  add_user_to_group user palmimo-apps

  install_owned_files
  install_managed_directories
  install_state_directories
  mkdir -p "${ROOT%/}/var/log/journal"
  install_uv
  install_bundle_cache
  remove_retired_paths

  # /run/systemd/system only exists under a running systemd instance -- a
  # chroot (pi-gen's ROOTFS_DIR, or `install --root /` run inside a chroot
  # that happens to bind-mount host / as its root) has none, and calling
  # daemon-reload there fails and aborts the rest of install under set -e.
  if [ "${ROOT}" = "/" ] && [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    systemd-tmpfiles --create
    # journald only re-reads Storage=persistent on its own restart/reload;
    # SIGUSR1 makes it flush its current (volatile) journal to
    # /var/log/journal immediately instead. Without this, a device updated
    # via the Portal keeps a volatile journal until its next reboot, and
    # the first app crash after the update leaves no log.
    systemctl kill --signal=SIGUSR1 systemd-journald
  else
    log "skipped systemctl daemon-reload / systemd-tmpfiles --create / journald flush (--root != / or no running systemd)"
  fi
}

do_verify() {
  python3 "${VERIFY_TOOL}" "${MANIFEST}" "${FILES_DIR}" "${ROOT}"
}

do_record() {
  local sha=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --root) ROOT="$2"; shift 2 ;;
      --sha) sha="$2"; shift 2 ;;
      *) usage ;;
    esac
  done
  [ -n "$sha" ] || usage
  local version
  version="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" version)"
  local platform_dir="${ROOT%/}/var/lib/palmimo/platform"
  mkdir -p "$platform_dir"
  # Written via a same-directory temp file + os.replace so a device losing
  # power or the Portal killing this process mid-write never leaves
  # installed.json truncated or half-written -- the Portal reads this file
  # to decide what version is on disk.
  python3 - "$platform_dir/installed.json" "$version" "$sha" <<'PYEOF'
import datetime
import json
import os
import sys
import tempfile

out_path, version, sha = sys.argv[1], int(sys.argv[2]), sys.argv[3]
data = {
    "version": version,
    "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "bundle_sha256": sha,
}
fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(out_path), prefix=".installed.json.")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, out_path)
except BaseException:
    os.unlink(tmp_path)
    raise
PYEOF
  # var/lib/palmimo/platform is user:user (state_directories above), but
  # this file itself is written as root -- the Portal (running as user)
  # later rewrites it after applying a bundle and needs to be able to
  # replace it, not just read it.
  if [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ] && grep -q "^user:" "${ROOT%/}/etc/passwd" 2>/dev/null; then
    chown user:user "$platform_dir/installed.json"
  fi
}

# --- entry point ---------------------------------------------------------------

[ $# -ge 1 ] || usage
command="$1"; shift
ROOT="/"

case "$command" in
  install|verify)
    while [ $# -gt 0 ]; do
      case "$1" in
        --root) ROOT="$2"; shift 2 ;;
        *) usage ;;
      esac
    done
    if [ "$command" = install ]; then
      do_install
    else
      do_verify
    fi
    ;;
  record)
    do_record "$@"
    ;;
  *)
    usage
    ;;
esac
