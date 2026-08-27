"""Bridge-wide constants and logging.

Deliberately the leaf of the dependency graph: every other bridge module may
import this, and this imports nothing from the package. Named settings.py, not
config.py, so it never shadows Cortana's root config module.
"""
import os
import socket

import config as cortana_config          # loads .env; source of ROOT and API keys
                                         # (bridge/__init__.py put it on the path)

BRIDGE_VERSION = "1.5.0"

PORT = int(os.getenv("BRIDGE_PORT", "8765"))
BIND = os.getenv("BRIDGE_BIND", "0.0.0.0")
HOST_NAME = os.getenv("BRIDGE_NAME", "") or socket.gethostname()

ROOT = cortana_config.ROOT               # the Cortana checkout
DIST = ROOT / "mobile" / "dist"          # CI-published APK + version.json

TTS_CAP = 1500                           # matches voice/tts.py speak()
MAX_UPLOAD = 32 * 1024 * 1024            # phone WAV ceiling
MAX_BOARD_SNAPSHOT = 200_000             # dashboard snapshot ceiling, bytes
PUSH_INTERVAL = 1.5                      # seconds between state pushes
# An UNCHANGED snapshot still goes out this often. The push loop dedupes, but
# never silently: the app reaches its pending-edit reconciliation only from an
# incoming state frame, so a fully quiet idle board would strand phone task
# edits with no timeout at all. Keep this well under the 10s freshness window
# in state.cortana_state(), or the phone reads Cortana as offline mid-turn.
PUSH_FLOOR = 5.0
WS_HEARTBEAT = 25                        # seconds

# Phase 2: the scheduler tick runs HERE, not in the cortana process, because
# cortana is designed to be absent (a spoken "shut down" exits 42 and the
# launcher leaves her off) while this unit is Restart=always. Running both
# tickers during the cutover is safe - schedule.claim() is a conditional UPDATE
# - so this switch exists for backing the change out, not for normal use.
SCHEDULER = os.getenv("BRIDGE_SCHEDULER", "1") not in ("0", "false", "False")

# Home fix for the coarse home/out call in presence_link.py. Belongs in
# .env.local, NOT .env: .env is synced across every box by secrets.sh, and this
# is the one value in the file that is about a place rather than a machine.
# Unset (the default) means coordinates cannot be classified, and the honest
# answer for `place` is then whatever the phone itself labelled it.
HOME_LAT = float(os.getenv("HOME_LAT", "") or 0)
HOME_LON = float(os.getenv("HOME_LON", "") or 0)
HOME_RADIUS_M = float(os.getenv("HOME_RADIUS_M", "") or 150)


def log(msg, err=None):
    """One log shape for the whole service so journalctl stays greppable."""
    print(f"[bridge] {msg}" + (f": {err}" if err is not None else ""), flush=True)
