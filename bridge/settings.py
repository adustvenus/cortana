"""Bridge-wide constants and logging.

Deliberately the leaf of the dependency graph: every other bridge module may
import this, and this imports nothing from the package. Named settings.py, not
config.py, so it never shadows Cortana's root config module.
"""
import os
import socket

import config as cortana_config          # loads .env; source of ROOT and API keys
                                         # (bridge/__init__.py put it on the path)

BRIDGE_VERSION = "1.3.0"

PORT = int(os.getenv("BRIDGE_PORT", "8765"))
BIND = os.getenv("BRIDGE_BIND", "0.0.0.0")
HOST_NAME = os.getenv("BRIDGE_NAME", "") or socket.gethostname()

ROOT = cortana_config.ROOT               # the Cortana checkout
DIST = ROOT / "mobile" / "dist"          # CI-published APK + version.json

TTS_CAP = 1500                           # matches voice/tts.py speak()
MAX_UPLOAD = 32 * 1024 * 1024            # phone WAV ceiling
MAX_BOARD_SNAPSHOT = 200_000             # dashboard snapshot ceiling, bytes
PUSH_INTERVAL = 1.5                      # seconds between state pushes
WS_HEARTBEAT = 25                        # seconds


def log(msg, err=None):
    """One log shape for the whole service so journalctl stays greppable."""
    print(f"[bridge] {msg}" + (f": {err}" if err is not None else ""), flush=True)
