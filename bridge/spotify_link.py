"""Spotify for the phone: reuses the SAME token files the Dusk dashboard's
Electron Spotify integration writes (Dashboard/app/spotify.json +
spotify_token.json). The phone never holds Spotify credentials - it asks the
bridge, the bridge acts with the dashboard's grant. Full transport control
(play/pause/next/previous) mirrors Dashboard/app/spotify.js exactly.

RATE LIMITING (why spotify_backoff.json exists)
Two independent processes poll Spotify for the same account: this bridge and
the dashboard's Electron shell. Neither knew about the other, and each state()
could cost TWO requests (/me/player returning 204, then the
currently-playing fallback), which added up to ~55 requests/minute and earned a
429 QUOTA_EXCEEDED. Fixes, all four applied here and mirrored in spotify.js:
  - a SHARED backoff file, so a 429 seen by either process silences both, for
    exactly as long as Spotify's Retry-After header asks
  - adaptive intervals: slow polling while nothing is playing
  - the 204 fallback is remembered, not repeated on every single poll
  - the last good state is served while backing off, so the UI keeps its data

Refresh-token rotation race: both processes can refresh. Both re-read the token
file immediately before refreshing and write the result back atomically, so
whoever refreshes last leaves a valid pair on disk for the other. If our
refresh loses the race (Spotify rejects the old refresh token), we re-read the
file once and retry - the other side's newer token will be there.
"""
import json
import os
import tempfile
import time
from pathlib import Path

import requests

APP_DIR = Path(__file__).resolve().parent.parent / "Dashboard" / "app"
CONFIG_FILE = APP_DIR / "spotify.json"
TOKEN_FILE = APP_DIR / "spotify_token.json"
BACKOFF_FILE = APP_DIR / "spotify_backoff.json"   # shared with Electron
API = "https://api.spotify.com/v1"

# How long to trust "nothing is playing" before asking the fallback endpoint
# again. Without this, every idle poll cost two requests instead of one.
IDLE_RECHECK = 60.0

_last_good = {"state": None, "at": 0.0}
_idle_until = {"at": 0.0}


def _client_id():
    try:
        cid = str(json.loads(CONFIG_FILE.read_text()).get("clientId", ""))
        return "" if cid.startswith("YOUR_") else cid
    except Exception:
        return ""


def _load_token():
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def _atomic_write(path, payload):
    try:
        tmp = tempfile.NamedTemporaryFile("w", dir=path.parent,
                                          delete=False, suffix=".tmp")
        json.dump(payload, tmp)
        tmp.close()
        os.replace(tmp.name, path)
    except Exception:
        pass


def _save_token(t):
    _atomic_write(TOKEN_FILE, t)


# ── shared backoff ──────────────────────────────────────────────────────────
def backoff_remaining():
    """Seconds left on the shared cool-off, 0 when clear. Read from disk so a
    429 seen by the Electron side silences this one too."""
    try:
        until = float(json.loads(BACKOFF_FILE.read_text()).get("until", 0))
    except Exception:
        return 0.0
    return max(0.0, until - time.time())


def _set_backoff(seconds, reason=""):
    _atomic_write(BACKOFF_FILE, {"until": time.time() + max(1.0, seconds),
                                 "reason": reason, "at": time.time()})


def _note_429(resp):
    """Honour Retry-After when Spotify sends it; otherwise back off 30s."""
    try:
        wait = float(resp.headers.get("Retry-After", "") or 30)
    except ValueError:
        wait = 30.0
    _set_backoff(wait, "429 from Spotify")
    return wait


def _refresh():
    t = _load_token()               # re-read: Electron may have rotated it
    if not t or not t.get("refresh_token"):
        return None
    r = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
        "client_id": _client_id()}, timeout=15)
    if r.status_code == 429:
        _note_429(r)
        return None
    if not r.ok:
        return None
    j = r.json()
    nt = {"access_token": j["access_token"],
          "refresh_token": j.get("refresh_token") or t["refresh_token"],
          "scope": j.get("scope", t.get("scope", "")),
          "expires_at": int(time.time() * 1000) + j["expires_in"] * 1000}
    _save_token(nt)
    return nt


def _access_token():
    t = _load_token()
    if not t:
        return None
    if time.time() * 1000 > (t.get("expires_at") or 0) - 60000:
        t = _refresh() or _load_token()   # lost a rotation race? take theirs
        if not t or time.time() * 1000 > (t.get("expires_at") or 0) - 5000:
            return None
    return t.get("access_token")


def _parse_playback(j):
    it = j.get("item") or {}
    imgs = ((it.get("album") or {}).get("images") or [{}])
    return {"configured": True, "connected": True, "active": True,
            "playing": bool(j.get("is_playing")),
            "track": it.get("name", ""),
            "artist": ", ".join(a.get("name", "") for a in it.get("artists", [])),
            "art": imgs[0].get("url", ""),
            "progress": j.get("progress_ms", 0),
            "duration": it.get("duration_ms", 0),
            "device": (j.get("device") or {}).get("name", "")}


def _remember(state):
    _last_good["state"] = state
    _last_good["at"] = time.time()
    return state


def state():
    """Same shape the dashboard Music module consumes, plus the active device
    name. Serves the last good reading while rate-limited rather than replacing
    the UI with an error."""
    if not _client_id():
        return {"configured": False, "connected": False}

    cooling = backoff_remaining()
    if cooling > 0:
        base = dict(_last_good["state"] or {"configured": True, "connected": True})
        base.update(rateLimited=True, retryIn=int(cooling))
        return base

    at = _access_token()
    if not at:
        # a 429 during refresh sets the backoff; report that rather than "off"
        cooling = backoff_remaining()
        if cooling > 0:
            base = dict(_last_good["state"] or {"configured": True, "connected": True})
            base.update(rateLimited=True, retryIn=int(cooling))
            return base
        return {"configured": True, "connected": False}

    h = {"Authorization": "Bearer " + at}
    try:
        r = requests.get(API + "/me/player", headers=h, timeout=10)
        if r.status_code == 429:
            wait = _note_429(r)
            base = dict(_last_good["state"] or {"configured": True, "connected": True})
            base.update(rateLimited=True, retryIn=int(wait))
            return base
        if r.ok and r.status_code != 204:
            _idle_until["at"] = 0.0
            return _remember(_parse_playback(r.json()))
        if r.status_code not in (200, 204):
            return _remember({"configured": True, "connected": True,
                              "error": r.status_code, "errorMsg": r.text[:160]})

        # 204: no active device in the API's view. The cross-device fallback
        # doubles our request count, so only ask periodically - in between,
        # trust the known-idle answer.
        if time.time() < _idle_until["at"]:
            return _remember({"configured": True, "connected": True,
                              "active": False, "playing": False})
        r2 = requests.get(API + "/me/player/currently-playing", headers=h, timeout=10)
        if r2.status_code == 429:
            wait = _note_429(r2)
            base = dict(_last_good["state"] or {"configured": True, "connected": True})
            base.update(rateLimited=True, retryIn=int(wait))
            return base
        if r2.status_code == 204:
            _idle_until["at"] = time.time() + IDLE_RECHECK
            return _remember({"configured": True, "connected": True,
                              "active": False, "playing": False})
        if not r2.ok:
            return _remember({"configured": True, "connected": True,
                              "error": r2.status_code, "errorMsg": r2.text[:160]})
        j2 = r2.json()
        if not j2 or not j2.get("item"):
            _idle_until["at"] = time.time() + IDLE_RECHECK
            return _remember({"configured": True, "connected": True,
                              "active": False, "playing": False})
        _idle_until["at"] = 0.0
        return _remember(_parse_playback(j2))
    except Exception as e:
        return {"configured": True, "connected": True, "error": str(e)[:120]}


def control(action):
    at = _access_token()
    if not at:
        return {"ok": False, "error": "Spotify not connected - connect it on the dashboard"}
    m = {"play": ("PUT", "/me/player/play"), "pause": ("PUT", "/me/player/pause"),
         "next": ("POST", "/me/player/next"), "previous": ("POST", "/me/player/previous")}
    if action not in m:
        return {"ok": False, "error": "bad action"}
    try:
        r = requests.request(m[action][0], API + m[action][1],
                             headers={"Authorization": "Bearer " + at}, timeout=10)
        if r.status_code == 429:
            wait = _note_429(r)
            return {"ok": False, "error": f"Spotify rate limit - retry in {int(wait)}s"}
        if r.status_code == 404:
            return {"ok": False, "error": "no active Spotify device - start playback once on any device"}
        # A transport action changes state, so stop trusting the idle shortcut.
        _idle_until["at"] = 0.0
        return {"ok": r.ok or r.status_code == 204, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
