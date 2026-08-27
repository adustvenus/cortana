"""Delivery routing: one entry point that decides WHERE something is said.

Before this existed, every proactive line in the codebase hardcoded
speech.announce() - which is correct only when the user is at the desk. A
reminder that fires while you are in the kitchen was simply spoken into an
empty room.

Legs are REGISTERED rather than imported, because the two processes can reach
different surfaces: the cortana process owns the speaker, the bridge owns the
phone WebSocket, and neither can do the other's job. A process that has not
registered a leg records the miss in `deliveries` instead of silently dropping
it, so "why didn't my phone buzz" is answerable from sqlite.
"""
import json
import time

import memory
import presence
import schedule

# surface -> callable(text, urgency). Populated by whichever process can serve
# that surface; absent legs are skipped and audited, never faked.
_legs = {}

# Items routed to a sleeping desk. Spoken when the user comes back rather than
# into a dark room, and dropped once they are too old to be worth hearing.
_HELD_KEY = "notify_held"
_HELD_MAX_AGE = 8 * 3600
_HELD_MAX = 20

# The whole routing policy. A table, not a chain of ifs, because it is the one
# part of this module the tests can assert exhaustively.
#   critical reaches every surface in every state: a redundant alarm is a much
#   smaller failure than a missed one.
_TABLE = {
    "ambient":  {"present": ("board",),
                 "away": ("board",),
                 "asleep": ("board",),
                 "unknown": ("board",)},
    "normal":   {"present": ("desk", "board"),
                 "away": ("phone", "board"),
                 "asleep": ("board", "hold"),
                 "unknown": ("phone", "board")},
    "urgent":   {"present": ("desk", "board"),
                 "away": ("desk", "phone", "board"),
                 "asleep": ("phone", "board"),
                 "unknown": ("desk", "phone", "board")},
    "critical": {"present": ("desk", "phone", "board"),
                 "away": ("desk", "phone", "board"),
                 "asleep": ("desk", "phone", "board"),
                 "unknown": ("desk", "phone", "board")},
}


def register(**legs):
    """register(desk=fn, phone=fn, board=fn). Called once at process start."""
    for name, fn in legs.items():
        if fn is not None:
            _legs[name] = fn


def surfaces_for(urgency, desk_state):
    urgency = urgency if urgency in _TABLE else "normal"
    row = _TABLE[urgency]
    return row.get(desk_state, row["unknown"])


def deliver(text, urgency="normal", src="", ref=0, desk_state=None):
    """Route one line. Returns the surfaces actually reached."""
    text = (text or "").strip()
    if not text:
        return []
    state = desk_state or presence.read_desk()
    wanted = surfaces_for(urgency, state)

    reached = []
    for surface in wanted:
        if surface == "hold":
            _hold(text, urgency, src)
            reached.append("hold")
            continue
        leg = _legs.get(surface)
        if leg is None:
            reached.append(surface + ":unavailable")
            continue
        try:
            leg(text, urgency)
            reached.append(surface)
        except Exception as e:
            reached.append(surface + ":failed")
            print(f"[notify] {surface} leg failed:", e)

    try:
        schedule.log_delivery(src, ref, urgency, reached, state, text)
    except Exception:
        pass       # an audit-row failure must never swallow a delivery
    return reached


# ── held items ─────────────────────────────────────────────────────────────
def _load_held():
    try:
        return json.loads(memory.meta_get(_HELD_KEY, "[]"))
    except Exception:
        return []


def _hold(text, urgency, src):
    items = _load_held()
    items.append({"text": text, "urgency": urgency, "src": src, "ts": time.time()})
    memory.meta_set(_HELD_KEY, json.dumps(items[-_HELD_MAX:]))


def release_held(now=None):
    """Speak what arrived while the desk was asleep. Called when presence flips
    back to `present`, which makes "you got three things while you were out" a
    routine on the presence trigger rather than special-cased code.

    Items older than _HELD_MAX_AGE are dropped, not spoken: someone away for a
    week does not want Tuesday read back to them.
    """
    now = now or time.time()
    items = _load_held()
    if not items:
        return []
    fresh = [i for i in items if now - float(i.get("ts") or 0) <= _HELD_MAX_AGE]
    memory.meta_set(_HELD_KEY, "[]")
    spoken = []
    for item in fresh:
        leg = _legs.get("desk")
        if leg is None:
            break
        try:
            leg(item["text"], item.get("urgency", "normal"))
            spoken.append(item["text"])
        except Exception as e:
            print("[notify] held release failed:", e)
            break
    dropped = len(items) - len(fresh)
    if dropped:
        print(f"[notify] dropped {dropped} held item(s) older than "
              f"{_HELD_MAX_AGE // 3600}h")
    return spoken


def held_count():
    return len(_load_held())
