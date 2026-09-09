# SPDX-License-Identifier: MPL-2.0
"""QCluster display app - runs on every board in the cluster.

Samples this board's CPU and RAM from /proc (which inside the app container is the
host's, not the container's) and renders two bars on the 8x13 LED matrix.

Deliberately self-contained: no configuration, no network. The host daemon reaches
every board over ADB instead, so this app is byte-identical on all boards.
"""

import time

from arduino.app_utils import App, Bridge, Frame, Logger

from qcluster_common import leds, sysstat

TICK_S = 0.5

logger = Logger("qcluster-display")
sampler = sysstat.Sampler()
display = leds.MatrixDisplay(Bridge, Frame)

_state = {"failures": 0, "logged_at": 0.0}


def loop():
    stats = sampler.sample()
    try:
        display.update(stats.cpu_percent, stats.mem_percent)
        _state["failures"] = 0
    except Exception as exc:
        # The sketch can take a few seconds to come up after a cold start.
        _state["failures"] += 1
        now = time.monotonic()
        if now - _state["logged_at"] > 30:
            _state["logged_at"] = now
            logger.warning(f"LED update failing ({_state['failures']}x): {exc}")
    time.sleep(TICK_S)


logger.info("QCluster display started")
App.run(user_loop=loop)
