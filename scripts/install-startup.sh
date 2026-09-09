#!/usr/bin/env bash
# Installs and enables qclusterd as the arduino user's startup service.
#
# This is idempotent. It can be run after cloning/publishing the repo, and it also
# records the exact startup contract used by this board: a systemd user service plus
# lingering so it starts at boot before anyone logs in.
set -euo pipefail

ROOT=/home/arduino/qcluster
USER_NAME=arduino
UNIT_SRC="$ROOT/scripts/qclusterd.service"
UNIT_DST="/home/$USER_NAME/.config/systemd/user/qclusterd.service"

if [ ! -f "$UNIT_SRC" ]; then
  echo "missing $UNIT_SRC" >&2
  exit 1
fi

mkdir -p "$(dirname "$UNIT_DST")"
cp "$UNIT_SRC" "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now qclusterd

if command -v loginctl >/dev/null 2>&1; then
  if [ "$(id -u)" -eq 0 ]; then
    loginctl enable-linger "$USER_NAME"
  elif sudo -n true >/dev/null 2>&1; then
    sudo loginctl enable-linger "$USER_NAME"
  else
    echo "NOTE: linger may need root: sudo loginctl enable-linger $USER_NAME" >&2
  fi
fi

systemctl --user is-enabled qclusterd
systemctl --user is-active qclusterd
loginctl show-user "$USER_NAME" --property=Linger 2>/dev/null || true
