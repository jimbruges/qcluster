"""System utilisation sampling straight from /proc and /sys (no psutil dependency)."""

from __future__ import annotations

import glob
import os
import time
from dataclasses import asdict, dataclass


@dataclass
class Stats:
    cpu_percent: float
    mem_percent: float
    mem_total_mb: int
    mem_available_mb: int
    swap_used_mb: int
    temp_c: float | None
    load1: float
    cores: int
    uptime_s: float
    timestamp: float

    def as_dict(self) -> dict:
        return asdict(self)


def _read_int_file(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _find_temp_source() -> str | None:
    candidates = sorted(glob.glob("/sys/class/hwmon/hwmon*/temp1_input"))
    candidates += sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))
    for path in candidates:
        if _read_int_file(path) is not None:
            return path
    return None


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                try:
                    values[key] = int(parts[0])
                except ValueError:
                    continue
    return values


class Sampler:
    """Stateful CPU sampler: CPU% needs two /proc/stat readings to be meaningful."""

    def __init__(self) -> None:
        self._prev_total = 0
        self._prev_idle = 0
        self._temp_path = _find_temp_source()
        self._cores = os.cpu_count() or 1
        self._read_cpu()

    def _read_cpu(self) -> float:
        with open("/proc/stat") as fh:
            fields = fh.readline().split()[1:]
        values = [int(v) for v in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        d_total = total - self._prev_total
        d_idle = idle - self._prev_idle
        self._prev_total, self._prev_idle = total, idle
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

    def sample(self) -> Stats:
        cpu = self._read_cpu()
        mem = _meminfo()
        total_kb = mem.get("MemTotal", 1)
        avail_kb = mem.get("MemAvailable", 0)
        swap_total = mem.get("SwapTotal", 0)
        swap_free = mem.get("SwapFree", 0)

        temp = None
        if self._temp_path:
            raw = _read_int_file(self._temp_path)
            if raw is not None:
                # hwmon reports millidegrees; some thermal zones report degrees.
                temp = raw / 1000.0 if raw > 1000 else float(raw)

        with open("/proc/loadavg") as fh:
            load1 = float(fh.read().split()[0])
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])

        return Stats(
            cpu_percent=round(cpu, 1),
            mem_percent=round(100.0 * (total_kb - avail_kb) / total_kb, 1),
            mem_total_mb=total_kb // 1024,
            mem_available_mb=avail_kb // 1024,
            swap_used_mb=(swap_total - swap_free) // 1024,
            temp_c=round(temp, 1) if temp is not None else None,
            load1=load1,
            cores=self._cores,
            uptime_s=round(uptime, 1),
            timestamp=time.time(),
        )
