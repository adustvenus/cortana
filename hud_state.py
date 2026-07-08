"""Tiny state broadcaster. Cortana calls set_state(); HUD polls the file.
Decoupled on purpose: writing is cheap, never blocks, HUD failure is isolated.

Cortana is the only writer, so the current state lives in-process and the file
is only rewritten when the payload actually changes - no disk read-back, no
churn from repeated identical writes.
"""
import json
import tempfile
import os
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / "hud_state.json"
_MAX_THOUGHTS = 6

_state = {"state": "idle", "agent": "", "detail": ""}
_thoughts = []                 # rolling reasoning feed shown in the HUD
_last_written = None           # last payload flushed to disk (minus timestamp)


# states: idle | listening | thinking | working | speaking | offline
def set_state(state, agent="", detail=""):
    if state == "idle":
        _thoughts.clear()      # wipe feed so HUD goes blank on idle
    _state.update(state=state, agent=agent, detail=detail)
    _write()


def think(line):
    """Push a short reasoning/status line to the live feed (like visible thinking)."""
    line = (line or "").strip()
    if not line:
        return
    _thoughts.append(line)
    while len(_thoughts) > _MAX_THOUGHTS:
        _thoughts.pop(0)
    _write()


def clear_thoughts():
    _thoughts.clear()
    _write()


def _write():
    global _last_written
    payload = {"state": _state["state"], "agent": _state["agent"],
               "detail": _state["detail"], "thoughts": list(_thoughts)}
    if payload == _last_written:
        return                 # nothing changed - skip the disk write
    _last_written = payload
    tmp = tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent,
                                      delete=False, suffix=".tmp")
    json.dump(dict(payload, ts=time.time()), tmp)
    tmp.close()
    os.replace(tmp.name, STATE_FILE)  # atomic, HUD never reads a half-written file


def read_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"state": "idle", "agent": "", "detail": "", "thoughts": []}
