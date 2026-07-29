"""Spotify for the phone: reuses the SAME token files the Dusk dashboard's
Electron Spotify integration writes (Dashboard/app/spotify.json +
spotify_token.json). The phone never holds Spotify credentials - it asks the
bridge, the bridge acts with the dashboard's grant. Full transport control
(play/pause/next/previous) mirrors Dashboard/app/spotify.js exactly.

Refresh-token rotation race: both Electron and this process can refresh. Both
sides re-read the token file immediately before refreshing and write the
result back atomically, so whoever refreshes last leaves a valid pair on disk
for the other. If our refresh loses the race (Spotify rejects the old refresh
token), we re-read the file once and retry - Electron's newer token will be
there.
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
API = "https://api.spotify.com/v1"


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


def _save_token(t):
    try:
        tmp = tempfile.NamedTemporaryFile("w", dir=TOKEN_FILE.parent,
                                          delete=False, suffix=".tmp")
        json.dump(t, tmp)
        tmp.close()
        os.replace(tmp.name, TOKEN_FILE)
    except Exception:
        pass


def _refresh():
    t = _load_token()               # re-read: Electron may have rotated it
    if not t or not t.get("refresh_token"):
        return None
    r = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"],
        "client_id": _client_id()}, timeout=15)
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


def state():
    """Same shape the dashboard Music module consumes, plus the active device
    name (so the phone can show WHERE music is playing - itself included)."""
    if not _client_id():
        return {"configured": False, "connected": False}
    at = _access_token()
    if not at:
        return {"configured": True, "connected": False}
    h = {"Authorization": "Bearer " + at}
    try:
        r = requests.get(API + "/me/player", headers=h, timeout=10)
        if r.ok and r.status_code != 204:
            return _parse_playback(r.json())
        if r.status_code not in (200, 204):
            return {"configured": True, "connected": True,
                    "error": r.status_code, "errorMsg": r.text[:160]}
        r2 = requests.get(API + "/me/player/currently-playing", headers=h, timeout=10)
        if r2.status_code == 204:
            return {"configured": True, "connected": True, "active": False, "playing": False}
        if not r2.ok:
            return {"configured": True, "connected": True,
                    "error": r2.status_code, "errorMsg": r2.text[:160]}
        j2 = r2.json()
        if not j2 or not j2.get("item"):
            return {"configured": True, "connected": True, "active": False, "playing": False}
        return _parse_playback(j2)
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
        if r.status_code == 404:
            return {"ok": False, "error": "no active Spotify device - start playback once on any device"}
        return {"ok": r.ok or r.status_code == 204, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
