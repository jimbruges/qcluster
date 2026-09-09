"""Supervises the single llama-server process that fronts the whole cluster.

Pooling works through llama.cpp's GGML RPC backend: every child board runs an
rpc-server that llama-server treats as an extra backend device, so model layers are
spread across the RAM of all boards instead of having to fit in one.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import config
from .models import Model

log = logging.getLogger("qclusterd.engine")

STATE_STOPPED = "stopped"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_ERROR = "error"

LISTENING_RE = re.compile(r"(server is listening|HTTP server listening|starting the main loop)", re.I)
PROGRESS_RE = re.compile(r"load_tensors:.*?(\d+)%")


class Placement:
    """Which boards hold the model, and in what proportion."""

    def __init__(self, endpoints: list[str], weights: list[float], labels: list[str],
                 total_mb: int, needed_mb: int) -> None:
        self.endpoints = endpoints
        self.weights = weights
        self.labels = labels
        self.total_mb = total_mb
        self.needed_mb = needed_mb

    @property
    def fits(self) -> bool:
        return self.total_mb >= self.needed_mb

    @property
    def pooled(self) -> bool:
        return bool(self.endpoints)

    def as_dict(self) -> dict:
        return {
            "endpoints": self.endpoints,
            "weights": [round(w, 3) for w in self.weights],
            "labels": self.labels,
            "total_usable_mb": self.total_mb,
            "needed_mb": self.needed_mb,
            "fits": self.fits,
            "pooled": self.pooled,
            "tensor_split": self.tensor_split(),
        }

    def tensor_split(self) -> str | None:
        if len(self.weights) < 2:
            return None
        return ",".join(f"{w:.3f}" for w in self.weights)


def plan_placement(model: Model, nodes: list, ctx_size: int | None = None) -> Placement:
    """Split the model across every ready board in proportion to its free RAM.

    Device order matters: llama.cpp registers RPC devices before the local backend,
    so RPC endpoints come first and the host's own share goes last.
    """
    ctx_overhead_mb = int((ctx_size or model.ctx_size) / 1024 * 120)
    needed = model.ram_mb + ctx_overhead_mb

    children = [n for n in nodes if not n.is_host and n.state == "ready" and n.rpc_running]
    host = next((n for n in nodes if n.is_host), None)

    endpoints = [n.rpc_endpoint for n in children]
    labels = [f"slot{n.slot} ({n.serial[:8]})" for n in children]
    capacities = [float(n.usable_mb()) for n in children]

    if host is not None:
        labels.append("host")
        capacities.append(float(host.usable_mb()))

    total = sum(capacities)
    weights = [c / total for c in capacities] if total > 0 else []
    return Placement(endpoints, weights, labels, int(total), int(needed))


class LlamaEngine:
    def __init__(self, log_lines: int = 400) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._log: collections.deque[str] = collections.deque(maxlen=log_lines)
        self.state = STATE_STOPPED
        self.error: str | None = None
        self.model_id: str | None = None
        self.alias: str | None = None
        self.argv: list[str] = []
        self.placement: dict | None = None
        self.load_percent = 0
        self.started_at: float | None = None
        self.last_metrics: dict = {}

    # -- introspection --------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def logs(self) -> list[str]:
        return list(self._log)

    def status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "model_id": self.model_id,
            "alias": self.alias,
            "running": self.running,
            "load_percent": self.load_percent,
            "placement": self.placement,
            "argv": self.argv,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else None,
            "endpoint": f"http://{config.LLAMA_HOST}:{config.LLAMA_PORT}",
            "last_metrics": self.last_metrics,
        }

    # -- lifecycle ------------------------------------------------------
    def start(self, model: Model, placement: Placement, ctx_size: int | None = None,
              threads: int | None = None) -> None:
        with self._lock:
            self._stop_locked()
            binary = config.RUNTIME_DIR / "llama-server"
            if not binary.exists():
                raise RuntimeError("runtime/llama-server missing - build llama.cpp first")
            if not model.downloaded():
                raise RuntimeError(f"model {model.id} is not downloaded")

            ctx = int(ctx_size or model.ctx_size)
            argv = [
                str(binary),
                "--model", str(model.path),
                "--host", config.LLAMA_HOST,
                "--port", str(config.LLAMA_PORT),
                "--alias", model.id,
                "--ctx-size", str(ctx),
                "--threads", str(int(threads or config.DEFAULT_THREADS)),
                "--no-webui",
                # Thinking mode is unusable at this speed: minutes of hidden
                # reasoning per reply, often looping without an answer.
                "--reasoning", "off",
                "--reasoning-budget", "0",
            ]
            if placement.endpoints:
                argv += ["--rpc", ",".join(placement.endpoints), "--n-gpu-layers", "999"]
                split = placement.tensor_split()
                if split:
                    argv += ["--tensor-split", split]

            env = dict(os.environ)
            env["LD_LIBRARY_PATH"] = str(config.RUNTIME_DIR) + (
                ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
            )

            self._log.clear()
            self.argv = argv
            self.model_id = model.id
            self.alias = model.id
            self.placement = placement.as_dict()
            self.state = STATE_LOADING
            self.error = None
            self.load_percent = 0
            self.started_at = time.time()

            log.info("starting llama-server: %s", " ".join(argv))
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, bufsize=1,
            )
            threading.Thread(target=self._pump, args=(self._proc,),
                             name="llama-log", daemon=True).start()
            threading.Thread(target=self._await_ready, name="llama-ready", daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            self._log.append(line)
            match = PROGRESS_RE.search(line)
            if match:
                self.load_percent = int(match.group(1))
        code = proc.wait()
        if self.state != STATE_STOPPED:
            self.state = STATE_ERROR
            self.error = f"llama-server exited with code {code}"
            log.error("llama-server exited (%s)", code)

    def _await_ready(self) -> None:
        deadline = time.time() + 900  # a big model over USB can take a while
        while time.time() < deadline:
            if not self.running:
                return
            if self.health():
                self.state = STATE_READY
                self.load_percent = 100
                log.info("llama-server ready (%s)", self.model_id)
                return
            time.sleep(1.0)
        self.state = STATE_ERROR
        self.error = "timed out waiting for llama-server to become healthy"

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://{config.LLAMA_HOST}:{config.LLAMA_PORT}/health", timeout=2
            ) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self.state = STATE_STOPPED
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.model_id = None
        self.alias = None
        self.load_percent = 0
        self.started_at = None
        self.placement = None
        self.argv = []

    def record_metrics(self, payload: dict) -> None:
        timings = payload.get("timings") or {}
        if not timings:
            return
        self.last_metrics = {
            "prompt_tokens": timings.get("prompt_n"),
            "predicted_tokens": timings.get("predicted_n"),
            "prompt_per_second": round(timings.get("prompt_per_second") or 0, 2),
            "predicted_per_second": round(timings.get("predicted_per_second") or 0, 2),
            "at": time.time(),
        }
