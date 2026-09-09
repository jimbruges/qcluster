"""Paths, ports and tunables. Every value can be overridden by an env var."""

from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(os.environ.get("QCLUSTER_ROOT", "/home/arduino/qcluster"))

ADB_BIN = os.environ.get("QCLUSTER_ADB", str(ROOT / "tools" / "bin" / "adb"))
RUNTIME_DIR = ROOT / "runtime"
MODELS_DIR = pathlib.Path(os.environ.get("QCLUSTER_MODELS", str(ROOT / "models")))
LOGS_DIR = ROOT / "logs"
STATE_PATH = ROOT / "state.json"
CATALOG_PATH = ROOT / "daemon" / "models.json"
WEB_DIR = ROOT / "daemon" / "web"

# Where the runtime is staged on each child board.
NODE_ROOT = "/home/arduino/qcluster"
NODE_RUNTIME_DIR = f"{NODE_ROOT}/runtime"
NODE_CACHE_DIR = f"{NODE_ROOT}/rpc-cache"
NODE_APP_DIR = "/home/arduino/ArduinoApps/q-cluster-display"

# Gateway (web UI + API). Binds all interfaces so you can reach it from a laptop.
GATEWAY_HOST = os.environ.get("QCLUSTER_BIND", "0.0.0.0")
GATEWAY_PORT = int(os.environ.get("QCLUSTER_PORT", "7000"))
# Optional shared secret. When set, required on /v1/* and all mutating /api/* calls.
AUTH_TOKEN = os.environ.get("QCLUSTER_TOKEN", "").strip()

# llama-server always stays on loopback; the gateway is the only public surface.
LLAMA_HOST = "127.0.0.1"
LLAMA_PORT = int(os.environ.get("QCLUSTER_LLAMA_PORT", "8081"))

# Per-node RPC ports are allocated as RPC_PORT_BASE + slot on the host side and
# forwarded to NODE_RPC_PORT on the board.
RPC_PORT_BASE = int(os.environ.get("QCLUSTER_RPC_PORT_BASE", "5100"))
NODE_RPC_PORT = int(os.environ.get("QCLUSTER_NODE_RPC_PORT", "50052"))

DISCOVERY_INTERVAL_S = float(os.environ.get("QCLUSTER_DISCOVERY_INTERVAL", "3"))
TELEMETRY_INTERVAL_S = float(os.environ.get("QCLUSTER_TELEMETRY_INTERVAL", "2"))

# Leave this much RAM to the OS on every board when planning a model placement.
# MemAvailable already excludes what is in use, so these are spike cushions only;
# every board also has swap as a second net during the load spike.
HOST_RESERVE_MB = int(os.environ.get("QCLUSTER_HOST_RESERVE_MB", "400"))
NODE_RESERVE_MB = int(os.environ.get("QCLUSTER_NODE_RESERVE_MB", "200"))

DEFAULT_CTX_SIZE = int(os.environ.get("QCLUSTER_CTX_SIZE", "2048"))
DEFAULT_THREADS = int(os.environ.get("QCLUSTER_THREADS", "4"))


def ensure_dirs() -> None:
    for path in (MODELS_DIR, LOGS_DIR, RUNTIME_DIR):
        path.mkdir(parents=True, exist_ok=True)
