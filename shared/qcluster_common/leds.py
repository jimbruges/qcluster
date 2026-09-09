"""Renders CPU/RAM utilisation onto the 8x13 LED matrix via the MCU sketch.

Layout (rows, top to bottom):
    0-2  CPU bar   - full brightness
    3    separator - always off
    4-6  RAM bar   - dimmer, so the two bars stay distinguishable at a glance
    7    heartbeat - blinks once per update so a frozen app is obvious

The trailing cell of each bar is brightness-scaled to give sub-cell resolution, so a
13-column bar reads closer to 90 steps than 13.

Values are 0..7 and the sketch runs setGrayscaleBits(3), matching the panel's 3-bit
greyscale.
"""

from __future__ import annotations

import numpy as np

ROWS = 8
COLS = 13

CPU_ROWS = (0, 1, 2)
RAM_ROWS = (4, 5, 6)
HEARTBEAT_ROW = 7

CPU_LEVEL = 7
RAM_LEVEL = 4
HEARTBEAT_LEVEL = 2


def _bar(percent: float, level: int) -> np.ndarray:
    """One row of a bar: full cells at `level`, partial trailing cell scaled down."""
    row = np.zeros(COLS, dtype=np.uint8)
    fraction = max(0.0, min(100.0, float(percent))) / 100.0 * COLS
    full = int(fraction)
    row[:full] = level
    if full < COLS:
        partial = fraction - full
        if partial > 0:
            row[full] = max(1, int(round(partial * level)))
    return row


def build_frame(cpu_percent: float, mem_percent: float, heartbeat: bool = False) -> np.ndarray:
    frame = np.zeros((ROWS, COLS), dtype=np.uint8)

    cpu = _bar(cpu_percent, CPU_LEVEL)
    for row in CPU_ROWS:
        frame[row] = cpu

    ram = _bar(mem_percent, RAM_LEVEL)
    for row in RAM_ROWS:
        frame[row] = ram

    if heartbeat:
        frame[HEARTBEAT_ROW, COLS - 1] = HEARTBEAT_LEVEL

    return frame


class MatrixDisplay:
    """Pushes frames to the sketch, skipping redundant Bridge calls."""

    def __init__(self, bridge, frame_cls, method: str = "draw") -> None:
        self._bridge = bridge
        self._frame_cls = frame_cls
        self._method = method
        self._last: bytes | None = None
        self._tick = 0

    def update(self, cpu_percent: float, mem_percent: float) -> None:
        self._tick += 1
        frame = build_frame(cpu_percent, mem_percent, heartbeat=bool(self._tick % 2))
        payload = self._frame_cls(frame).to_board_bytes()
        if payload == self._last:
            return
        self._last = payload
        self._bridge.call(self._method, payload)

    def clear(self) -> None:
        payload = self._frame_cls(np.zeros((ROWS, COLS), dtype=np.uint8)).to_board_bytes()
        self._last = payload
        self._bridge.call(self._method, payload)
