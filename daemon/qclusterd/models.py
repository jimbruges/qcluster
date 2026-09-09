"""Model catalog and downloader.

Built-in models come from models.json. You can also add a model by pasting a Hugging
Face file URL: those are validated against huggingface.co and a strict path shape, so
the control API still cannot be pointed at arbitrary hosts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import config
from .state import STATE

log = logging.getLogger("qclusterd.models")

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+\.gguf$")
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CHUNK = 1 << 20
USER_AGENT = "qclusterd/0.1"

ALLOWED_HOSTS = {"huggingface.co"}
# /<owner>/<repo>/resolve/<revision>/<path...>.gguf
HF_PATH_RE = re.compile(
    r"^/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/resolve/(?P<rev>[\w.-]+)/(?P<file>[\w./-]+\.gguf)$"
)


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
    custom: bool = False

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
            models[entry["id"]] = self._validated(Model(**entry))
        for entry in STATE.get("custom_models") or []:
            try:
                model = self._validated(Model(**entry, custom=True))
            except (TypeError, ValueError) as exc:
                log.warning("dropping invalid custom model %r: %s", entry.get("id"), exc)
                continue
            models[model.id] = model
        self._models = models

    @staticmethod
    def _validated(model: Model) -> Model:
        if not SAFE_ID.match(model.id):
            raise ValueError(f"unsafe model id: {model.id!r}")
        if not SAFE_FILENAME.match(model.file):
            raise ValueError(f"unsafe filename: {model.file!r}")
        parsed = urllib.parse.urlparse(model.url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"url must be https on {sorted(ALLOWED_HOSTS)}: {model.url!r}")
        return model

    # -- custom (Hugging Face) models ------------------------------------
    def add_from_url(self, url: str, name: str = "", ctx_size: int = 0) -> Model:
        """Register a model from a Hugging Face file URL.

        Only huggingface.co /resolve/ links to .gguf files are accepted, and the size
        is read from the server rather than trusted from the client.
        """
        url = (url or "").strip()
        # Accept the "blob" links you get from the browser address bar too.
        url = url.replace("/blob/", "/resolve/", 1)
        url, _, _ = url.partition("?")

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("URL must be an https link on huggingface.co")
        match = HF_PATH_RE.match(parsed.path)
        if not match:
            raise ValueError(
                "expected a Hugging Face file URL such as "
                "https://huggingface.co/<owner>/<repo>/resolve/main/<model>.gguf"
            )

        filename = os.path.basename(match.group("file"))
        if not SAFE_FILENAME.match(filename):
            raise ValueError(f"unsupported filename: {filename}")

        size_bytes = self._remote_size(url)
        size_mb = max(1, size_bytes // (1024 * 1024))

        model_id = self._unique_id(filename)
        # Weights plus KV cache and runtime overhead; deliberately generous.
        ram_mb = int(size_mb * 1.15) + 350
        model = Model(
            id=model_id,
            name=name.strip() or filename.replace(".gguf", ""),
            params=_guess_params(filename),
            quant=_guess_quant(filename),
            file=filename,
            url=url,
            file_size_mb=size_mb,
            ram_mb=ram_mb,
            ctx_size=int(ctx_size) or config.DEFAULT_CTX_SIZE,
            notes=f"Added from {match.group('owner')}/{match.group('repo')}",
            custom=True,
        )
        self._validated(model)

        def _append(existing):
            entries = [e for e in (existing or []) if e.get("id") != model.id]
            payload = {k: v for k, v in model.__dict__.items() if k != "custom"}
            entries.append(payload)
            return entries

        STATE.update("custom_models", _append)
        self.reload()
        log.info("added custom model %s (%d MB)", model.id, size_mb)
        return model

    def _unique_id(self, filename: str) -> str:
        base = re.sub(r"[^A-Za-z0-9._-]", "-", filename[:-5].lower())[:48].strip("-")
        candidate = base or "custom-model"
        suffix = 2
        while candidate in self._models:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _remote_size(url: str) -> int:
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                length = int(response.headers.get("Content-Length") or 0)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Hugging Face returned {exc.code} for that URL") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ValueError(f"could not reach that URL: {exc}") from exc
        if length <= 0:
            raise ValueError("server did not report a file size; is the link a direct file?")
        return length

    def remove_custom(self, model_id: str) -> None:
        model = self._models.get(model_id)
        if not model or not model.custom:
            raise KeyError(model_id)
        self.delete(model_id)
        STATE.update(
            "custom_models",
            lambda existing: [e for e in (existing or []) if e.get("id") != model_id],
        )
        self.reload()

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
                "custom": model.custom,
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


def _guess_params(filename: str) -> str:
    match = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?![\w])", filename.replace("-", " "))
    if match:
        return f"{match.group(1)}B"
    match = re.search(r"(\d{2,4})\s*[mM](?![\w])", filename.replace("-", " "))
    return f"{match.group(1)}M" if match else "?"


def _guess_quant(filename: str) -> str:
    match = re.search(r"(IQ\d[\w]*|Q\d[\w_]*|BF16|F16|F32)", filename, re.I)
    return match.group(1).upper() if match else "?"
