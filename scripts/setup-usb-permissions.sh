#!/usr/bin/env bash
# One-time privileged setup: lets the 'arduino' user talk to child UNO Q boards over USB.
# Everything else in QCluster runs unprivileged.
#
#   sudo ./scripts/setup-usb-permissions.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run me with sudo: sudo $0" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-arduino}"
RULES=/etc/udev/rules.d/51-arduino-uno-q-adb.rules

getent group plugdev >/dev/null || groupadd -r plugdev
usermod -aG plugdev "$TARGET_USER"

# 2341:0078 is the UNO Q; 2341 covers other Arduino boards on the same hub.
cat > "$RULES" <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="2341", MODE="0660", GROUP="plugdev", TAG+="uaccess"
EOF

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=add

echo "done. '$TARGET_USER' added to plugdev and udev rules installed."
echo "Log out and back in (or run 'newgrp plugdev') for the group to take effect."
