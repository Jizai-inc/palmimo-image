#!/bin/bash -e
# Shares logic with apply-pi.sh instead of duplicating it: the
# WPA2/PMF nm.py patch and the files/ tree both come from
# $PALMIMO_IMAGE_DIR (the bind-mounted repository checkout -- see
# pigen/README.md). See doc/design.md for why each
# of these steps exists; this script only orders them.

: "${PALMIMO_IMAGE_DIR:?PALMIMO_IMAGE_DIR is unset -- see pigen/README.md (PIGEN_DOCKER_OPTS bind mount)}"

# 1. comitup nm.py: WPA2/PMF hotspot-security patch. Copy the shared script
#    into the chroot, run it there, then remove it -- the shipped image
#    never keeps this file (same "patch, don't ship the patcher" contract
#    apply-pi.sh's SSH-piped version has).
install -m 0644 "${PALMIMO_IMAGE_DIR}/lib/patch_comitup_nm.py" "${ROOTFS_DIR}/tmp/patch_comitup_nm.py"
on_chroot <<- 'EOF'
	python3 /tmp/patch_comitup_nm.py
EOF
rm -f "${ROOTFS_DIR}/tmp/patch_comitup_nm.py"

# 2. comitup-web: heal a rootfs that inherited a masked comitup-web
#    (defensive; pi-gen's stock stages never mask it). Before the rsync,
#    same ordering as apply-pi.sh: the mask symlink sits at the exact path
#    the files/ no-op replacement unit is about to occupy.
on_chroot <<- 'EOF'
	systemctl unmask comitup-web
EOF

# 3. files/ -> / : identical tree, identical destination, as apply-pi.sh's
#    `rsync -az ... "$FILES_SRC" "${PI_HOST}:/"` step (units, polkit rule,
#    comitup.conf, the NM avahi dispatcher hook, firstboot.sh, the
#    comitup-web no-op replacement unit, and files/boot/firmware/licenses/ --
#    the third-party license texts, see README.md "Licenses and
#    corresponding source"). -a preserves the modes files/ was checked in
#    with; unlike apply-pi.sh, this rsync targets the rootfs directly
#    (ext4), not a live vfat /boot/firmware, so it needs no special-casing
#    here -- export-image later copies /boot/firmware into the FAT
#    partition on its own (see pigen/.workspace/pi-gen/export-image/
#    prerun.sh).
rsync -a "${PALMIMO_IMAGE_DIR}/files/" "${ROOTFS_DIR}/"

# 4. dnsmasq: comitup spawns its own dnsmasq instance for hotspot DHCP/DNS
#    (cdns.py) but only needs the binary -- installed via 00-packages. The
#    system dnsmasq.service must never hold port 53, so disable it. No
#    --now here (unlike apply-pi.sh): there is nothing running in a chroot to
#    stop, the image just never starts it on first boot.
# Enable exactly the four units the design doc specifies -- comitup-web is
#    deliberately absent: files/ already placed the no-op replacement unit
#    at its path, and comitup's webmgr.py starts it on its own on HOTSPOT
#    entry.
on_chroot <<- 'EOF'
	systemctl disable dnsmasq
	systemctl enable comitup avahi-daemon palmimo-portal palmimo-firstboot
EOF
