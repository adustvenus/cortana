"""Connected-phone registry and outbound messaging.

Holds the live WebSockets and the two ways we push to them: a full state
snapshot (driven by the server's push loop) and one-off announcements from
Cortana's speech layer. Imports nothing from the package beyond settings, so
brain.py can announce without creating an import cycle back through state.
"""
import asyncio
import json

_sockets = {}          # ws -> device ident (token hash), for live presence
_loop = None


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


def announce(text, **_kwargs):
    """Thread-safe. Installed over voice.speech.say/announce inside the bridge
    process: the bridge has no speaker, so every line Cortana would have spoken
    goes to the connected phones instead. Accepts and ignores speech's keyword
    arguments so it is a drop-in replacement."""
    text = (text or "").strip()
    if not text or _loop is None:
        return
    msg = json.dumps({"type": "announce", "text": text[:500]})

    def dispatch():
        for ws in list(_sockets):
            asyncio.ensure_future(send(ws, msg))

    _loop.call_soon_threadsafe(dispatch)
