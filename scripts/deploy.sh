#!/usr/bin/env bash
# Assembles runnable Arduino apps from the repo (source of truth) into deploy/ and
# installs the host copy into ~/ArduinoApps. The shared/ package is copied in rather
# than symlinked, because arduino-app-cli copies the app tree into a container.
set -euo pipefail

ROOT=/home/arduino/qcluster
APP=q-cluster-display
SRC="$ROOT/apps/$APP"
OUT="$ROOT/deploy/$APP"
INSTALLED="/home/arduino/ArduinoApps/$APP"

rm -rf "$OUT"
mkdir -p "$OUT"
cp -r "$SRC/." "$OUT/"
cp -r "$ROOT/shared/qcluster_common" "$OUT/python/"
find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} +

echo "assembled $OUT"

if [ "${1:-}" = "--install" ]; then
  mkdir -p "$INSTALLED"
  rsync -a --delete --exclude '.cache' "$OUT/" "$INSTALLED/" 2>/dev/null \
    || { rm -rf "$INSTALLED"; mkdir -p "$INSTALLED"; cp -r "$OUT/." "$INSTALLED/"; }
  echo "installed $INSTALLED"
fi
