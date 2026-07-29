"""Phone pairing + device token store for the mobile bridge.

Security model:
- A 6-digit pairing code is generated ON the workstation and only ever shown
  on the Dusk dashboard (served via a loopback-only endpoint). It is
  single-use and expires after PAIR_TTL seconds.
- A successful pair mints a 256-bit random token for the phone. Only the
  SHA-256 of the token is stored here, so a leaked mobile_link.json cannot
  impersonate a phone.
- Brute force: MAX_FAILS wrong codes lock pairing for LOCKOUT seconds and
  burn the current code (a new one must be generated from the dashboard).

The store lives in mobile_link.json next to the Cortana source (gitignored -
it holds token hashes and device names, both per-machine).
"""
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

STORE_FILE = Path(__file__).resolve().parent.parent / "mobile_link.json"
PAIR_TTL = 600          # pairing code lifetime, seconds
MAX_FAILS = 5           # wrong codes before lockout
LOCKOUT = 300           # lockout length, seconds
ONLINE_WINDOW = 75      # last_seen newer than this = phone shown ONLINE

_pair = {"code": None, "expires": 0.0}
_fails = {"count": 0, "until": 0.0}


def _load():
    try:
        d = json.loads(STORE_FILE.read_text())
        return d if isinstance(d.get("devices"), list) else {"devices": []}
    except Exception:
        return {"devices": []}   # missing/corrupt store -> no devices, re-pair


def _save(d):
    tmp = tempfile.NamedTemporaryFile("w", dir=STORE_FILE.parent,
                                      delete=False, suffix=".tmp")
    json.dump(d, tmp, indent=1)
    tmp.close()
    os.replace(tmp.name, STORE_FILE)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_code():
    """Generate (and return) a fresh pairing code. Clears any lockout so the
    user can always recover from the dashboard side."""
    _fails.update(count=0, until=0.0)
    _pair["code"] = f"{secrets.randbelow(1000000):06d}"
    _pair["expires"] = time.time() + PAIR_TTL
    return dict(code=_pair["code"], expires=_pair["expires"])


def pair_info():
    """Current code for the dashboard module (loopback only). None if expired."""
    if _pair["code"] and time.time() < _pair["expires"]:
        return dict(code=_pair["code"], expires=_pair["expires"])
    return None


def locked_for():
    return max(0, _fails["until"] - time.time())


def try_pair(code, device_name):
    """Exchange a valid pairing code for a device token.
    Returns (token, None) or (None, error-string)."""
    if locked_for() > 0:
        return None, f"pairing locked for {int(locked_for())}s"
    cur = pair_info()
    if not cur:
        return None, "no active pairing code - generate one on the dashboard"
    if not hmac.compare_digest(str(code or ""), cur["code"]):
        _fails["count"] += 1
        if _fails["count"] >= MAX_FAILS:
            _fails["until"] = time.time() + LOCKOUT
            _pair["code"] = None        # burn the code on lockout
            return None, "too many attempts - pairing locked"
        return None, "wrong code"
    _pair["code"] = None                # single use
    _fails.update(count=0, until=0.0)
    token = secrets.token_urlsafe(32)
    name = str(device_name or "Android phone").strip()[:48] or "Android phone"
    d = _load()
    # Re-pairing a phone REPLACES its entry instead of stacking another row.
    # Without this, every re-pair (host change, troubleshooting, revoked token)
    # left a duplicate behind, and the list filled with identical names.
    d["devices"] = [x for x in d["devices"] if x.get("name") != name]
    d["devices"].append({"id": secrets.token_hex(8), "hash": _hash(token),
                         "name": name, "created": time.time(),
                         "last_seen": time.time()})
    _save(d)
    return token, None


def auth(token):
    """Validate a bearer token. Returns the device dict (and refreshes its
    last_seen, throttled to ~10s so auth stays cheap) or None."""
    if not token:
        return None
    h = _hash(token)
    d = _load()
    for dev in d["devices"]:
        if hmac.compare_digest(dev.get("hash", ""), h):
            if time.time() - dev.get("last_seen", 0) > 10:
                dev["last_seen"] = time.time()
                _save(d)
            return dev
    return None


def set_app_version(token_hash, version):
    """Record the app version a phone reported (its WS hello frame), so the
    dashboard can show per-device 'up to date' / 'update available'."""
    version = str(version or "")[:24]
    if not token_hash or not version:
        return
    d = _load()
    changed = False
    for dev in d["devices"]:
        if dev.get("hash") == token_hash and dev.get("appVersion") != version:
            dev["appVersion"] = version
            changed = True
    if changed:
        _save(d)


def devices(online_hashes=None):
    """Public device list for both the dashboard module and the phone.
    `id` is what revoke() expects; it falls back to the name for entries
    written before ids existed, so old stores stay revocable.
    `online_hashes`: token hashes with a live WebSocket - authoritative for
    the online flag (last_seen alone flaps while a phone sits idle)."""
    now = time.time()
    online_hashes = online_hashes or set()
    return [{"id": dev.get("id") or dev.get("name", "?"),
             "name": dev.get("name", "?"),
             "last_seen": dev.get("last_seen", 0),
             "appVersion": dev.get("appVersion", ""),
             "online": dev.get("hash") in online_hashes
                       or now - dev.get("last_seen", 0) < ONLINE_WINDOW}
            for dev in _load()["devices"]]


def revoke(ident):
    """Revoke ONE device, by its id. Returns how many entries were removed.

    Revoking used to match on name, which deleted every device sharing it -
    and since a re-pair appended rather than replaced, duplicates were normal.
    Ids are per-entry, so this now removes exactly the row the user tapped.
    Entries written before ids existed are still revocable by name.
    """
    ident = str(ident or "")
    if not ident:
        return 0
    d = _load()
    before = len(d["devices"])
    kept = []
    removed = 0
    for dev in d["devices"]:
        dev_id = dev.get("id")
        # Match this row only: by id when it has one, else by name (legacy).
        hit = (dev_id == ident) if dev_id else (dev.get("name") == ident)
        if hit and removed == 0:
            removed += 1
            continue
        kept.append(dev)
    d["devices"] = kept
    _save(d)
    return before - len(d["devices"])
