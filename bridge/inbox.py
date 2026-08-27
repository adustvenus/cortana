"""speech_inbox.json - the bridge's desk leg.

This process has no speaker and does not own hud_state.json, so anything the
bridge routes to the DESK is queued here and spoken by the cortana process when
it drains the file. Single writer, and it is this module; cortana only reads and
advances a high-water mark in memory.meta. That is the same one-writer rule
presence_desk.json follows in the other direction, and it exists for the same
reason - two processes appending to one JSON file lose items on every overlap.

Bounded on purpose. The whole point of moving the tick into the bridge is that
cortana is DESIGNED to be absent: "shut down" exits 42 and the launcher leaves
her off. "Nobody is draining this" is therefore a normal state, not a fault, and
an unbounded file would read a whole weekend back at her the moment she started.
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from bridge.settings import log

INBOX_FILE = Path(__file__).resolve().parent.parent / "speech_inbox.json"

MAX_ITEMS = 50            # ceiling while nobody is draining
MAX_AGE = 2 * 3600        # older than this is not worth saying on her return
MARK_KEY = "speech_inbox_seq"     # cortana's high-water mark, in memory.meta

_lock = threading.Lock()
_state = {"seq": None, "items": []}


def _resume_seq():
    """Next seq to hand out. Read from the file ONCE, at the first write.

    Monotonic across a bridge restart for exactly the reason hub's SEQ_FILE is:
    cortana compares seq against a mark she persisted, so restarting the counter
    at 1 would make every queued line look already-spoken and silence the desk
    leg completely - the failure would only show up while she was down, which is
    the one case this file exists for.
    """
    try:
        d = json.loads(INBOX_FILE.read_text())
        seq = int(d.get("seq") or 0)
        items = [i for i in d.get("items", []) if isinstance(i, dict)]
        return max(0, seq), items
    except FileNotFoundError:
        return 0, []
    except Exception:
        # Corrupt: restarting the counter would silently reinstate the bug
        # above, so jump above any mark that can already exist.
        log("speech inbox unreadable - resuming the seq from the clock")
        return int(time.time()), []


def put(surface, text, urgency="normal", src=""):
    """Queue one line for cortana. Returns the item, or None if it was empty."""
    text = (text or "").strip()
    if not text:
        return None
    with _lock:
        if _state["seq"] is None:
            _state["seq"], _state["items"] = _resume_seq()
        _state["seq"] += 1
        item = {"seq": _state["seq"], "ts": time.time(),
                "surface": surface, "urgency": urgency,
                "src": src, "text": text[:500]}
        _state["items"].append(item)
        _trim()
        _write()
    return item


def _trim(now=None):
    """Caller holds _lock. Age first, then the hard count cap."""
    now = now or time.time()
    items = [i for i in _state["items"]
             if now - float(i.get("ts") or 0) <= MAX_AGE]
    _state["items"] = items[-MAX_ITEMS:]


def _write():
    """Atomic, same shape as hud_state.py: tempfile in the same directory then
    os.replace, so a reader never sees half a file."""
    payload = {"seq": _state["seq"], "items": _state["items"],
               "ts": time.time()}
    try:
        tmp = tempfile.NamedTemporaryFile("w", dir=INBOX_FILE.parent,
                                          delete=False, suffix=".tmp")
        json.dump(payload, tmp)
        tmp.close()
        os.replace(tmp.name, INBOX_FILE)
    except Exception as e:
        # A queued line is already lost at this point; say so rather than
        # leaving "why didn't she say it" to be guessed at later.
        log("speech inbox not written", e)


def mtime():
    """File mtime, or 0. Lets the cortana-side loop skip the JSON parse on the
    overwhelmingly common tick where nothing was queued - this polls forever on
    a laptop and idle burn is a real constraint here."""
    try:
        return INBOX_FILE.stat().st_mtime
    except OSError:
        return 0.0


def drain(now=None, max_age=MAX_AGE):
    """CORTANA SIDE. Items she has not spoken yet, oldest first.

    The mark advances past everything in the file, including items dropped for
    age, so a stale line is skipped once and never reconsidered. Reading is all
    this side does - it must never write INBOX_FILE.
    """
    import memory                       # lazy: the bridge's write path never
                                        # needs sqlite, and this runs elsewhere
    now = now or time.time()
    try:
        d = json.loads(INBOX_FILE.read_text())
        items = [i for i in d.get("items", []) if isinstance(i, dict)]
    except FileNotFoundError:
        return []
    except Exception as e:
        log("speech inbox unreadable on drain", e)
        return []
    if not items:
        return []
    try:
        mark = float(memory.meta_get(MARK_KEY, "0") or 0)
    except (TypeError, ValueError):
        mark = 0.0
    fresh, seen = [], mark
    for item in items:
        seq = float(item.get("seq") or 0)
        if seq <= mark:
            continue
        seen = max(seen, seq)
        if now - float(item.get("ts") or 0) <= max_age:
            fresh.append(item)
    if seen > mark:
        memory.meta_set(MARK_KEY, repr(seen))
    return fresh
