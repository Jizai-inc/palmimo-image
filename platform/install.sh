#!/usr/bin/env bash
# Platform bundle installer (palmimo-image, platform v2). Runs as root.
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
  install.sh uv      [--root DIR]
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

_apt_root_run() {
  if [ "${ROOT}" = "/" ]; then
    "$@"
  else
    # An offline target root has no running init of its own -- apt-get
    # must run inside it.
    chroot "${ROOT%/}" "$@"
  fi
}

# --- apt packages -------------------------------------------------------

install_apt_packages() {
  # Apps run as palmimo-app with no permission to install system
  # libraries themselves (see doc/design/palmimo-app-platform.md); shared
  # libraries an app's own dependencies need (e.g. opencv-python's
  # libGL/libglib) can only reach a field device through this bundle.
  if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
    local rows
    rows="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" apt-packages)"
    while IFS= read -r pkg; do
      [ -n "$pkg" ] || continue
      record_account_intent "apt-get install ${pkg}"
    done <<< "$rows"
    return 0
  fi
  # Used only by the root-owned-target convergence check.  That check makes
  # a deliberately minimal target tree, where apt cannot run.
  [ -n "${PALMIMO_SKIP_APT:-}" ] && return 0

  local missing="" pkg status
  local rows
  rows="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" apt-packages)"
  while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    # shellcheck disable=SC2016  # dpkg-query's own format string, not shell expansion
    status="$(_apt_root_run dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)"
    case "$status" in
      *"install ok installed"*) ;;
      *) missing="${missing}${missing:+ }${pkg}" ;;
    esac
  done <<< "$rows"

  [ -n "$missing" ] || return 0

  # Devices flashed from the shipped image carry a package left unpacked
  # but unconfigured by the image build (comitup); apt refuses to run until
  # that is finished. Idempotent and quick when nothing is pending. The
  # image ships its own /etc/comitup.conf, so keep installed conffiles
  # instead of prompting (there is no terminal here).
  _apt_root_run env DEBIAN_FRONTEND=noninteractive dpkg --configure -a --force-confold
  _apt_root_run env DEBIAN_FRONTEND=noninteractive apt-get update
  # shellcheck disable=SC2086  # $missing is a controlled, space-joined package-name list
  _apt_root_run env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    -o Dpkg::Options::=--force-confold $missing
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
  # pi-gen stage / already present on a real device. Install tolerates a
  # bare test root without this account; verify requires owns.users to exist.
  grep -q "^${user}:" "${ROOT%/}/etc/passwd" 2>/dev/null || return 0
  usermod -R "${ROOT}" -aG "${group}" "${user}"
}

# --- root-owned path repair --------------------------------------------------

# Images built before commit 28fdabb (palmimo-image) shipped files/ via
# `rsync -a` from a macOS checkout, leaving these paths on every such
# device owned by the build host's uid instead of root. An unsafe owner
# there makes systemd-tmpfiles and NetworkManager's dispatcher refuse to
# run (see doc/design/palmimo-app-platform.md 2.8), so the platform bundle
# -- the only update vehicle those devices have -- repairs it here.
repair_root_owned_paths() {
  local rows
  rows="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" repair-root-owned)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    local target
    if [ "$path" = "." ]; then
      target="${ROOT%/}"
      [ -n "$target" ] || target="/"
    else
      target="${ROOT%/}/${path}"
    fi
    [ -e "$target" ] || continue
    if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
      record_account_intent "chown root:root ${target}"
      continue
    fi
    local owner_uid
    owner_uid="$(python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_uid)' "$target")"
    if [ "$owner_uid" != "0" ]; then
      chown root:root "$target"
      log "repaired ownership: ${path}"
    fi
    chmod g-w,o-w "$target"
  done <<< "$rows"
}

# --- file installation -------------------------------------------------------

target_account_id() {
  local database="$1" name="$2" field="$3"
  local value
  value="$(awk -F: -v name="$name" -v field="$field" '$1 == name { print $field; exit }' \
    "${ROOT%/}/etc/${database}" 2>/dev/null || true)"
  [ -n "$value" ] || {
    echo "install.sh: ${database} entry missing for ${name} under ${ROOT}" >&2
    return 1
  }
  printf '%s\n' "$value"
}

target_uid() {
  if [ "$ROOT" = "/" ]; then
    printf '%s\n' "$1"
  else
    target_account_id passwd "$1" 3
  fi
}

target_gid() {
  if [ "$ROOT" = "/" ]; then
    printf '%s\n' "$1"
  else
    target_account_id group "$1" 3
  fi
}

# comitup ships in the image, not in this bundle; its fixes are patches to
# the apt package's own files (see lib/), so they ride every platform
# update to reach devices already in the field. Both patches are
# idempotent, and the nm.py one must run first (its notice marker would
# otherwise read the reconnect patch's notice as its own). comitup is
# restarted only when a patch actually changed a file -- a restart while
# CONNECTED keeps the connection (verified on hardware).
patch_comitup() {
  local dir="${ROOT%/}/usr/share/comitup/comitup"
  if [ ! -f "${dir}/nm.py" ]; then
    log "comitup not installed under ${ROOT}; skipped its patches"
    return 0
  fi
  local out
  out="$(python3 "${SCRIPT_DIR}/lib/patch_comitup_nm.py" "${dir}/nm.py")"
  out+=$'
'"$(python3 "${SCRIPT_DIR}/lib/patch_comitup_reconnect.py" "${dir}")"
  log "${out}"
  if printf '%s
' "${out}" | grep -qE '^ +(patched|upgraded):'; then
    if [ "${ROOT}" = "/" ] && [ -d /run/systemd/system ]; then
      systemctl try-restart comitup.service
    fi
  fi
}

install_owned_files() {
  local rows
  rows="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" files)"
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
      local uid gid
      # Command substitution failure inside an argument list does not trip
      # set -e, so target_uid/target_gid's error would otherwise be
      # swallowed and `install -o "" -g ""` would run against an empty
      # owner/group (a silent no-op on some install(1) implementations).
      uid="$(target_uid "$owner")" || exit 1
      gid="$(target_gid "$group")" || exit 1
      install -m "${mode}" -o "$uid" -g "$gid" \
        "${FILES_DIR}/${path}" "${ROOT%/}/${path}"
    fi
  done <<< "$rows"
}

install_managed_directories() {
  local rows
  rows="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" managed-directories)"
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
      local uid gid
      uid="$(target_uid "$owner")" || exit 1
      gid="$(target_gid "$group")" || exit 1
      chown -R "${uid}:${gid}" "${ROOT%/}/${path}"
    fi
    chmod "${mode}" "${ROOT%/}/${path}"
  done <<< "$rows"
}

install_state_directories() {
  local rows
  rows="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" state-directories)"
  while IFS=$'\t' read -r path mode owner group; do
    [ -n "$path" ] || continue
    mkdir -p "${ROOT%/}/${path}"
    # A bare test root has no "user"/"palmimo-apps" accounts to chown to;
    # PALMIMO_FAKE_ACCOUNTS mode asserts ownership intent instead of the
    # filesystem, so skip chown/chgrp on a name that would not resolve.
    if [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
      local uid gid
      uid="$(target_uid "$owner")" || exit 1
      gid="$(target_gid "$group")" || exit 1
      chown "${uid}:${gid}" "${ROOT%/}/${path}"
    fi
    chmod "${mode}" "${ROOT%/}/${path}"
  done <<< "$rows"
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
  if [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ] && grep -q "^user:" "${ROOT%/}/etc/passwd" 2>/dev/null; then
    local uid gid
    uid="$(target_uid user)" || exit 1
    gid="$(target_gid user)" || exit 1
    chown -R "${uid}:${gid}" "$tmp"
  fi
  if [ -d "$dest" ]; then
    rm -rf "${dest}.old"
    mv "$dest" "${dest}.old"
  fi
  mv "$tmp" "$dest"
  rm -rf "${dest}.old"
}

remove_retired_paths() {
  local rows
  rows="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" retired)"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    rm -rf "${ROOT%/}/${path:?}"
  done <<< "$rows"
}

uv_version() {
  local output
  # Only a live root has a user who could replace this binary before root runs
  # it; under an offline --root, "user" would resolve in the host's accounts.
  if [ "${2:-}" = user ] && [ "$ROOT" = / ] && [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
    output="$(runuser -u user -- "$1" --version 2>/dev/null)" || return 0
  else
    output="$("$1" --version 2>/dev/null)" || return 0
  fi
  [[ "$output" =~ ^uv\ ([0-9]+\.[0-9]+\.[0-9]+)(\ |$) ]] || return 0
  printf '%s\n' "${BASH_REMATCH[1]}"
}

install_uv_binary() {
  local source="$1" dest="$2" mode="$3" owner="$4" group="$5"
  if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
    mkdir -p "$(dirname "$dest")"
    install -m "$mode" "$source" "$dest"
  else
    local uid gid
    uid="$(target_uid "$owner")"
    gid="$(target_gid "$group")"
    if [ "$dest" = "${ROOT%/}/home/user/.local/bin/uv" ]; then
      local directory
      for directory in "${ROOT%/}/home/user/.local" "${ROOT%/}/home/user/.local/bin"; do
        [ -d "$directory" ] || install -d -m 0755 -o "$uid" -g "$gid" "$directory"
      done
    else
      mkdir -p "$(dirname "$dest")"
    fi
    install -m "$mode" -o "$uid" -g "$gid" "$source" "$dest"
  fi
}

install_uv() (
  local row path mode owner group version url sha
  row="$(python3 "${MANIFEST_TOOL}" "${MANIFEST}" uv)"
  IFS=$'\t' read -r path mode owner group version url sha <<< "$row"
  local dest="${ROOT%/}/${path}" home_uv="${ROOT%/}/home/user/.local/bin/uv"
  local system_version home_version update_system=0 update_home=0
  system_version="$(uv_version "$dest")"
  home_version="$(uv_version "$home_uv" user)"
  [ "$system_version" = "$version" ] || update_system=1
  if [ -z "$home_version" ]; then
    update_home=1
  elif [ "$home_version" != "$version" ]; then
    local comparison=0
    dpkg --compare-versions "$home_version" lt "$version" || comparison=$?
    case "$comparison" in
      0) update_home=1 ;;
      1) ;;
      *) exit "$comparison" ;;
    esac
  fi

  local tmp source="${PALMIMO_UV_SOURCE:-}"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  if [ "$update_system" = 1 ] || [ "$update_home" = 1 ]; then
    if [ -z "$source" ]; then
      curl -fLsS "$url" -o "$tmp/uv.tar.gz"
      python3 - "$tmp/uv.tar.gz" "$sha" <<'PYEOF'
import hashlib
import sys
from pathlib import Path

if hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() != sys.argv[2]:
    sys.exit("uv archive sha256 mismatch")
PYEOF
      tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
      source="$tmp/uv-aarch64-unknown-linux-gnu/uv"
    fi
    [ "$(uv_version "$source")" = "$version" ] || {
      echo "install.sh: uv source does not match manifest version" >&2
      exit 1
    }
    [ "$update_system" = 0 ] || install_uv_binary "$source" "$dest" "$mode" "$owner" "$group"
    [ "$update_home" = 0 ] || install_uv_binary "$source" "$home_uv" 0755 user user
  fi
  chmod "$mode" "$dest"
  if [ -z "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
    local uid gid
    uid="$(target_uid "$owner")"
    gid="$(target_gid "$group")"
    chown "$uid:$gid" "$dest"
  fi
)

upgrade_uv_python() {
  [ "$ROOT" = / ] || [ -n "${PALMIMO_UV_PYTHON_DIRS:-}" ] || return 0
  local dir dirs uv_bin="${ROOT%/}/usr/local/bin/uv"
  dirs="${PALMIMO_UV_PYTHON_DIRS:-$(printf '%s\n' /var/lib/palmimo/uv-python /home/user/.local/share/uv/python)}"
  while IFS= read -r dir; do
    [ -d "$dir" ] || continue
    [ -n "$(find "$dir" -mindepth 2 -maxdepth 3 -path '*/bin/python*' -type f -print -quit)" ] || continue
    # Portal subsequently installs here as user; root-owned interpreters would
    # prevent it from maintaining the shared installation.
    # Without UV_PYTHON_INSTALL_BIN=0 the upgrade links pythonX.Y into
    # ~user/.local/bin; `python upgrade` (unlike `python install`) has no --no-bin.
    if [ -n "${PALMIMO_FAKE_ACCOUNTS:-}" ]; then
      env -u UV_PYTHON_DOWNLOADS HOME=/home/user UV_PYTHON_INSTALL_DIR="$dir" \
        UV_PYTHON_INSTALL_BIN=0 "$uv_bin" python upgrade --no-config
    else
      runuser -u user -- env -u UV_PYTHON_DOWNLOADS HOME=/home/user \
        UV_PYTHON_INSTALL_DIR="$dir" UV_PYTHON_INSTALL_BIN=0 "$uv_bin" python upgrade --no-config
    fi
  done <<< "$dirs"
}

# --- subcommands --------------------------------------------------------------

do_install() {
  ensure_group palmimo-apps
  ensure_user palmimo-app "video,audio,dialout,palmimo-apps"
  add_user_to_group user palmimo-apps
  install_apt_packages
  patch_comitup

  install_owned_files
  install_managed_directories
  install_state_directories
  mkdir -p "${ROOT%/}/var/log/journal"
  install_uv
  upgrade_uv_python
  install_bundle_cache
  remove_retired_paths
  repair_root_owned_paths

  # /run/systemd/system only exists under a running systemd instance -- a
  # chroot (pi-gen's ROOTFS_DIR, or `install --root /` run inside a chroot
  # that happens to bind-mount host / as its root) has none, and calling
  # daemon-reload there fails and aborts the rest of install under set -e.
  if [ "${ROOT}" = "/" ] && [ -d /run/systemd/system ]; then
    systemctl daemon-reload
    # Scoped to our own config: a global `--create` also processes every
    # other tmpfiles.d entry on the device, and one unrelated broken entry
    # there would abort this install under set -e.
    systemd-tmpfiles --create /etc/tmpfiles.d/palmimo.conf
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
    local uid gid
    uid="$(target_uid user)" || exit 1
    gid="$(target_gid user)" || exit 1
    chown "${uid}:${gid}" "$platform_dir/installed.json"
  fi
}

# --- entry point ---------------------------------------------------------------

[ $# -ge 1 ] || usage
command="$1"; shift
ROOT="/"

case "$command" in
  install|verify|uv)
    while [ $# -gt 0 ]; do
      case "$1" in
        --root) ROOT="$2"; shift 2 ;;
        *) usage ;;
      esac
    done
    if [ "$command" = install ]; then
      do_install
    elif [ "$command" = uv ]; then
      install_uv
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
