"""Calendar state broadcaster: Cortana refreshes today's events on a timer and
writes them here; the Dusk dashboard's Agenda module reads this file (via the
Electron bridge). Decoupled and atomic, exactly like hud_state.
"""
import json
import os
import tempfile
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / "calendar_state.json"


def write(events):
    _flush({"events": list(events or []), "error": "", "ts": time.time()})


def write_error(msg):
    # Preserve last-known events on error if we have them; just note the error.
    prev = read()
    _flush({"events": prev.get("events", []), "error": str(msg)[:200], "ts": time.time()})


def _flush(payload):
    tmp = tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent, delete=False, suffix=".tmp")
    json.dump(payload, tmp)
    tmp.close()
    os.replace(tmp.name, STATE_FILE)   # atomic; reader never sees a half file


def read():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"events": [], "error": "", "ts": 0}
