"""Shared primitives: the TTL cache every state reader uses, and subprocess
helpers. Imports only settings, so anything may depend on it."""
import json
import subprocess
import time

_cache = {}


def dedup_key(snap):
    """Serialised snapshot minus the fields that move on their own, so an
    unchanged house actually compares equal.

    Lives here rather than in server.py so it stays unit-testable: server.py
    needs aiohttp, which CI deliberately does not install.

    Exactly two fields tick with nothing happening - the top-level `ts`, stamped
    fresh by every state.build(), and each device's `last_seen`, which advances
    on any REST call. `online` is deliberately KEPT: it flips when a phone ages
    past ONLINE_WINDOW, and that is a real change the app should be told about.
    Never mutates the snapshot it is given; the original still gets sent.
    """
    trimmed = {k: v for k, v in snap.items() if k != "ts"}
    devices = trimmed.get("devices")
    if isinstance(devices, list):
        trimmed["devices"] = [
            {k: v for k, v in d.items() if k != "last_seen"} if isinstance(d, dict) else d
            for d in devices]
    return json.dumps(trimmed, sort_keys=True, default=str)


def cached(key, ttl, fn):
    """Memoize fn() under key for ttl seconds. On failure keep serving the last
    good value - a transient git/systemd hiccup must not blank the phone."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = hit[1] if hit else {"error": str(e)[:120]}
    _cache[key] = (now, val)
    return val


def invalidate(*keys):
    """Drop cache entries so the next read is fresh (e.g. right after an action
    that changed the underlying state)."""
    for k in keys:
        _cache.pop(k, None)


def run(cmd, timeout=6):
    """Run a command, return trimmed stdout. Raises on timeout/missing binary,
    which callers translate into user-facing messages."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()
