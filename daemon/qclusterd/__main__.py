"""Daemon entry point: wires the registry, provisioner, model store and engine
together, then serves the gateway."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from . import config
from .engine import LlamaEngine, plan_placement
from .gateway import Gateway
from .models import ModelStore
from .nodes import NodeRegistry
from .provision import Provisioner


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


class Daemon:
    def __init__(self) -> None:
        config.ensure_dirs()
        self.store = ModelStore()
        self.engine = LlamaEngine()
        self.registry = NodeRegistry(on_added=self._on_node_added, on_lost=self._on_node_lost)
        self.cluster_lock = threading.RLock()
        self.provisioner = Provisioner(self.registry, self.cluster_lock)
        self.gateway = Gateway(
            self.registry, self.provisioner, self.store, self.engine, self.cluster_lock
        )
        self._log = logging.getLogger("qclusterd")
        self._retries: dict[str, tuple[int, float]] = {}

    def _on_node_added(self, node) -> None:
        self._log.info("provisioning board %s (slot %d)", node.serial, node.slot)
        self._retries.pop(node.serial, None)
        self.provisioner.provision_async(node)

    def _on_node_lost(self, node) -> None:
        self.provisioner.teardown(node)
        if self.engine.running and self.engine.placement:
            if node.rpc_endpoint in (self.engine.placement.get("endpoints") or []):
                self._log.warning(
                    "board %s held model layers - stopping the engine", node.serial
                )
                self.engine.stop()
                self.engine.error = (
                    f"board {node.serial} (slot {node.slot}) disconnected while holding "
                    "model layers; reload a model to continue"
                )
                self.engine.state = "error"

    def _supervise(self, stop: threading.Event) -> None:
        """Restart rpc-server where it has died, and retry boards that failed to provision."""
        while not stop.wait(10):
            for node in self.registry.ready_nodes_missing_rpc():
                self._log.warning(
                    "rpc-server not running on %s (slot %d) - restarting",
                    node.serial, node.slot,
                )
                try:
                    self.provisioner.provision(node)
                except Exception:
                    self._log.exception("could not recover board %s", node.serial)

            for node in self.registry.recoverable():
                # Errors here are usually a transient adb timeout on a busy board.
                if not self._retry_due(node.serial):
                    continue
                self._log.info("retrying board %s (slot %d)", node.serial, node.slot)
                try:
                    self.provisioner.provision(node)
                    self._retries.pop(node.serial, None)
                except Exception:
                    self._log.exception("retry failed for board %s", node.serial)

    def _retry_due(self, serial: str) -> bool:
        """Exponential backoff so a permanently broken board is not retried in a loop."""
        now = time.monotonic()
        attempts, next_at = self._retries.get(serial, (0, 0.0))
        if now < next_at:
            return False
        attempts += 1
        self._retries[serial] = (attempts, now + min(30 * 2 ** (attempts - 1), 600))
        return True

    def run(self) -> None:
        self.registry.start()
        stop = threading.Event()
        threading.Thread(
            target=self._supervise, args=(stop,), name="supervisor", daemon=True
        ).start()

        def _shutdown(*_):
            self._log.info("shutting down")
            stop.set()
            self.engine.stop()
            self.registry.stop()
            # shutdown() blocks until serve_forever() returns, so it must not run
            # on the thread that is inside serve_forever().
            threading.Thread(target=self.gateway.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        self.gateway.serve_forever()


def main() -> int:
    _setup_logging()
    Daemon().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
