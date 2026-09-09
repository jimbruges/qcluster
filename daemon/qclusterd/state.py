"""Persistent daemon state: slot map, decommissioned boards, custom models, and the
optional per-board sudo password.

Written 0600 because it can hold board shell passwords.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from . import config

log = logging.getLogger("qclusterd.state")

_DEFAULTS: dict = {
    "slots": {},
    "decommissioned": [],
    "custom_models": [],
    "sudo_passwords": {},
}


class State:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = dict(_DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            with config.STATE_PATH.open() as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = {**_DEFAULTS, **loaded}
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s: %s", config.STATE_PATH, exc)

    def _save(self) -> None:
        try:
            config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.STATE_PATH.with_suffix(".tmp")
            with tmp.open("w") as fh:
                json.dump(self._data, fh, indent=2)
            os.chmod(tmp, 0o600)
            tmp.replace(config.STATE_PATH)
        except OSError as exc:
            log.warning("could not persist state: %s", exc)

    def get(self, key: str):
        with self._lock:
            value = self._data.get(key, _DEFAULTS.get(key))
            return json.loads(json.dumps(value))  # detached copy

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def update(self, key: str, fn) -> None:
        with self._lock:
            self._data[key] = fn(self._data.get(key, _DEFAULTS.get(key)))
            self._save()

    # -- board sudo passwords -------------------------------------------
    def sudo_password(self, serial: str) -> str:
        with self._lock:
            return str((self._data.get("sudo_passwords") or {}).get(serial, ""))

    def set_sudo_password(self, serial: str, password: str) -> None:
        def _apply(current):
            current = dict(current or {})
            if password:
                current[serial] = password
            else:
                current.pop(serial, None)
            return current

        self.update("sudo_passwords", _apply)

    def has_sudo_password(self, serial: str) -> bool:
        return bool(self.sudo_password(serial))

    # -- decommissioned boards ------------------------------------------
    def is_decommissioned(self, serial: str) -> bool:
        return serial in (self.get("decommissioned") or [])

    def set_decommissioned(self, serial: str, value: bool) -> None:
        def _apply(current):
            current = list(current or [])
            if value and serial not in current:
                current.append(serial)
            if not value and serial in current:
                current.remove(serial)
            return current

        self.update("decommissioned", _apply)


STATE = State()
