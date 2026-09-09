"""Node registry: discovers boards over ADB, tracks their state and telemetry.

The host board is registered as a node too (slot 0, role "host") so the UI and the
placement planner can treat the whole cluster uniformly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field

from qcluster_common.sysstat import Sampler

from . import adb, config

log = logging.getLogger("qclusterd.nodes")

# One round-trip per telemetry tick; markers keep parsing unambiguous.
PROBE_SCRIPT = (
    "echo \"cpu $(head -1 /proc/stat | cut -d' ' -f2-)\"; "
    "grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo; "
    "echo \"load $(cut -d' ' -f1 /proc/loadavg)\"; "
    "echo \"uptime $(cut -d' ' -f1 /proc/uptime)\"; "
    "echo \"cores $(nproc)\"; "
    "echo \"temp $(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo -1)\"; "
    "echo \"diskfree $(df -Pm /home/arduino | awk 'NR==2{print $4}')\"; "
    # -x matches the process name exactly, so pgrep cannot match its own -f pattern.
    "echo \"rpc $(pgrep -c -x rpc-server || echo 0)\"; "
    "echo \"board $(tr -d '\\0' < /sys/firmware/devicetree/base/compatible | head -c 32)\""
)

STATE_DISCOVERED = "discovered"
STATE_PROVISIONING = "provisioning"
STATE_READY = "ready"
STATE_ERROR = "error"
STATE_LOST = "lost"


@dataclass
class Node:
    serial: str
    slot: int
    role: str = "node"
    state: str = STATE_DISCOVERED
    rpc_host_port: int = 0
    rpc_running: bool = False
    error: str | None = None
    stats: dict = field(default_factory=dict)
    caps: dict = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)
    provisioned_manifest: str | None = None

    @property
    def is_host(self) -> bool:
        return self.role == "host"

    @property
    def rpc_endpoint(self) -> str:
        return f"127.0.0.1:{self.rpc_host_port}"

    def usable_mb(self) -> int:
        """RAM we are willing to hand to the model on this board."""
        available = int(self.stats.get("mem_available_mb") or 0)
        reserve = config.HOST_RESERVE_MB if self.is_host else config.NODE_RESERVE_MB
        return max(0, available - reserve)

    def as_dict(self) -> dict:
        return {
            "serial": self.serial,
            "slot": self.slot,
            "role": self.role,
            "state": self.state,
            "rpc_host_port": self.rpc_host_port,
            "rpc_running": self.rpc_running,
            "error": self.error,
            "stats": self.stats,
            "caps": self.caps,
            "usable_mb": self.usable_mb(),
            "last_seen": self.last_seen,
        }


class _RemoteCpu:
    """CPU% needs two /proc/stat samples; keep the previous one per board."""

    def __init__(self) -> None:
        self._prev: dict[str, tuple[int, int]] = {}

    def update(self, serial: str, fields: list[int]) -> float:
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        prev = self._prev.get(serial)
        self._prev[serial] = (total, idle)
        if not prev:
            return 0.0
        d_total = total - prev[0]
        d_idle = idle - prev[1]
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

    def forget(self, serial: str) -> None:
        self._prev.pop(serial, None)


def parse_probe(text: str, cpu_percent: float) -> tuple[dict, dict]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        # /proc/meminfo lines are "Key:  value kB"; our own echoes are "key value".
        key, _, rest = line.partition(":") if ":" in line else line.partition(" ")
        if key:
            values[key.strip()] = rest.strip()

    def num(key: str, default: float = 0.0) -> float:
        raw = values.get(key, "").split()
        try:
            return float(raw[0]) if raw else default
        except ValueError:
            return default

    mem_total_kb = num("MemTotal", 1.0) or 1.0
    mem_avail_kb = num("MemAvailable")
    swap_total_kb = num("SwapTotal")
    swap_free_kb = num("SwapFree")
    temp_raw = num("temp", -1.0)

    stats = {
        "cpu_percent": round(cpu_percent, 1),
        "mem_percent": round(100.0 * (mem_total_kb - mem_avail_kb) / mem_total_kb, 1),
        "mem_total_mb": int(mem_total_kb // 1024),
        "mem_available_mb": int(mem_avail_kb // 1024),
        "swap_used_mb": int((swap_total_kb - swap_free_kb) // 1024),
        "temp_c": round(temp_raw / 1000.0, 1) if temp_raw > 1000 else None,
        "load1": num("load"),
        "cores": int(num("cores", 1)),
        "uptime_s": round(num("uptime"), 1),
        "timestamp": time.time(),
    }
    caps = {
        "mem_total_mb": stats["mem_total_mb"],
        "cores": stats["cores"],
        "disk_free_mb": int(num("diskfree")),
        "board": values.get("board", "").strip() or "unknown",
        "rpc_count": int(num("rpc")),
    }
    return stats, caps


class NodeRegistry:
    """Polls `adb devices`, keeps a stable serial -> slot map, samples telemetry."""

    def __init__(self, on_added=None, on_lost=None) -> None:
        self._nodes: dict[str, Node] = {}
        self._lock = threading.RLock()
        self._cpu = _RemoteCpu()
        self._host_sampler = Sampler()
        self._slots = self._load_slots()
        self._on_added = on_added
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        host = Node(serial="host", slot=0, role="host", state=STATE_READY)
        self._nodes["host"] = host

    # -- persistence ----------------------------------------------------
    def _load_slots(self) -> dict[str, int]:
        try:
            with open(config.STATE_PATH) as fh:
                return {k: int(v) for k, v in json.load(fh).get("slots", {}).items()}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _save_slots(self) -> None:
        try:
            config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(config.STATE_PATH, "w") as fh:
                json.dump({"slots": self._slots}, fh, indent=2)
        except OSError as exc:
            log.warning("could not persist slot map: %s", exc)

    def _slot_for(self, serial: str) -> int:
        if serial in self._slots:
            return self._slots[serial]
        taken = set(self._slots.values()) | {0}
        slot = next(i for i in range(1, 256) if i not in taken)
        self._slots[serial] = slot
        self._save_slots()
        return slot

    # -- accessors ------------------------------------------------------
    def all(self) -> list[Node]:
        with self._lock:
            return sorted(self._nodes.values(), key=lambda n: n.slot)

    def get(self, serial: str) -> Node | None:
        with self._lock:
            return self._nodes.get(serial)

    def children(self) -> list[Node]:
        return [n for n in self.all() if not n.is_host]

    def ready_children(self) -> list[Node]:
        return [n for n in self.children() if n.state == STATE_READY and n.rpc_running]

    def ready_nodes_missing_rpc(self) -> list[Node]:
        return [
            n for n in self.children()
            if n.state == STATE_READY and not n.rpc_running and n.stats
        ]

    def snapshot(self) -> dict:
        nodes = [n.as_dict() for n in self.all()]
        ready = [n for n in self.all() if n.state == STATE_READY]
        return {
            "nodes": nodes,
            "total_usable_mb": sum(n.usable_mb() for n in ready),
            "board_count": len(nodes),
            "ready_count": len(ready),
        }

    # -- discovery ------------------------------------------------------
    def _discover_once(self) -> None:
        try:
            devices = adb.devices()
        except adb.AdbError as exc:
            log.warning("adb devices failed: %s", exc)
            return

        seen: set[str] = set()
        for device in devices:
            seen.add(device.serial)
            with self._lock:
                node = self._nodes.get(device.serial)
                if node is None:
                    node = Node(serial=device.serial, slot=self._slot_for(device.serial))
                    node.rpc_host_port = config.RPC_PORT_BASE + node.slot
                    self._nodes[device.serial] = node
                    log.info("discovered board %s (slot %d)", device.serial, node.slot)
                    is_new = True
                else:
                    is_new = node.state == STATE_LOST
                node.last_seen = time.time()
                if not device.usable:
                    node.state = STATE_ERROR
                    if device.state == adb.NO_PERMISSIONS:
                        node.error = (
                            "USB permission denied - run "
                            "'sudo ./scripts/setup-usb-permissions.sh' once, then "
                            "reconnect"
                        )
                    else:
                        node.error = f"adb state: {device.state}"
                    continue
                if node.state in (STATE_ERROR, STATE_LOST) and is_new:
                    node.state = STATE_DISCOVERED
                    node.error = None
            if is_new and self._on_added:
                self._on_added(node)

        with self._lock:
            gone = [
                n for n in self._nodes.values()
                if not n.is_host and n.serial not in seen and n.state != STATE_LOST
            ]
            for node in gone:
                node.state = STATE_LOST
                node.rpc_running = False
                node.error = "disconnected"
                self._cpu.forget(node.serial)
                log.warning("board %s (slot %d) disconnected", node.serial, node.slot)
        for node in gone:
            if self._on_lost:
                self._on_lost(node)

    # -- telemetry ------------------------------------------------------
    def _sample_host(self) -> None:
        stats = self._host_sampler.sample()
        with self._lock:
            host = self._nodes["host"]
            host.stats = stats.as_dict()
            host.caps = {
                "mem_total_mb": stats.mem_total_mb,
                "cores": stats.cores,
                "disk_free_mb": _host_disk_free_mb(),
                "board": "arduino,imola (host)",
            }
            host.last_seen = time.time()

    def _sample_child(self, node: Node) -> None:
        try:
            text = adb.shell_script(node.serial, PROBE_SCRIPT, timeout=15)
        except (adb.AdbError, OSError) as exc:
            log.debug("telemetry failed for %s: %s", node.serial, exc)
            return
        cpu_line = ""
        for line in text.splitlines():
            if line.startswith("cpu "):
                cpu_line = line[4:]
                break
        try:
            fields = [int(v) for v in cpu_line.split()]
        except ValueError:
            return
        if not fields:
            return
        cpu = self._cpu.update(node.serial, fields)
        stats, caps = parse_probe(text, cpu)
        with self._lock:
            node.stats = stats
            node.caps = caps
            if node.state == STATE_READY:
                node.rpc_running = caps.pop("rpc_count", 0) > 0
            else:
                caps.pop("rpc_count", None)

    def _telemetry_once(self) -> None:
        self._sample_host()
        for node in self.children():
            if node.state in (STATE_LOST,):
                continue
            self._sample_child(node)

    # -- lifecycle ------------------------------------------------------
    def _loop(self, fn, interval: float, name: str) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                fn()
            except Exception:
                log.exception("%s loop error", name)
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.2, interval - elapsed))

    def start(self) -> None:
        adb.start_server()
        self._discover_once()
        for fn, interval, name in (
            (self._discover_once, config.DISCOVERY_INTERVAL_S, "discovery"),
            (self._telemetry_once, config.TELEMETRY_INTERVAL_S, "telemetry"),
        ):
            thread = threading.Thread(
                target=self._loop, args=(fn, interval, name), name=name, daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()


def _host_disk_free_mb() -> int:
    import shutil

    try:
        return shutil.disk_usage("/home/arduino").free // (1024 * 1024)
    except OSError:
        return 0
