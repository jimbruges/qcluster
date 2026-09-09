"""Daemon entry point: wires the registry, provisioner, model store and engine
together, then serves the gateway."""

from __future__ import annotations

import logging
import signal
import sys
import threading

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
        self.provisioner = Provisioner(self.registry)
        self.gateway = Gateway(self.registry, self.provisioner, self.store, self.engine)
        self._log = logging.getLogger("qclusterd")

    def _on_node_added(self, node) -> None:
        self._log.info("provisioning board %s (slot %d)", node.serial, node.slot)
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

    def run(self) -> None:
        self.registry.start()
        stop = threading.Event()

        def _shutdown(*_):
            self._log.info("shutting down")
            stop.set()
            self.engine.stop()
            self.registry.stop()
            self.gateway.shutdown()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        self.gateway.serve_forever()


def main() -> int:
    _setup_logging()
    Daemon().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
