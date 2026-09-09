"""Model catalog and downloader.

Downloads are restricted to URLs that appear in models.json. The control API never
accepts a URL from the client, only a catalog id, which keeps this from becoming an
arbitrary-fetch (SSRF) endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("qclusterd.models")

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+\.gguf$")
CHUNK = 1 << 20
USER_AGENT = "qclusterd/0.1"


@dataclass
class Model:
    id: str
    name: str
    params: str
    quant: str
    file: str
    url: str
    file_size_mb: int
    ram_mb: int
    ctx_size: int
    notes: str = ""

    @property
    def path(self):
        return config.MODELS_DIR / self.file

    def downloaded(self) -> bool:
        if not self.path.exists():
            return False
        # A partial file from an interrupted download must not look complete.
        expected = self.file_size_mb * 1024 * 1024
        return self.path.stat().st_size > expected * 0.95


@dataclass
class Download:
    model_id: str
    total_bytes: int = 0
    done_bytes: int = 0
    state: str = "idle"  # idle | downloading | done | error | cancelled
    error: str | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    def as_dict(self) -> dict:
        pct = (100.0 * self.done_bytes / self.total_bytes) if self.total_bytes else 0.0
        return {
            "model_id": self.model_id,
            "state": self.state,
            "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes,
            "percent": round(pct, 1),
            "error": self.error,
        }


class ModelStore:
    def __init__(self) -> None:
        self._models: dict[str, Model] = {}
        self._downloads: dict[str, Download] = {}
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with config.CATALOG_PATH.open() as fh:
            raw = json.load(fh)
        models: dict[str, Model] = {}
        for entry in raw.get("models", []):
            entry = {k: v for k, v in entry.items() if not k.startswith("$")}
            model = Model(**entry)
            if not SAFE_FILENAME.match(model.file):
                raise ValueError(f"unsafe filename in catalog: {model.file!r}")
            if not model.url.startswith("https://"):
                raise ValueError(f"catalog url must be https: {model.url!r}")
            models[model.id] = model
        self._models = models

    def all(self) -> list[Model]:
        return list(self._models.values())

    def get(self, model_id: str) -> Model | None:
        return self._models.get(model_id)

    def downloads(self) -> dict:
        with self._lock:
            return {k: v.as_dict() for k, v in self._downloads.items()}

    def catalog_dict(self) -> list[dict]:
        with self._lock:
            active = {k: v.as_dict() for k, v in self._downloads.items()}
        out = []
        for model in self.all():
            entry = {
                "id": model.id,
                "name": model.name,
                "params": model.params,
                "quant": model.quant,
                "file": model.file,
                "file_size_mb": model.file_size_mb,
                "ram_mb": model.ram_mb,
                "ctx_size": model.ctx_size,
                "notes": model.notes,
                "downloaded": model.downloaded(),
                "download": active.get(model.id),
            }
            out.append(entry)
        return out

    def delete(self, model_id: str) -> None:
        model = self._models.get(model_id)
        if not model:
            raise KeyError(model_id)
        try:
            os.remove(model.path)
        except FileNotFoundError:
            pass

    def cancel(self, model_id: str) -> None:
        with self._lock:
            dl = self._downloads.get(model_id)
        if dl:
            dl._cancel.set()

    def start_download(self, model_id: str) -> Download:
        model = self._models.get(model_id)
        if not model:
            raise KeyError(model_id)
        with self._lock:
            existing = self._downloads.get(model_id)
            if existing and existing.state == "downloading":
                return existing
            download = Download(model_id=model_id, state="downloading")
            self._downloads[model_id] = download
        threading.Thread(
            target=self._download, args=(model, download), name=f"dl-{model_id}", daemon=True
        ).start()
        return download

    def _download(self, model: Model, download: Download) -> None:
        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        target = model.path
        partial = target.with_suffix(target.suffix + ".part")
        resume_from = partial.stat().st_size if partial.exists() else 0

        request = urllib.request.Request(model.url, headers={"User-Agent": USER_AGENT})
        if resume_from:
            request.add_header("Range", f"bytes={resume_from}-")

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if resume_from and response.status != 206:
                    resume_from = 0  # server ignored the range request
                length = int(response.headers.get("Content-Length") or 0)
                download.total_bytes = length + resume_from
                download.done_bytes = resume_from
                mode = "ab" if resume_from else "wb"
                with partial.open(mode) as fh:
                    while True:
                        if download._cancel.is_set():
                            download.state = "cancelled"
                            return
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        download.done_bytes += len(chunk)
            partial.replace(target)
            download.state = "done"
            log.info("downloaded %s (%d bytes)", model.id, download.done_bytes)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            download.state = "error"
            download.error = str(exc)
            log.error("download of %s failed: %s", model.id, exc)
