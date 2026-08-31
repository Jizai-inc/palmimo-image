#!/bin/bash -e
# Account/SSH policy for the shipped image (pi-gen-only -- not shared with
# apply-pi.sh, which targets a Pi already provisioned by Raspberry Pi
# Imager). Rationale lives in doc/design.md's "pi-gen
# イメージビルドと焼き込み CLI" section: Portal is the key-registration path,
# so SSH ships key-only from boot one, and the account has no usable
# password at all -- NOPASSWD sudo is the only thing standing in for it, for
# devkit ergonomics.

install -m 0440 "files/010-palmimo-user" "${ROOTFS_DIR}/etc/sudoers.d/010-palmimo-user"
on_chroot <<- EOF
	visudo -c -f /etc/sudoers.d/010-palmimo-user
EOF

install -D -m 0644 "files/50-palmimo-key-only.conf" \
	"${ROOTFS_DIR}/etc/ssh/sshd_config.d/50-palmimo-key-only.conf"

# pi-gen only runs `usermod -s /bin/bash` when FIRST_USER_PASS is set
# (stage1/01-sys-tweaks); we ship passwordless, so without this the account
# keeps /usr/sbin/nologin -- breaking both key-based SSH on the device and
# the su calls in 03-portal (found by the first virgin build).
# userconfig.service is Raspberry Pi OS's interactive first-boot account
# wizard (tty8). The primary defense is DISABLE_FIRST_BOOT_USER_RENAME=1
# in config (pi-gen's export-image otherwise re-arms the wizard AFTER all
# stages); this disable is belt-and-braces for a pi-gen that arms earlier.
on_chroot <<- EOF
	usermod -s /bin/bash "${FIRST_USER_NAME}"
	passwd -l "${FIRST_USER_NAME}"
	systemctl disable userconfig.service
EOF
