"""Locate a physical board by blinking its MPU-controlled user LED."""

from __future__ import annotations

import glob
import logging
import subprocess
import threading

from . import adb

log = logging.getLogger("qclusterd.identify")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(serial: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(serial, threading.Lock())


def host_supported() -> bool:
    return any(glob.glob("/sys/class/leds/*user*/brightness"))


def start(serial: str, slot: int) -> dict:
    """Blink without blocking the API request; repeat clicks do not overlap."""
    lock = _lock_for(serial)
    if lock.locked():
        return {"ok": True, "identifying": True, "message": "already blinking"}

    thread = threading.Thread(
        target=_blink,
        args=(serial, slot, lock),
        name=f"identify-{serial}",
        daemon=True,
    )
    thread.start()
    pulses = max(1, min(int(slot), 12))
    return {
        "ok": True,
        "identifying": True,
        "pulses": pulses,
        "message": f"blinking board {slot if slot else 'host'}",
    }


def _blink(serial: str, slot: int, lock: threading.Lock) -> None:
    with lock:
        script = _blink_script(slot)
        try:
            if serial == "host":
                proc = subprocess.run(
                    ["/bin/sh", "-c", script],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                if proc.returncode != 0:
                    detail = proc.stderr.decode(errors="replace").strip()
                    log.warning("host identify failed: %s", detail or proc.returncode)
            else:
                adb.shell_script(serial, script, timeout=30)
        except (adb.AdbError, OSError, subprocess.SubprocessError) as exc:
            log.warning("identify failed for %s: %s", serial, exc)


def _blink_script(slot: int) -> str:
    pulses = max(1, min(int(slot), 12))
    on_time = "0.8" if slot == 0 else "0.22"
    return rf"""
state=/tmp/qcluster-identify.$$
: > "$state"
for brightness in /sys/class/leds/*user*/brightness; do
  [ -w "$brightness" ] || continue
  directory=${{brightness%/brightness}}
  trigger=""
  if [ -r "$directory/trigger" ]; then
    trigger=$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$directory/trigger")
    [ -w "$directory/trigger" ] && echo none > "$directory/trigger"
  fi
  printf '%s|%s|%s\n' "$brightness" "$(cat "$brightness")" "$trigger" >> "$state"
done
[ -s "$state" ] || {{ rm -f "$state"; exit 3; }}
restore() {{
  while IFS='|' read -r brightness value trigger; do
    directory=${{brightness%/brightness}}
    [ -n "$trigger" ] && [ -w "$directory/trigger" ] && echo "$trigger" > "$directory/trigger"
    echo "$value" > "$brightness"
  done < "$state"
  rm -f "$state"
}}
trap restore EXIT HUP INT TERM
repeat=0
while [ "$repeat" -lt 2 ]; do
  pulse=0
  while [ "$pulse" -lt {pulses} ]; do
    while IFS='|' read -r brightness _ _; do echo 1 > "$brightness"; done < "$state"
    sleep {on_time}
    while IFS='|' read -r brightness _ _; do echo 0 > "$brightness"; done < "$state"
    sleep 0.22
    pulse=$((pulse + 1))
  done
  sleep 0.65
  repeat=$((repeat + 1))
done
"""