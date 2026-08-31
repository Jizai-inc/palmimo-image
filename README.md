# Palmimo Image

The Raspberry Pi SD image that ships on the Palmimo device: a pi-gen custom
stage, a manual apply script for the dev loop, the on-device file tree, and
the SD-card provisioning CLI. Together these three flows carry a device from
a clean checkout to a bootable, individualized unit:

```
build the image       flash + individualize            first boot (on-device)
make_image.py    ->   provision_sd.py            ->    palmimo-firstboot.service
dist/*.img.xz          writes the image to the SD +      individualizes hostname
                        injects the identity file          and AP password, AP comes up
```

## Status

This repository is the shipped image: `tools/make_image.py` here is what
produces the `.img.xz` that goes on every Palmimo unit's SD card, and
`tools/provision_sd.py` is what manufacturing uses to flash and individualize
each card.

## Layout

```
apply-pi.sh             -> manual apply script (dev loop, over SSH)
files/                  -> files placed at the same absolute path on the device
  etc/systemd/system/palmimo-portal.service
  etc/systemd/system/palmimo-firstboot.service
  etc/systemd/system/comitup-web.service   -> no-op replacement (never masked)
  etc/polkit-1/rules.d/50-palmimo-portal.rules
  etc/comitup.conf
  etc/NetworkManager/dispatcher.d/50-palmimo-avahi
  usr/local/lib/palmimo/firstboot.sh
tools/
  make_image.py          -> build the shipped .img.xz (pi-gen, Docker)
  provision_sd.py        -> flash an SD card + inject the identity file
  make_identity.py       -> generate a test identity file
lib/
  patch_comitup_nm.py    -> shared WPA2/PMF nm.py patch (apply-pi.sh + pi-gen both use this)
packages.txt             -> apt package list (shared source for apply-pi.sh and pigen/)
pigen/                   -> pi-gen custom stage (see pigen/README.md for stage notes)
dist/                    -> built images land here (gitignored)
doc/design.md            -> design rationale, verification history, failure matrix
```

`files/` reproduces the target's absolute paths as a tree, so apply-pi.sh and
the pi-gen stage can both just rsync/copy it — placement logic lives in one
place.

## Quick start

### 1. Build an image

```bash
uv run tools/make_image.py
```

This automates the whole pi-gen build: preflight (Docker reachable, no stray
build container), clone/pin [pi-gen](https://github.com/RPi-Distro/pi-gen),
sync `pigen/stage-palmimo` and `pigen/config` into the pi-gen checkout, run
the Docker build (streamed to stdout and to `pigen/.workspace/build.log`),
then copy the resulting `.img.xz` into `dist/` and print its sha256.

Useful flags: `--portal-tag` (which palmimo-portal tag to bake in, default
`v0.1.0`), `--pigen-ref` (pin override for one-off builds), `--dry-run`
(print the plan, touch nothing), `--clean` (wipe the pi-gen workspace).

Requires an arm64 Docker host (Docker Desktop on Apple Silicon works). The
image is identical for every unit — it carries no per-device identity.

### 2. Flash + individualize an SD card

```bash
sudo $(which uv) run tools/provision_sd.py \
  --image dist/image_<date>-palmimo.img.xz \
  --device-id 406 --password <sticker-value>
```

- Needs `sudo` (writing `/dev/rdiskN` requires root on macOS; the script
  refuses with guidance up front rather than mid-flash).
- `--device-id` must match `^[a-z0-9-]{1,32}$`, `--password` must match
  `^[A-Za-z0-9]{8,63}$` (the 8-char floor is the WPA2-PSK minimum). Omit
  `--password` to have one generated and printed at the end for the sticker.
- After flashing, it writes `palmimo-identity.json` (device id + initial
  password) to the boot (FAT) partition and touches nothing else —
  individualization itself happens on the device at first boot.
- `--dry-run` prints the plan without touching a real device (no root
  needed).

Safety model: only external removable disks (or an SD card in a built-in
reader — Secure Digital bus + removable media) with no mounted system
partition are ever offered; the operator must retype the exact disk
identifier (e.g. `disk4`) to confirm, not a bare y/N; the disk's fingerprint
is re-checked before flashing and before writing the identity file, in case
the card was swapped mid-sequence; an oversized image is refused before any
byte is written.

### 3. What happens at first boot

`palmimo-firstboot.service` reads the identity file and:

- sets the hostname to `palmimo-<device-id>` (this becomes the AP's SSID)
- sets comitup's AP password to the sticker's initial password

From there the buyer joins the AP with the sticker password, hits the
captive portal, and completes setup in Palmimo Portal.

A unit with no identity file injected (a DIY build) keeps hostname
`palmimo` and Portal falls back to the open first-setup flow — this is an
intentional degradation, not a failure mode.

### Debug flow: apply-pi.sh (apply without reflashing)

Development-loop tool. Applies the same configuration (apt packages, the
nm.py patch, `files/`, units) to a running Raspberry Pi OS Lite (64-bit,
trixie) machine over SSH. **Not used in the shipping path** — shipping
always goes through image build + flash.

```bash
PI_HOST=user@<addr> PORTAL_TAG=v0.1.0 apply-pi.sh
PI_HOST=user@<addr> PORTAL_TAG=v0.1.0 apply-pi.sh --identity ./palmimo-identity.json
PI_HOST=user@<addr> PORTAL_TAG=v0.1.0 apply-pi.sh --no-apt   # faster re-run once apt is done
```

Generate a test identity file:

```bash
uv run tools/make_identity.py --device-id 405 --password <plain>
```

Prerequisites: SSH key auth, and passwordless sudo for the Pi user (both the
Raspberry Pi Imager default). Each step is idempotent; a failed step stops
the script without touching later steps, and running from the start always
converges.

## comitup and the GPL

comitup 1.43's hotspot code (`/usr/share/comitup/comitup/nm.py`, GPL-2.0)
only sets `key-mgmt`/`psk` on the setup AP, which leaves NetworkManager free
to negotiate WPA1/TKIP with PMF unset — modern Apple clients then fail the
handshake against the AP (verified on hardware). `lib/patch_comitup_nm.py`
pins `proto=rsn` / `pairwise=ccmp` / `group=ccmp` and disables PMF
(`brcmfmac` doesn't support PMF in AP mode) right after the anchor lines.
The same patch script runs both in apply-pi.sh (piped over SSH as
`sudo python3 -`) and in the pi-gen custom stage (copied into the chroot),
so the two consumers never diverge. It fails loud — non-zero exit naming the
comitup version it targets — if its anchor lines are missing, so a comitup
upgrade that changes `nm.py`'s structure cannot silently ship an unpatched
hotspot.

Shipping this modified `nm.py` on the SD image triggers GPLv2 §2(a): the
patch inserts a `Modified by Jizai Inc. on <date>` comment right alongside
the changed lines, satisfying the "carry a prominent notice with the date"
requirement. The idempotency key is that notice marker itself, not just the
presence of the new keys, so re-running the patch is always a safe no-op.
The modified `nm.py` stays on the image as plain Python source (never
compiled or stripped) — that covers the source-availability side for this
modification. The rest of comitup and the underlying OS ship unmodified, as
stock Debian / Raspberry Pi OS packages; the corresponding source for those
is available from those projects' own archives, and Jizai does not claim
more than that here. `lib/patch_comitup_nm.py` itself — the script that
performs the patch — is Jizai Inc.'s own work and is licensed under this
repository's Apache-2.0 license. It necessarily quotes the two anchor lines
it searches for in `nm.py`, a de-minimis excerpt used only to locate the
insertion point; the lines it inserts become part of the GPL-2.0 file once
applied.

## Security model

The identity file (`palmimo-identity.json`) holds the device's initial login
password in plaintext and is written to the boot FAT partition, where it
stays readable after first boot — this lets Palmimo Portal reset login
credentials back to the factory value on request. The threat model treats
whoever holds the physical SD card as the device's owner; theft of the SD
card itself is out of scope (see `doc/design.md` for the full reasoning).
SSH ships key-only (`PasswordAuthentication no` +
`KbdInteractiveAuthentication no`); the `user` account ships password-locked
with NOPASSWD sudo, since Palmimo Portal — not SSH — is the key-registration
path. NOPASSWD sudo is there because Palmimo Portal runs the device's own
maintenance actions (Wi-Fi setup, updates, power, SSH-key registration)
through polkit/systemd as this account, so the account is root-equivalent
by design; since it has no password and no SSH access except keys the owner
registered through the Portal, the trust boundary is "whoever can log in to
the Portal / holds the SD card", not the sudo rule.

## More detail

See [`doc/design.md`](doc/design.md) for the full design: the confirmed
service/unit contracts, the polkit rule set, the comitup configuration
decisions, the identity file spec, the pi-gen stage structure, and the
on-device verification history. See [`pigen/README.md`](pigen/README.md)
for pi-gen stage implementation notes and the manual (non-`make_image.py`)
build recipe.
