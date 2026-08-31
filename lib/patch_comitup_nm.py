#!/usr/bin/env python3
"""Patch comitup's nm.py to pin WPA2/CCMP and disable PMF on the hotspot AP.

Shared between apply-pi.sh (piped over SSH as `sudo python3 -`) and the
pi-gen custom stage (copied into the chroot and run as
`python3 /tmp/patch_comitup_nm.py`), so the two consumers can never diverge
on this patch. See doc/design.md ("comitup 設定") for why the
patch exists: comitup 1.43's make_hotspot() in
/usr/share/comitup/comitup/nm.py sets only key-mgmt/psk on the hotspot
802-11-wireless-security settings, leaving NetworkManager free to negotiate
WPA1/TKIP with PMF unset -- modern Apple clients then fail the handshake
against the setup AP.

Distributing this modified nm.py (GPL-2.0) on the SD image triggers GPLv2
section 2(a): the modified file must carry a prominent notice that it was
changed, with the date. This patch inserts that notice as a comment right
alongside the proto/pairwise/group/pmf lines, and the notice marker
(NOTICE_MARKER) -- not the mere presence of "pmf" -- is the idempotency key.

Idempotent: exits 0 immediately if NOTICE_MARKER is already present in the
file. A file patched by a previous version of this script (proto/pairwise/
group/pmf present, but no notice) is upgraded in place: the notice is added
without duplicating the four keys.

Fails loud (nonzero exit + a message naming the comitup version this patch
targets) if the expected anchor lines are missing, so a comitup upgrade that
changes nm.py's structure cannot silently ship an unpatched (broken) hotspot.
"""

import datetime
import sys


NOTICE_MARKER = "Modified by Jizai Inc."

path = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/comitup/comitup/nm.py"
with open(path, encoding="utf-8") as f:
    text = f.read()

if NOTICE_MARKER in text:
    print("    already patched: Jizai modification notice already present in nm.py")
    sys.exit(0)

anchor = (
    '        settings["802-11-wireless-security"]["key-mgmt"] = "wpa-psk"\n'
    '        settings["802-11-wireless-security"]["psk"] = password\n'
)

today = datetime.date.today().isoformat()
notice = (
    "        # Modified by Jizai Inc. on " + today + ": pin WPA2/CCMP and disable PMF\n"
    "        # for the Palmimo setup hotspot (GPLv2 section 2(a) notice).\n"
)

patched_lines = (
    '        settings["802-11-wireless-security"]["proto"] = ["rsn"]\n'
    '        settings["802-11-wireless-security"]["pairwise"] = ["ccmp"]\n'
    '        settings["802-11-wireless-security"]["group"] = ["ccmp"]\n'
    '        settings["802-11-wireless-security"]["pmf"] = dbus.Int32(1)\n'
)

old_patched_block = anchor + patched_lines

if old_patched_block in text:
    text = text.replace(old_patched_block, anchor + notice + patched_lines, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("    upgraded: added the modification notice to an already-patched nm.py")
    sys.exit(0)

if anchor not in text:
    sys.stderr.write(
        "    FAIL: expected anchor lines not found in " + path + "\n"
        "    This patch targets comitup 1.43 make_hotspot(); a comitup "
        "upgrade likely changed nm.py structure. Re-derive the patch "
        "against the installed comitup version before re-running -- "
        "do not ship an unpatched hotspot.\n"
    )
    sys.exit(1)

text = text.replace(anchor, anchor + notice + patched_lines, 1)
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("    patched: added modification notice and proto/pairwise/group/pmf to nm.py")
