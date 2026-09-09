"""WiFi status and configuration for the host board.

Only the host needs a network: it serves the UI and downloads models. Child boards
are reached exclusively over USB/ADB, so they are deliberately left off the network.

nmcli works unprivileged because the board's user is in the netdev group, so none of
this needs root or a stored sudo password.
"""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("qclusterd.wifi")

STATUS_SCRIPT = (
    "dev=$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null "
    "| awk -F: '$2==\"wifi\"{print $1; exit}'); "
    "echo \"device ${dev}\"; "
    "if [ -n \"$dev\" ]; then "
    "  nmcli -t -f GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS device show \"$dev\" "
    "    2>/dev/null | sed 's/^/show /'; "
    "  nmcli -t -f IN-USE,SSID,SIGNAL device wifi list --rescan no 2>/dev/null "
    "    | awk -F: '$1==\"*\"{print \"active \" $2 \" \" $3; exit}'; "
    "fi"
)

SCAN_SCRIPT = (
    "nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list --rescan yes 2>/dev/null "
    "| head -40"
)


def _sh(script: str, timeout: int = 30) -> str:
    proc = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, timeout=timeout, check=False
    )
    return proc.stdout.decode(errors="replace")


def status() -> dict:
    """Current WiFi device, SSID, IP address and signal for the host board."""
    info: dict = {"available": False, "device": None, "state": None,
                  "ssid": None, "ip": None, "signal": None, "connected": False}
    try:
        text = _sh(STATUS_SCRIPT)
    except (OSError, subprocess.SubprocessError) as exc:
        info["error"] = str(exc)
        return info

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("device "):
            device = line[7:].strip()
            info["device"] = device or None
            info["available"] = bool(device)
        elif line.startswith("show "):
            key, _, value = line[5:].partition(":")
            key, value = key.strip(), value.strip()
            if key == "GENERAL.STATE":
                info["state"] = value
            elif key == "GENERAL.CONNECTION":
                info["ssid"] = value if value and value != "--" else None
            elif key.startswith("IP4.ADDRESS") and not info["ip"]:
                info["ip"] = value.split("/")[0] or None
        elif line.startswith("active "):
            parts = line.split()
            if len(parts) >= 3:
                info["ssid"] = info["ssid"] or parts[1]
                try:
                    info["signal"] = int(parts[-1])
                except ValueError:
                    pass
    info["connected"] = bool(info["ip"])
    return info


def _split_terse(line: str) -> list[str]:
    """nmcli -t escapes literal colons as '\\:', so split on unescaped colons only."""
    fields, current, escaped = [], "", False
    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(current)
            current = ""
        else:
            current += char
    fields.append(current)
    return fields


def scan() -> list[dict]:
    try:
        text = _sh(SCAN_SCRIPT, timeout=45)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"WiFi scan failed: {exc}") from exc

    networks: dict[str, dict] = {}
    for line in text.splitlines():
        fields = _split_terse(line)
        if len(fields) < 3 or not fields[1]:
            continue
        ssid = fields[1]
        try:
            signal = int(fields[2])
        except ValueError:
            signal = 0
        entry = {
            "ssid": ssid,
            "signal": signal,
            "security": fields[3] if len(fields) > 3 else "",
            "in_use": fields[0] == "*",
        }
        if ssid not in networks or signal > networks[ssid]["signal"]:
            networks[ssid] = entry
    return sorted(networks.values(), key=lambda n: -n["signal"])


def connect(ssid: str, password: str | None) -> dict:
    """Join a network. Credentials go through argv, never a shell string, and the
    password is never logged."""
    if not ssid:
        raise ValueError("ssid is required")
    argv = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        argv += ["password", password]

    log.info("connecting host to SSID %r", ssid)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc

    output = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode != 0 or "error" in output.lower():
        raise RuntimeError(output.splitlines()[0] if output else "connect failed")
    return {"ok": True, "message": output[:300], "status": status()}


def forget(ssid: str) -> None:
    subprocess.run(
        ["nmcli", "connection", "delete", ssid], capture_output=True, timeout=30, check=False
    )
