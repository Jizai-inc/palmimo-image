#!/usr/bin/env python3
"""Patch comitup's mdns.py and nm.py so a lost Wi-Fi network falls back to
the setup AP, and saved networks are tried in priority order.

Shared between apply-pi.sh (piped over SSH as `sudo python3 -`) and the
pi-gen custom stage (copied into the chroot), like patch_comitup_nm.py.
See doc/design.md ("comitup 設定") for the hardware findings.

1. mdns.py -- comitup caches one avahi EntryGroup proxy for its lifetime.
   files/etc/NetworkManager/dispatcher.d/50-palmimo-avahi restarts avahi
   on every STA up (#683), which leaves that proxy pointing at a D-Bus name
   that no longer exists. The next hotspot_start() calls clear_entries()
   before activating the AP, gets DBusException(ServiceUnknown), and the
   GLib callback dies there: the AP never comes up until a reboot.
2. nm.py -- get_candidate_connections() returns saved networks in
   ListConnections order and ignores connection.autoconnect-priority.

Both fixes are inserted just above the `__main__` block as wrappers that rebind the
module-level names, so comitup's own function bodies stay untouched and
this script quotes nothing of them beyond the signature lines it checks
for. Callers resolve these names at call time (states.py through the module
attribute, mdns.py's own calls through its globals), so they get the
wrappers.

Distributing the modified files (GPL-2.0) triggers GPLv2 section 2(a): each
appended block carries a dated "Modified by Jizai Inc." notice, which is
also that file's idempotency marker.

Fails loud (nonzero exit naming the comitup version this targets) if a
signature line is missing, so a comitup upgrade cannot silently ship an AP
fallback that cannot come up.
"""

import datetime
import os
import sys


COMITUP_DIR = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/comitup/comitup"
TODAY = datetime.date.today().isoformat()

MDNS_MARKER = "keep a stale avahi entry group from blocking the AP fallback"
NM_MARKER = "try candidates in autoconnect-priority order"

MAIN_GUARD = '\nif __name__ == "__main__":\n'
MDNS_ANCHORS = (
    "def establish_group() -> None:\n",
    "def clear_entries(emphatic=False) -> None:\n",
    MAIN_GUARD,
)
NM_ANCHORS = (
    "def get_candidate_connections(device: nm.Device) -> List[str]:\n",
    MAIN_GUARD,
)

MDNS_APPENDIX = f"""

# Modified by Jizai Inc. on {TODAY}: {MDNS_MARKER}
# (GPLv2 section 2(a) notice). avahi restarts on every STA up, which leaves
# the cached entry group pointing at a D-Bus name that is gone; drop it and
# build a new one, and if avahi is down skip mDNS rather than raise.
_comitup_establish_group = establish_group
_comitup_clear_entries = clear_entries


def establish_group() -> None:
    global group

    try:
        _comitup_establish_group()
    except dbus.exceptions.DBusException as e:
        log.warning("avahi unavailable, skipping mDNS: {{}}".format(e))
        group = None


def clear_entries(emphatic=False) -> None:
    global group

    try:
        _comitup_clear_entries(emphatic)
    except dbus.exceptions.DBusException as e:
        log.warning("avahi entry group is stale, re-establishing: {{}}".format(e))
        group = None
        establish_group()
"""

NM_APPENDIX = f"""

# Modified by Jizai Inc. on {TODAY}: {NM_MARKER}
# (GPLv2 section 2(a) notice). Highest connection.autoconnect-priority
# first; ties keep comitup's ListConnections order.
_comitup_get_candidate_connections = get_candidate_connections


def get_candidate_connections(device: nm.Device) -> List[str]:
    priorities: Dict[str, int] = {{}}
    for conn in get_all_connections():
        ssid = get_ssid_from_connection(conn)
        # Uncached, unlike get_connection_settings(): a priority changed while
        # comitup runs (the Portal reorders saved networks) must count now.
        # A profile deleted since ListConnections must not raise out of a
        # GLib callback (that stops comitup's retries).
        try:
            settings = conn.GetSettings()
        except (nm.ObjectVanished, dbus.exceptions.DBusException):
            continue
        if (
            ssid
            and ssid not in priorities
            and settings.get("802-11-wireless", {{}}).get("mode") == "infrastructure"
        ):
            priorities[ssid] = int(settings["connection"].get("autoconnect-priority", 0))

    candidates = _comitup_get_candidate_connections(device)
    ordered = sorted(candidates, key=lambda ssid: -priorities.get(ssid, 0))
    log.debug("candidates by priority: %s" % ordered)
    return ordered
"""


def fail(path: str, anchor: str) -> None:
    sys.stderr.write(
        f"    FAIL: expected line {anchor.strip()!r} not found in {path}\n"
        "    This patch targets comitup 1.43; a comitup upgrade likely changed "
        "the file. Re-derive the patch against the installed comitup version "
        "before re-running -- do not ship an AP fallback that cannot come up.\n"
    )
    sys.exit(1)


def patch(name: str, marker: str, anchors: tuple[str, ...], appendix: str) -> None:
    path = os.path.join(COMITUP_DIR, name)
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if marker in text:
        print(f"    already patched: {name}")
        return
    for anchor in anchors:
        if text.count(anchor) != 1:
            fail(path, anchor)
    with open(path, "w", encoding="utf-8", newline="") as f:
        # Above the `__main__` block, so running the module as a CLI gets
        # the wrappers too.
        head, guard, tail = text.rpartition(MAIN_GUARD)
        f.write(head.rstrip("\n") + "\n" + appendix + "\n" + guard + tail)
    print(f"    patched: {name}")


def main() -> None:
    patch("mdns.py", MDNS_MARKER, MDNS_ANCHORS, MDNS_APPENDIX)
    patch("nm.py", NM_MARKER, NM_ANCHORS, NM_APPENDIX)


if __name__ == "__main__":
    main()
