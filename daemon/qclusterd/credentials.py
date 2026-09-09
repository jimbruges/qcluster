"""Board shell (sudo) credentials.

QCluster itself runs entirely unprivileged; this exists only for the handful of
administrative jobs that genuinely need root on a board, such as installing the USB
udev rule on the host.

Handling rules, applied throughout:
  * the password is fed to `sudo -S` on **stdin** - never argv, never an env var, and
    never interpolated into a shell string, so it cannot leak via `ps` or shell history
  * it is stored in state.json, which is written 0600
  * it is never logged and never returned by the API
"""

from __future__ import annotations

import logging
import subprocess

from . import config
from .state import STATE

log = logging.getLogger("qclusterd.credentials")

HOST = "host"

NEEDS_PASSWORD = "password-required"
PASSWORDLESS = "passwordless"
STORED_OK = "stored"
STORED_BAD = "stored-invalid"
UNKNOWN = "unknown"


def _run_local(argv: list[str], stdin: str | None = None, timeout: int = 30):
    return subprocess.run(
        argv,
        input=stdin.encode() if stdin is not None else None,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_remote(serial: str, script: str, stdin: str | None = None, timeout: int = 30):
    """Run a shell script on a child board, optionally piping stdin to it."""
    argv = [config.ADB_BIN, "-s", serial, "shell", script]
    return subprocess.run(
        argv,
        input=stdin.encode() if stdin is not None else None,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _sudo_check(serial: str, password: str | None) -> bool:
    """True if sudo succeeds, using the password on stdin when one is supplied."""
    # -k ignores any cached credential timestamp, so a wrong password cannot be
    # accepted just because sudo was used recently. -S reads the password from
    # stdin and -p '' keeps the prompt out of stderr.
    script = "sudo -k -S -p '' true"
    try:
        if serial == HOST:
            proc = _run_local(["/bin/sh", "-c", script], stdin=(password or "") + "\n")
        else:
            proc = _run_remote(serial, script, stdin=(password or "") + "\n")
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _sudo_passwordless(serial: str) -> bool:
    # -k again, so a recent successful sudo does not look like NOPASSWD.
    script = "sudo -k -n true"
    try:
        if serial == HOST:
            proc = _run_local(["/bin/sh", "-c", script])
        else:
            proc = _run_remote(serial, script)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def status(serial: str) -> dict:
    """Whether this board needs a password, and whether the stored one works."""
    try:
        if _sudo_passwordless(serial):
            return {"serial": serial, "state": PASSWORDLESS, "stored": False,
                    "message": "sudo works without a password on this board"}
    except Exception:
        return {"serial": serial, "state": UNKNOWN, "stored": False,
                "message": "could not reach this board"}

    stored = STATE.sudo_password(serial)
    if not stored:
        return {"serial": serial, "state": NEEDS_PASSWORD, "stored": False,
                "message": "sudo needs a password and none is saved"}
    if _sudo_check(serial, stored):
        return {"serial": serial, "state": STORED_OK, "stored": True,
                "message": "saved password verified"}
    return {"serial": serial, "state": STORED_BAD, "stored": True,
            "message": "saved password was rejected by this board"}


def save(serial: str, password: str) -> dict:
    """Verify a password against the board, then store it."""
    if not password:
        STATE.set_sudo_password(serial, "")
        return status(serial)
    if not _sudo_check(serial, password):
        raise ValueError("that password was rejected by the board")
    STATE.set_sudo_password(serial, password)
    log.info("stored sudo password for board %s", serial)
    return status(serial)


def forget(serial: str) -> dict:
    STATE.set_sudo_password(serial, "")
    log.info("removed stored sudo password for board %s", serial)
    return status(serial)


def set_login_password(serial: str, new_password: str) -> dict:
    """Set or change the Linux password for the board's user.

    Works both when sudo is currently passwordless (first-time setup) and when a
    verified password is already stored.
    """
    if len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")

    current = STATE.sudo_password(serial)
    passwordless = _sudo_passwordless(serial)
    if not passwordless and not (current and _sudo_check(serial, current)):
        raise ValueError("save a working sudo password for this board first")

    user = "arduino"
    # chpasswd reads "user:password" from stdin; when sudo also needs a password it
    # comes first on the same stream.
    payload = "" if passwordless else f"{current}\n"
    payload += f"{user}:{new_password}\n"
    script = "sudo -k -S -p '' chpasswd"

    try:
        if serial == HOST:
            proc = _run_local(["/bin/sh", "-c", script], stdin=payload, timeout=45)
        else:
            proc = _run_remote(serial, script, stdin=payload, timeout=45)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc

    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[:200]
        raise RuntimeError(f"chpasswd failed: {detail or 'unknown error'}")

    STATE.set_sudo_password(serial, new_password)
    log.info("changed login password for board %s", serial)
    return status(serial)


def run_privileged(serial: str, script: str, timeout: int = 180) -> str:
    """Run a fixed, developer-authored script as root on a board.

    `script` is only ever a literal from this codebase - never client input.
    """
    password = STATE.sudo_password(serial)
    passwordless = _sudo_passwordless(serial)
    if not passwordless and not password:
        raise PermissionError("no sudo password saved for this board")

    wrapped = f"sudo -k -S -p '' /bin/sh -c {_quote(script)}"
    stdin = "" if passwordless else f"{password}\n"
    try:
        if serial == HOST:
            proc = _run_local(["/bin/sh", "-c", wrapped], stdin=stdin, timeout=timeout)
        else:
            proc = _run_remote(serial, wrapped, stdin=stdin, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc

    output = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(output[:400] or f"command failed ({proc.returncode})")
    return output


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def all_status(serials: list[str]) -> list[dict]:
    return [status(s) for s in serials]
