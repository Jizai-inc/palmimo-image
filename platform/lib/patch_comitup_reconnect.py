#!/usr/bin/env python3
"""Patch comitup's mdns.py, nm.py and states.py so a lost Wi-Fi network falls back to
the setup AP, and saved networks are tried in priority order when visible or hidden.

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

Fixes are appended (above `__main__` where present) as wrappers that rebind the
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
from typing import NoReturn


COMITUP_DIR = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/comitup/comitup"
TODAY = datetime.date.today().isoformat()

MDNS_MARKER = "keep a stale avahi entry group from blocking the AP fallback"
NM_MARKER = "try candidates in autoconnect-priority order"
LEGACY_SCAN_MARKER = "limit candidates to freshly visible or hidden networks"
SCAN_MARKER = "preserve active candidates and cache scan outcomes for 60 seconds"
LEGACY_STATES_MARKER = "ignore residual disconnects during the single-device startup hotspot"
STATES_MARKER = "ignore residual disconnects through the single-device startup hotspot pass"

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


SCAN_APPENDIX = f"""

# Modified by Jizai Inc. on {TODAY}: {SCAN_MARKER}
# (GPLv2 section 2(a) notice). This device has one Wi-Fi interface. Both
# successful and failed scans have a 60-second lifetime, covering the
# supplicant's 25-second connection attempts without repeated GLib waits.
import time as _palmimo_time

_palmimo_ordered_candidates = get_candidate_connections
_palmimo_scan_completed = None
_palmimo_visible_ssids = None
_palmimo_scan_reason = "not scanned"
_palmimo_empty_rechecked = False


def _palmimo_scan(device):
    global _palmimo_scan_completed, _palmimo_visible_ssids, _palmimo_scan_reason

    _palmimo_visible_ssids = None
    try:
        wireless = device.SpecificDevice()
        previous_scan = wireless.LastScan
        deadline = _palmimo_time.monotonic() + 8
        wireless.RequestScan({{}})
        while wireless.LastScan == previous_scan:
            remaining = deadline - _palmimo_time.monotonic()
            if remaining <= 0:
                _palmimo_scan_reason = "scan timed out"
                return
            _palmimo_time.sleep(min(0.1, remaining))

        visible = set()
        for ap in wireless.GetAllAccessPoints():
            try:
                seen = ap.LastSeen
                # LastSeen has only second precision. Keep the boundary
                # second: an AP seen after LastScan may round down to it.
                if seen >= 0 and (previous_scan < 0 or seen >= previous_scan // 1000):
                    visible.add(ap.Ssid)
            except (nm.ObjectVanished, dbus.exceptions.DBusException):
                continue
        if visible:
            _palmimo_visible_ssids = visible
        else:
            _palmimo_scan_reason = "no fresh APs"
    except Exception as error:
        _palmimo_scan_reason = type(error).__name__
    finally:
        _palmimo_scan_completed = _palmimo_time.monotonic()


def get_candidate_connections(device: nm.Device) -> List[str]:
    global _palmimo_scan_completed, _palmimo_visible_ssids, _palmimo_scan_reason
    global _palmimo_empty_rechecked

    candidates = _palmimo_ordered_candidates(device)
    try:
        if (
            _palmimo_scan_completed is None
            or _palmimo_time.monotonic() - _palmimo_scan_completed >= 60
        ):
            _palmimo_scan(device)
            _palmimo_empty_rechecked = False

        if _palmimo_visible_ssids is None:
            log.info("candidate scan: unfiltered (%s)", _palmimo_scan_reason)
            return candidates

        active = get_active_ssid(device)
        hidden = set()
        for conn in get_all_connections():
            try:
                settings = conn.GetSettings()
                wifi = settings.get("802-11-wireless", {{}})
                if wifi.get("mode") == "infrastructure" and wifi.get("hidden"):
                    ssid = get_ssid_from_connection(conn)
                    if ssid is not None:
                        hidden.add(ssid)
            except (nm.ObjectVanished, dbus.exceptions.DBusException):
                continue

        while True:
            if _palmimo_visible_ssids is None:
                log.info("candidate scan: unfiltered (%s)", _palmimo_scan_reason)
                return candidates
            filtered = [
                ssid for ssid in candidates
                if ssid in _palmimo_visible_ssids or ssid in hidden or ssid == active
            ]
            if filtered or not candidates or _palmimo_empty_rechecked:
                log.info("candidate scan: filtered %s to %s", candidates, filtered)
                return filtered
            # Once per cached outcome, confirm an empty result before
            # keeping the AP. Do not rescan on each subsequent callback.
            _palmimo_empty_rechecked = True
            _palmimo_scan(device)
    except Exception as error:
        # Uncertainty must not suppress a reachable saved network until
        # the next 180/360-second retry, or block every callback again.
        _palmimo_visible_ssids = None
        _palmimo_scan_reason = type(error).__name__
        _palmimo_scan_completed = _palmimo_time.monotonic()
        log.warning("candidate scan: unfiltered (%s)", _palmimo_scan_reason)
        return candidates
"""

STATES_ANCHORS = (
    "@timeout\n@state_callback\ndef hotspot_fail(reason: int) -> None:\n",
    "@timeout\n@state_callback\ndef hotspot_pass(reason: int) -> None:\n",
    "class state_matrix",
)

STATES_APPENDIX = f"""

# Modified by Jizai Inc. on {TODAY}: {STATES_MARKER}
# (GPLv2 section 2(a) notice). A single-device startup HOTSPOT has no AP
# activation to fail. Its residual disconnects remain harmless through
# hotspot_pass until state_id advances, regardless of GLib callback order.
_palmimo_hotspot_fail = hotspot_fail
_palmimo_hotspot_pass = hotspot_pass
_palmimo_startup_pass_id = None


def hotspot_pass(id: int, reason: int) -> bool:
    global _palmimo_startup_pass_id

    if id == state_id and startup and modemgr.get_mode() == modemgr.SINGLE_MODE:
        _palmimo_startup_pass_id = id
    return _palmimo_hotspot_pass(id, reason)


def hotspot_fail(id: int, reason: int) -> bool:
    if id != state_id:
        return False
    if modemgr.get_mode() == modemgr.SINGLE_MODE and (startup or id == _palmimo_startup_pass_id):
        log.warning("Ignoring residual disconnect in startup hotspot")
        return True
    return _palmimo_hotspot_fail(id, reason)
"""


def fail(path: str, anchor: str) -> NoReturn:
    sys.stderr.write(
        f"    FAIL: expected line {anchor.strip()!r} not found in {path}\n"
        "    This patch targets comitup 1.43; a comitup upgrade likely changed "
        "the file. Re-derive the patch against the installed comitup version "
        "before re-running -- do not ship an AP fallback that cannot come up.\n"
    )
    sys.exit(1)


def patch(
    name: str,
    marker: str,
    anchors: tuple[str, ...],
    appendix: str,
    legacy: tuple[str, str] | None = None,
) -> None:
    path = os.path.join(COMITUP_DIR, name)
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if marker in text:
        print(f"    already patched: {name}")
        return
    for anchor in anchors:
        if text.count(anchor) != 1:
            fail(path, anchor)
    if legacy is not None and legacy[0] in text:
        # The previous scan/state appendix is the final block before the
        # CLI guard (or EOF). Refuse unfamiliar tails instead of deleting
        # custom code or stacking a scanner on top of the old scanner.
        head, guard, tail = text.rpartition(MAIN_GUARD)
        if not guard:
            head, tail = text, ""
        notice = next(
            (
                line
                for line in head.splitlines(keepends=True)
                if line.startswith("# Modified by Jizai Inc. on ") and line.rstrip().endswith(": " + legacy[0])
            ),
            None,
        )
        if notice is None or not head.rstrip().endswith(legacy[1]):
            fail(path, legacy[1])
        head = head[: head.index(notice)]
        text = head + guard + tail
    with open(path, "w", encoding="utf-8", newline="") as f:
        # Above the `__main__` block, so running the module as a CLI gets
        # the wrappers too.
        head, guard, tail = text.rpartition(MAIN_GUARD)
        if not guard:
            head, tail = text, ""
        f.write(head.rstrip("\n") + "\n" + appendix + "\n" + guard + tail)
    print(f"    patched: {name}")


def main() -> None:
    patch("mdns.py", MDNS_MARKER, MDNS_ANCHORS, MDNS_APPENDIX)
    patch("nm.py", NM_MARKER, NM_ANCHORS, NM_APPENDIX)
    patch(
        "nm.py",
        SCAN_MARKER,
        ("_comitup_get_candidate_connections = get_candidate_connections\n", MAIN_GUARD),
        SCAN_APPENDIX,
        (LEGACY_SCAN_MARKER, "return candidates"),
    )
    patch(
        "states.py",
        STATES_MARKER,
        STATES_ANCHORS,
        STATES_APPENDIX,
        (LEGACY_STATES_MARKER, "return _palmimo_hotspot_fail(id, reason)"),
    )


if __name__ == "__main__":
    main()
