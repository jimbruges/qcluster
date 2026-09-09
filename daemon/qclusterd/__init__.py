"""QCluster host daemon: discovers boards over ADB, pools their RAM through the
llama.cpp RPC backend, and serves a web UI plus an OpenAI-compatible API.

Pure standard library on purpose - the UNO Q factory image has no root, no compiler
and no web framework, so the daemon must run with nothing but python3.
"""

import pathlib
import sys

# shared/ holds the modules the on-board display app also uses.
_SHARED = pathlib.Path(__file__).resolve().parents[2] / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

__version__ = "0.1.0"
