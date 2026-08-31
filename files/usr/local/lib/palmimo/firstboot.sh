#!/usr/bin/env bash
set -euo pipefail

IDENTITY_FILE="/boot/firmware/palmimo-identity.json"
DONE_MARKER="/var/lib/palmimo/firstboot-done"
COMITUP_CONF="/etc/comitup.conf"
DEVICE_ID_REGEX='^[a-z0-9-]{1,32}$'
PASSWORD_REGEX='^[A-Za-z0-9]{8,63}$'

log_error() {
  echo "firstboot: ERROR: $*" >&2
}

log_info() {
  echo "firstboot: $*"
}

mkdir -p "$(dirname "$DONE_MARKER")"

if [ ! -f "$IDENTITY_FILE" ]; then
  log_error "identity file not found: $IDENTITY_FILE"
  exit 1
fi

if ! IDENTITY_FIELDS="$(python3 - "$IDENTITY_FILE" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except (OSError, json.JSONDecodeError) as exc:
    print(f"failed to parse {path}: {exc}", file=sys.stderr)
    sys.exit(1)

if not isinstance(data, dict):
    print("identity file is not a JSON object", file=sys.stderr)
    sys.exit(1)

device_id = data.get("device_id")
initial_password = data.get("initial_password")

if not isinstance(device_id, str) or not device_id:
    print("missing or invalid device_id", file=sys.stderr)
    sys.exit(1)
if not isinstance(initial_password, str) or not initial_password:
    print("missing or invalid initial_password", file=sys.stderr)
    sys.exit(1)
if "\t" in device_id or "\t" in initial_password:
    print("device_id/initial_password must not contain a tab", file=sys.stderr)
    sys.exit(1)

print(f"{device_id}\t{initial_password}")
PYEOF
)"; then
  log_error "identity file is malformed or missing device_id/initial_password: $IDENTITY_FILE"
  exit 1
fi

DEVICE_ID="${IDENTITY_FIELDS%%$'\t'*}"
INITIAL_PASSWORD="${IDENTITY_FIELDS#*$'\t'}"

if ! [[ "$DEVICE_ID" =~ $DEVICE_ID_REGEX ]]; then
  log_error "device_id fails validation ($DEVICE_ID_REGEX): '$DEVICE_ID'"
  exit 1
fi

if ! [[ "$INITIAL_PASSWORD" =~ $PASSWORD_REGEX ]]; then
  log_error "initial_password fails validation ($PASSWORD_REGEX) — manufacturing produced an out-of-alphabet value"
  exit 1
fi

HOSTNAME="palmimo-${DEVICE_ID}"
log_info "setting hostname to $HOSTNAME"
printf '%s\n' "$HOSTNAME" >/etc/hostname
hostnamectl set-hostname --transient "$HOSTNAME"

if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts 2>/dev/null; then
  sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t${HOSTNAME}/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$HOSTNAME" >>/etc/hosts
fi

log_info "setting comitup ap_password"
if [ ! -f "$COMITUP_CONF" ]; then
  log_error "comitup config not found: $COMITUP_CONF"
  exit 1
fi

tmp_conf="$(mktemp "${COMITUP_CONF}.XXXXXX")"
grep -v '^ap_password:' "$COMITUP_CONF" >"$tmp_conf" || true
printf 'ap_password: %s\n' "$INITIAL_PASSWORD" >>"$tmp_conf"
mv "$tmp_conf" "$COMITUP_CONF"
chmod 0600 "$COMITUP_CONF"
chown root:root "$COMITUP_CONF"

touch "$DONE_MARKER"
log_info "done: hostname=$HOSTNAME"
