"""Shared primitives: the TTL cache every state reader uses, and subprocess
helpers. Imports only settings, so anything may depend on it."""
import subprocess
import time

_cache = {}


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
