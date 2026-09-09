"""Brings a freshly discovered board up to "ready".

Idempotent by design: it compares the board's copy of runtime/manifest.json against
the host's and only pushes what changed, so a reconnect costs one adb round-trip.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time

from . import adb, config
from .nodes import STATE_ERROR, STATE_PROVISIONING, STATE_READY, Node

log = logging.getLogger("qclusterd.provision")

RPC_LOG = f"{config.NODE_ROOT}/rpc-server.log"
RPC_PIDFILE = f"{config.NODE_ROOT}/rpc-server.pid"

# rpc-server is started detached so it outlives the adb shell session.
START_RPC = f"""
mkdir -p {config.NODE_ROOT} {config.NODE_CACHE_DIR}
if [ -f {RPC_PIDFILE} ] && kill -0 "$(cat {RPC_PIDFILE})" 2>/dev/null; then
  echo already-running
  exit 0
fi
chmod +x {config.NODE_RUNTIME_DIR}/rpc-server 2>/dev/null
cd {config.NODE_ROOT}
LD_LIBRARY_PATH={config.NODE_RUNTIME_DIR} LLAMA_CACHE={config.NODE_CACHE_DIR} \
  setsid nohup {config.NODE_RUNTIME_DIR}/rpc-server \
  --host 127.0.0.1 --port {config.NODE_RPC_PORT} --threads 4 --cache \
  > {RPC_LOG} 2>&1 < /dev/null &
echo $! > {RPC_PIDFILE}
sleep 1
kill -0 "$(cat {RPC_PIDFILE})" 2>/dev/null && echo started || (echo failed; tail -5 {RPC_LOG})
"""

STOP_RPC = f"""
if [ -f {RPC_PIDFILE} ]; then
  kill "$(cat {RPC_PIDFILE})" 2>/dev/null
  rm -f {RPC_PIDFILE}
fi
pkill -f '{config.NODE_RUNTIME_DIR}/rpc-server' 2>/dev/null
echo stopped
"""

RPC_STATUS = f"""
if [ -f {RPC_PIDFILE} ] && kill -0 "$(cat {RPC_PIDFILE})" 2>/dev/null; then
  echo "running $(cat {RPC_PIDFILE})"
else
  echo "stopped"
fi
"""


class Provisioner:
    def __init__(self, registry, cluster_lock: threading.RLock | None = None) -> None:
        self._registry = registry
        self._locks: dict[str, threading.Lock] = {}
        self._global = threading.Lock()
        # Shared with the engine: re-creating a port forward under a running
        # llama-server breaks its RPC connection mid-flight.
        self._cluster = cluster_lock or threading.RLock()

    def _lock_for(self, serial: str) -> threading.Lock:
        with self._global:
            return self._locks.setdefault(serial, threading.Lock())

    # -- public ---------------------------------------------------------
    def provision_async(self, node: Node) -> None:
        threading.Thread(
            target=self.provision, args=(node,), name=f"provision-{node.slot}", daemon=True
        ).start()

    def provision(self, node: Node, force: bool = False) -> bool:
        with self._lock_for(node.serial), self._cluster:
            try:
                node.state = STATE_PROVISIONING
                node.error = None
                self._sync_runtime(node, force=force)
                self._sync_display_app(node)
                self._start_rpc(node)
                self._setup_forward(node)
                node.state = STATE_READY
                node.error = None
                log.info("board %s (slot %d) ready on %s",
                         node.serial, node.slot, node.rpc_endpoint)
                return True
            except Exception as exc:
                node.state = STATE_ERROR
                node.error = str(exc)
                node.rpc_running = False
                log.error("provisioning %s failed: %s", node.serial, exc)
                return False

    def teardown(self, node: Node) -> None:
        adb.forward_remove(node.serial, node.rpc_host_port)
        node.rpc_running = False

    def stop_rpc(self, node: Node) -> None:
        try:
            adb.shell_script(node.serial, STOP_RPC, timeout=20)
        except adb.AdbError as exc:
            log.warning("stopping rpc-server on %s: %s", node.serial, exc)
        node.rpc_running = False

    def restart_rpc(self, node: Node) -> None:
        self.stop_rpc(node)
        time.sleep(1)
        self._start_rpc(node)

    def refresh_rpc_state(self, node: Node) -> None:
        try:
            out = adb.shell_script(node.serial, RPC_STATUS, timeout=10)
        except adb.AdbError:
            node.rpc_running = False
            return
        node.rpc_running = out.strip().startswith("running") and self._port_open(
            node.rpc_host_port
        )

    # -- steps ----------------------------------------------------------
    def _local_manifest(self) -> dict:
        path = config.RUNTIME_DIR / "manifest.json"
        if not path.exists():
            raise RuntimeError(
                "runtime/manifest.json missing - run scripts/build-llama.sh first"
            )
        with path.open() as fh:
            return json.load(fh)

    def _sync_runtime(self, node: Node, force: bool = False) -> None:
        local = self._local_manifest()
        remote = {}
        if not force:
            try:
                raw = adb.shell_script(
                    node.serial,
                    f"cat {config.NODE_RUNTIME_DIR}/manifest.json 2>/dev/null || echo '{{}}'",
                    timeout=45,
                )
                remote = json.loads(raw.strip() or "{}")
            except (adb.AdbError, json.JSONDecodeError):
                remote = {}

        local_files = local.get("files", {})
        remote_files = remote.get("files", {})
        stale = [
            name for name, meta in local_files.items()
            if remote_files.get(name, {}).get("sha256") != meta["sha256"]
        ]
        if not stale:
            node.provisioned_manifest = "up-to-date"
            return

        log.info("pushing %d runtime file(s) to %s", len(stale), node.serial)
        adb.shell_script(node.serial, f"mkdir -p {config.NODE_RUNTIME_DIR}", timeout=15)
        for name in stale:
            adb.push(
                node.serial,
                str(config.RUNTIME_DIR / name),
                f"{config.NODE_RUNTIME_DIR}/{name}",
            )
        adb.shell_script(
            node.serial, f"chmod +x {config.NODE_RUNTIME_DIR}/rpc-server", timeout=15
        )
        adb.push(
            node.serial,
            str(config.RUNTIME_DIR / "manifest.json"),
            f"{config.NODE_RUNTIME_DIR}/manifest.json",
        )
        node.provisioned_manifest = "pushed"

    def _sync_display_app(self, node: Node) -> None:
        """Install and start the LED matrix app if it is not already running."""
        local_app = config.ROOT / "deploy" / "q-cluster-display"
        if not local_app.is_dir():
            log.debug("no built display app at %s, skipping", local_app)
            return
        try:
            running = adb.shell_script(
                node.serial,
                "arduino-app-cli app list 2>/dev/null "
                "| grep -i 'q-cluster-display' | grep -ic 'running' || true",
                timeout=45,
            ).strip()
        except adb.AdbError:
            running = "0"
        if running and running.splitlines()[-1].strip() != "0":
            return
        log.info("installing display app on %s", node.serial)
        adb.shell_script(node.serial, f"mkdir -p {config.NODE_APP_DIR}", timeout=15)
        for child in local_app.iterdir():
            adb.push(node.serial, str(child), f"{config.NODE_APP_DIR}/")
        # First start compiles the sketch on the board, which is slow.
        adb.shell_script(
            node.serial,
            f"arduino-app-cli app start {config.NODE_APP_DIR} >/dev/null 2>&1 &",
            timeout=30,
        )

    def _start_rpc(self, node: Node) -> None:
        out = adb.shell_script(node.serial, START_RPC, timeout=60).strip()
        if "failed" in out:
            raise RuntimeError(f"rpc-server did not start: {out}")
        node.rpc_running = True

    def _setup_forward(self, node: Node) -> None:
        if self._port_open(node.rpc_host_port):
            return
        adb.forward_remove(node.serial, node.rpc_host_port)
        adb.forward(node.serial, node.rpc_host_port, config.NODE_RPC_PORT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._port_open(node.rpc_host_port):
                return
            time.sleep(0.5)
        raise RuntimeError(f"rpc port {node.rpc_host_port} never became reachable")

    @staticmethod
    def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
        with socket.socket() as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
