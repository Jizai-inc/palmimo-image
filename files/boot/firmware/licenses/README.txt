Palmimo DevKit -- third-party licenses
=======================================

This directory holds the license texts and copyright notices for
third-party software distributed with this product (Palmimo DevKit,
Jizai Inc.). It lives on the boot partition (FAT32) because that is the
one storage medium on the SD card a purchaser can read directly from a PC,
Mac, or Linux box without booting the device or reaching it over the
network -- the same reason the identity file lives here.

Subdirectories
---------------

  display-firmware/
      Third-party notices for the RP2350 face-display firmware. NOTICE is
      the canonical attribution file; licenses/ holds the full texts.

  tools/uv/
      License texts for `uv`, the Python package manager binary installed
      into the image at build time. Dual-licensed Apache-2.0 OR MIT.

  pi/
      Copyright files for every apt package installed on this image (not
      a fixed list), generated at build time, plus common-licenses/.

  portal/
      License texts for Palmimo Portal's Python dependencies and, if
      generated, its npm THIRD_PARTY_LICENSES.txt. Built at image time.

Corresponding source (GPL / LGPL)
-----------------------------------

By GPLv2 §3(a) / GPLv3 §6(a), corresponding source for every apt source
package on this image is collected at build time into
/usr/share/palmimo/sources/ -- on the root filesystem (ext4), not this FAT
partition, so read it from the device itself or over SSH/SCP.

Check the STATUS line at the top of MANIFEST.txt (here and at
/usr/share/palmimo/sources/MANIFEST.txt): only STATUS: OK means this card
is fit to ship as-is. STATUS: INCOMPLETE lists the reason(s) -- a skipped
dev build, a license needing manual review, or an uncovered binary needing
a decision.

apt package copyright files
-----------------------------

Every apt package's copyright file is also available directly on the
device at /usr/share/doc/<package>/copyright (the standard Debian
location) -- the copies under pi/ in this directory are a convenience
duplicate for anyone who only has the SD card and a card reader, not a
substitute for that on-device location.
