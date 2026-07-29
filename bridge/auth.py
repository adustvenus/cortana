"""Request authentication for the two audiences the bridge serves.

Phones present a bearer token minted at pairing (see pairing.py). The Dusk
dashboard talks over loopback only and needs no token - it already runs on this
machine, and its /local endpoints expose nothing a local process couldn't read
from disk anyway.
"""
from aiohttp import web

from bridge import pairing

CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def token_of(request):
    """Bearer header, or the query parameter WebSockets need (browsers and
    OkHttp can't always attach headers to a ws:// handshake)."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.query.get("token", "")


def device(request):
    """The paired device for this request, or None. Refreshes last_seen."""
    return pairing.auth(token_of(request))


def deny():
    return web.json_response({"error": "unauthorized"}, status=401)


def is_loopback(request):
    return (request.remote or "") in _LOOPBACK


def local_guard(request):
    """Returns an error response for non-loopback callers, else None."""
    if not is_loopback(request):
        return web.json_response({"error": "loopback only"}, status=403, headers=CORS)
    return None
