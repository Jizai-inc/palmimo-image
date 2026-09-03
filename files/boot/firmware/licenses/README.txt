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
      Third-party notices for the RP2350 face-display firmware (the
      `.uf2` flashed onto the display board). NOTICE is the canonical
      attribution file; licenses/ holds the full texts it points to.

  tools/uv/
      License texts for `uv`, the Python package manager binary this
      image installs on first boot (see apply-pi.sh / the pi-gen stage).
      uv is dual-licensed Apache-2.0 OR MIT; both texts are included.

  pi/
      Copyright files for every apt package installed on this image (not
      a fixed list -- whatever set of packages a given build actually
      installs). Generated at image-build time from each package's
      /usr/share/doc/<package>/copyright, plus common-licenses/ for the
      licenses those copyright files refer to by pointer (e.g. GPL-2,
      GPL-3, LGPL-2.1) rather than by inline text.

  portal/
      License texts for Palmimo Portal's own Python dependencies (from
      each venv package's dist-info METADATA), Portal's own
      THIRD_PARTY_NOTICES.md, and, if the portal build produced one,
      THIRD_PARTY_LICENSES.txt for its npm dependencies. Generated at
      image-build time.

Corresponding source (GPL / LGPL)
-----------------------------------

Palmimo DevKit is sold, not merely distributed at no charge, so GPLv2 3(a)
/ GPLv3 6(a) style "same-medium, on request only" delivery is not enough --
we ship the source alongside the binaries on the medium the product itself
comes on. Every apt source package's corresponding source is collected at
build time and included on this same SD card, at:

    /usr/share/palmimo/sources/

That path is on the root filesystem (ext4), not this FAT boot partition,
so it cannot be read directly by inserting the card into a PC's SD slot --
read it from the device itself (a terminal on the Pi) or over SSH/SCP from
another machine on the network. This mirrors comitup's own modified
nm.py, which Jizai Inc. patches for WPA2/PMF hotspot security and ships as
plain, unstripped Python source under GPLv2 2(a) (see this repository's
README.md, "Licenses and corresponding source", for the details of that
patch).

Completeness is not a claim this README makes on its own -- check it.
MANIFEST.txt (both at /usr/share/palmimo/sources/MANIFEST.txt and, for
convenience, right here at /boot/firmware/licenses/MANIFEST.txt) opens
with a STATUS line. STATUS: OK means this build collected everything it
was supposed to; anything else (STATUS: INCOMPLETE, with the reason(s)
listed right after it) means this card is NOT fit to ship as-is --
whether because corresponding-source collection was skipped for a dev
build, a package's copyright or license text needs manual review, or an
uncovered binary (e.g. an unreviewed Python runtime) needs a decision.
Always read the STATUS line before treating this directory as complete.

apt package copyright files
-----------------------------

Every apt package's copyright file is also available directly on the
device at /usr/share/doc/<package>/copyright (the standard Debian
location) -- the copies under pi/ in this directory are a convenience
duplicate for anyone who only has the SD card and a card reader, not a
substitute for that on-device location.
