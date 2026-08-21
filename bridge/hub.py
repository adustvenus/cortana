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
import time

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
_announce_seq = itertools.count(1)


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


async def send(ws, msg):
    """Send, dropping the socket on any failure. Never raises."""
    try:
        await ws.send_str(msg)
    except Exception:
        _sockets.discard(ws)


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


def announce(text, **_kwargs):
    """Thread-safe. Installed over voice.speech.say/announce inside the bridge
    process: the bridge has no speaker, so every line Cortana would have spoken
    goes to the connected phones instead. Accepts and ignores speech's keyword
    arguments so it is a drop-in replacement.

    Recorded before it is sent, so it survives having no listener. How it is
    shown - banner, toast, or inline - is the app's decision: only the phone
    knows whether it is foregrounded and which screen is open."""
    text = (text or "").strip()
    if not text:
        return
    item = {"type": "announce", "id": next(_announce_seq),
            "ts": time.time(), "text": text[:500]}
    _announces.append(item)
    if _loop is None:
        return          # no server loop yet; it is still recorded for replay
    msg = json.dumps(item)

    def dispatch():
        for ws in list(_sockets):
            asyncio.ensure_future(send(ws, msg))

    _loop.call_soon_threadsafe(dispatch)
