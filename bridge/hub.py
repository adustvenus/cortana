"""Connected-phone registry and outbound messaging.

Holds the live WebSockets and the two ways we push to them: a full state
snapshot (driven by the server's push loop) and one-off announcements from
Cortana's speech layer. Imports nothing from the package beyond settings, so
brain.py can announce without creating an import cycle back through state.
"""
import asyncio
import collections
import itertools
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from bridge.settings import log

_sockets = {}          # ws -> device ident (token hash), for live presence
_loop = None

# Announcements are the ONLY thing Cortana says to a phone, and they were fire
# and forget: a task finishing while no phone held a socket was dropped on the
# floor with no trace. Phones disconnect constantly - screen off, app
# backgrounded, tailnet blip - so "connected right now" is the exception, not
# the rule, and that is why completions arrived only sometimes.
#
# Every announcement is kept here with a monotonic id. A phone reports the last
# id it saw in its hello frame and gets everything newer, so a completion
# survives the app being closed.
ANNOUNCE_HISTORY = 50
_announces = collections.deque(maxlen=ANNOUNCE_HISTORY)

# The id counter MUST survive a bridge restart. The phone stores the highest id
# it has seen and only ever raises it (Prefs.setLastAnnounce), then sends it in
# its hello so pending_after() can replay what it missed. A per-process
# count(1) therefore broke replay completely after every restart - and the unit
# is Restart=always: a phone holding id 37 would ask for "newer than 37", get
# fresh ids 1, 2, 3..., and receive nothing until 37 more announcements had
# accumulated in one process lifetime. Live delivery hid it, so the only thing
# that ever failed was the exact case this mechanism exists for: a completion
# landing while the app is closed.
SEQ_FILE = Path(__file__).resolve().parent.parent / "announce_seq.json"
_seq_lock = threading.Lock()


def _load_seq():
    """Next id to hand out. Absent file = first run, start at 1."""
    try:
        return max(1, int(json.loads(SEQ_FILE.read_text())["next"]))
    except FileNotFoundError:
        return 1
    except Exception:
        # Corrupt/unreadable. Restarting at 1 would silently reinstate the very
        # bug above, so fall back to the clock - always higher than any small
        # id already issued, and still inside the Int32 the app stores.
        return int(time.time())


def _persist_seq(next_id):
    try:
        tmp = tempfile.NamedTemporaryFile("w", dir=SEQ_FILE.parent,
                                          delete=False, suffix=".tmp")
        json.dump({"next": next_id}, tmp)
        tmp.close()
        os.replace(tmp.name, SEQ_FILE)
    except Exception as e:
        # Not fatal: ids stay correct for this process, only replay across the
        # next restart degrades. Worth a line so it is not silent.
        log("announce seq not persisted", e)


_announce_seq = itertools.count(_load_seq())


def bind_loop(loop):
    """Called once at startup: lets worker threads schedule sends safely."""
    global _loop
    _loop = loop


def add(ws, ident=""):
    _sockets[ws] = ident


def discard(ws):
    _sockets.pop(ws, None)


def count():
    return len(_sockets)


def online_idents():
    """Idents of devices with a live socket RIGHT NOW - the authoritative
    'online' signal. last_seen timestamps only move on REST calls, so an idle
    phone used to flap to OFFLINE while its socket was happily connected."""
    return {i for i in _sockets.values() if i}


def sockets():
    return list(_sockets)


def target_socket():
    """The ONE socket a command should go to, or None.

    Deliberately not a broadcast. A "state" frame is idempotent, so every phone
    gets it; a "cmd" frame asks a phone to DO something, and "send this SMS" run
    on three paired devices sends three text messages. The most recently
    connected socket is the best guess at the handset the user is holding -
    _sockets is a dict, so insertion order is connection order.
    """
    live = list(_sockets)
    return live[-1] if live else None


async def send(ws, msg):
    """Send, dropping the socket on any failure. Never raises."""
    try:
        await ws.send_str(msg)
    except Exception:
        # pop, not discard: _sockets is a dict. discard() raised AttributeError
        # from inside this handler, which escaped through broadcast() into the
        # server's push loop and killed it for EVERY phone until a restart -
        # while the heartbeat kept the sockets open, so the app showed a healthy
        # green dot over frozen data.
        _sockets.pop(ws, None)


async def broadcast(msg):
    for ws in list(_sockets):
        await send(ws, msg)


def pending_after(last_id):
    """Announcements newer than last_id, oldest first. A phone sends the last id
    it saw when it reconnects and replays whatever it missed."""
    try:
        last = int(last_id or 0)
    except (TypeError, ValueError):
        last = 0
    return [a for a in list(_announces) if a["id"] > last]


# Urgency rides along in the frame so the phone can pick a notification
# channel - a critical alarm has to make noise through Do Not Disturb, a task
# completion must not. Sanitised rather than trusted: this same function is
# monkeypatched over voice.speech.announce, whose second positional argument is
# max_hold (an int), so an unaudited value would end up in the frame.
URGENCIES = ("ambient", "normal", "urgent", "critical")


def record(text, urgency="normal", keep=True):
    """Mint an id and build the announcement WITHOUT sending it.

    Split out of announce() for the reconnect replay, which sends to exactly one
    socket - the phone that just came back - and must not re-announce a
    six-hour-old reminder to every other device in the house.

    keep=False leaves it out of the replay ring, and the replay is the only
    caller that wants that. _announces is SHARED: a per-device replay appended
    to it would be handed to the NEXT phone that reconnects and asks for
    everything after its own id, which is the same duplicate this mechanism
    exists to prevent, arriving by a different route.
    """
    text = (text or "").strip()
    if not text:
        return None
    if urgency not in URGENCIES:
        urgency = "normal"
    with _seq_lock:                    # called from worker threads
        seq = next(_announce_seq)
        _persist_seq(seq + 1)
    item = {"type": "announce", "id": seq, "urgency": urgency,
            "ts": time.time(), "text": text[:500]}
    if keep:
        _announces.append(item)
    return item


def announce(text, urgency="normal", **_kwargs):
    """Thread-safe. Installed over voice.speech.say/announce inside the bridge
    process: the bridge has no speaker, so every line Cortana would have spoken
    goes to the connected phones instead. Accepts and ignores speech's keyword
    arguments so it is a drop-in replacement.

    Recorded before it is sent, so it survives having no listener. How it is
    shown - banner, toast, or inline - is the app's decision: only the phone
    knows whether it is foregrounded and which screen is open."""
    item = record(text, urgency)
    if item is None:
        return
    # Delivery has been guessed at for several rounds. Make it visible.
    log(f"announce id={item['id']} {item['urgency']} -> "
        f"{len(_sockets)} socket(s): {item['text'][:60]}")
    if _loop is None:
        return          # no server loop yet; it is still recorded for replay
    msg = json.dumps(item)

    def dispatch():
        for ws in list(_sockets):
            asyncio.ensure_future(send(ws, msg))

    _loop.call_soon_threadsafe(dispatch)
