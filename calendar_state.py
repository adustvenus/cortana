"""Calendar state broadcaster: Cortana refreshes today's events on a timer and
writes them here; the Dusk dashboard's Agenda module reads this file (via the
Electron bridge). Decoupled and atomic, exactly like hud_state.
"""
import datetime
import json
import os
import tempfile
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / "calendar_state.json"


def write(events):
    _flush({"events": list(events or []), "error": "", "ts": time.time(),
            "day": datetime.date.today().isoformat()})


def write_error(msg):
    # Preserve last-known events on error, but KEEP their original `day`. Wiping
    # it while refreshing `ts` made yesterday's agenda look like today's - the
    # staleness guard in read() can only work if the day survives an error.
    prev = read()
    _flush({"events": prev.get("events", []), "error": str(msg)[:200],
            "day": prev.get("day", ""), "ts": time.time()})


def _flush(payload):
    tmp = tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent, delete=False, suffix=".tmp")
    json.dump(payload, tmp)
    tmp.close()
    os.replace(tmp.name, STATE_FILE)   # atomic; reader never sees a half file


def read():
    """Never hand back yesterday's agenda as if it were today's: Cortana can be
    down for hours (or the file preserved across an error), and stale events
    look identical to real ones on the dashboard and the phone."""
    try:
        d = json.loads(STATE_FILE.read_text())
    except Exception:
        return {"events": [], "error": "", "ts": 0}
    day = d.get("day")
    if day and day != datetime.date.today().isoformat():
        return {"events": [], "ts": d.get("ts", 0),
                "error": "calendar data is from " + day + " - is Cortana running?"}
    return d
