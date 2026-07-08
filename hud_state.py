"""Tiny state broadcaster. Cortana calls set_state(); HUD polls the file.
Decoupled on purpose: writing is cheap, never blocks, HUD failure is isolated.
"""
import json
import tempfile
import os
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / "hud_state.json"
_thoughts = []          # rolling reasoning feed shown in the HUD
_MAX_THOUGHTS = 6


# states: idle | listening | thinking | working | speaking | offline
def set_state(state, agent="", detail=""):
    _write(state, agent, detail)


def think(line):
    """Push a short reasoning/status line to the live feed (like visible thinking)."""
    line = (line or "").strip()
    if not line:
        return
    _thoughts.append(line)
    while len(_thoughts) > _MAX_THOUGHTS:
        _thoughts.pop(0)
    cur = read_state()
    _write(cur.get("state", "thinking"), cur.get("agent", ""), cur.get("detail", ""))


def _write(state, agent, detail):
    tmp = tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent,
                                      delete=False, suffix=".tmp")
    json.dump({"state": state, "agent": agent, "detail": detail,
               "thoughts": list(_thoughts), "ts": time.time()}, tmp)
    tmp.close()
    os.replace(tmp.name, STATE_FILE)  # atomic, HUD never reads a half-written file


def clear_thoughts():
    _thoughts.clear()


def read_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"state": "idle", "agent": "", "detail": "", "thoughts": []}
