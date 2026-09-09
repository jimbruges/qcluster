"""Thin, safe wrapper around the adb client.

Every call goes through subprocess with an argv list - never a shell string - and
serials are validated before use, so a hostile device name cannot inject commands.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

from . import config

log = logging.getLogger("qclusterd.adb")

SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{4,64}$")
DEFAULT_TIMEOUT = 20
NO_PERMISSIONS = "no-usb-permission"


class AdbError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    usb: str | None = None
    product: str | None = None
    model: str | None = None

    @property
    def usable(self) -> bool:
        return self.state == "device"


def _validate(serial: str) -> str:
    if not SERIAL_RE.match(serial):
        raise AdbError(f"refusing to use suspicious adb serial: {serial!r}")
    return serial


def _run(argv: list[str], timeout: int = DEFAULT_TIMEOUT, check: bool = True,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [config.ADB_BIN, *argv],
        capture_output=True,
        timeout=timeout,
        input=input_bytes,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise AdbError(f"adb {' '.join(argv)} failed ({proc.returncode}): {stderr}")
    return proc


def start_server() -> None:
    _run(["start-server"], timeout=30, check=False)


def devices() -> list[Device]:
    proc = _run(["devices", "-l"], check=False)
    if proc.returncode != 0:
        return []
    found: list[Device] = []
    for line in proc.stdout.decode(errors="replace").splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial = parts[0]
        if not SERIAL_RE.match(serial):
            log.warning("ignoring device with unexpected serial: %r", serial)
            continue
        # The state can be several words ("no permissions (user ...)"), and the
        # key:value attributes always come last.
        attrs = [t for t in parts[1:] if ":" in t and not t.startswith("[")]
        words = [t for t in parts[1:] if t not in attrs]
        state = " ".join(words) or "unknown"
        if state.startswith("no permissions"):
            state = NO_PERMISSIONS
        extras = dict(t.split(":", 1) for t in attrs)
        found.append(
            Device(
                serial=serial,
                state=state,
                usb=extras.get("usb"),
                product=extras.get("product"),
                model=extras.get("model"),
            )
        )
    return found


def shell(serial: str, command: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a command on the device.

    `command` is joined into a single shell string because adb shell always goes
    through the device's shell; each element is quoted so arguments stay literal.
    """
    _validate(serial)
    quoted = " ".join(_shell_quote(c) for c in command)
    proc = _run(["-s", serial, "shell", quoted], timeout=timeout)
    return proc.stdout.decode(errors="replace")


def shell_script(serial: str, script: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a fixed, developer-authored script on the device.

    Only ever called with literals from this codebase - never with user input.
    """
    _validate(serial)
    proc = _run(["-s", serial, "shell", script], timeout=timeout)
    return proc.stdout.decode(errors="replace")


def _shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value or ""):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def push(serial: str, local: str, remote: str, timeout: int = 900) -> None:
    _validate(serial)
    _run(["-s", serial, "push", local, remote], timeout=timeout)


def pull(serial: str, remote: str, local: str, timeout: int = 300) -> None:
    _validate(serial)
    _run(["-s", serial, "pull", remote, local], timeout=timeout)


def forward(serial: str, host_port: int, device_port: int) -> None:
    _validate(serial)
    _run(["-s", serial, "forward", f"tcp:{host_port}", f"tcp:{device_port}"])


def forward_remove(serial: str, host_port: int) -> None:
    _validate(serial)
    _run(["-s", serial, "forward", "--remove", f"tcp:{host_port}"], check=False)


def forward_list() -> list[tuple[str, str, str]]:
    proc = _run(["forward", "--list"], check=False)
    rows = []
    for line in proc.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows
