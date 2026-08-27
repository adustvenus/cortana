"""Read-only views of state OTHER components own: the routines table and
sentinel_state.json.

Nothing here writes anything. Both readers follow the rule every reader in
state.py follows - answer with an empty or last-good value and put the reason in
the payload, because the caller is a dashboard poller and a 500 blanks a tile
that was showing something useful a second ago.

The sentinel file is deliberately passed through rather than interpreted: this
module reports `worst` exactly as written plus how old the reading is, and lets
the dashboard decide what a stale `ok` is worth. Downgrading it here would be
inventing a state the checker never reported.
"""
import json
import sqlite3
import time
from pathlib import Path

from bridge.settings import log

SENTINEL_FILE = Path(__file__).resolve().parent.parent / "sentinel_state.json"
SENTINEL_STALE = 300          # a checker that has not run in 5 minutes is stale
_WORST = ("ok", "warn", "bad")


def routines():
    """The /local/routines contract shape."""
    import memory
    con = memory.connect()
    try:
        rows = con.execute(
            "SELECT name, trigger, enabled, last_fired, fires FROM routines"
            " ORDER BY enabled DESC, name").fetchall()
    except sqlite3.OperationalError as e:
        # A fresh box, or state.db mid-migration: the table is simply not there
        # yet. That is a normal state during a rollout, not an error worth
        # breaking the module over.
        log("routines read failed", e)
        return {"items": [], "error": str(e)[:120]}
    finally:
        con.close()
    return {"items": [{"name": r[0], "trigger": r[1], "enabled": bool(r[2]),
                       "lastFired": r[3] or 0, "fires": r[4] or 0}
                      for r in rows]}


def sentinel(now=None):
    """The /local/sentinel contract shape, plus `stale` and `ageSec`."""
    now = now or time.time()
    try:
        data = json.loads(SENTINEL_FILE.read_text())
    except FileNotFoundError:
        # Nothing has ever written it. Reporting "bad" would light the tile up
        # red on a box where the checker simply is not installed yet.
        return {"worst": "ok", "checks": [], "stale": True, "ageSec": None,
                "error": "no sentinel state yet"}
    except Exception as e:
        log("sentinel state unreadable", e)
        return {"worst": "ok", "checks": [], "stale": True, "ageSec": None,
                "error": str(e)[:120]}
    if not isinstance(data, dict):
        return {"worst": "ok", "checks": [], "stale": True, "ageSec": None,
                "error": "sentinel state is not an object"}
    ts = float(data.get("ts") or 0)
    age = now - ts if ts else None
    checks = [c for c in data.get("checks", []) if isinstance(c, dict)]
    worst = data.get("worst")
    return {"worst": worst if worst in _WORST else "ok",
            "checks": checks[:40],
            "stale": age is None or age > SENTINEL_STALE,
            "ageSec": int(age) if age is not None else None}
