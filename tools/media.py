"""Media as a voice tool: one `media` action verb over Spotify, with the local
machine's own players as the fallback.

WHY THIS EXISTS
The dashboard (Dashboard/app/spotify.js) and the phone (bridge/spotify_link.py)
both have full transport. The voice lead had none, so "pause the music" became a
shell one-liner or nothing at all. This is the third client of the same grant.

THE SHARED QUOTA IS THE WHOLE DESIGN CONSTRAINT
Three processes now spend one Spotify rate limit. The token files
(Dashboard/app/spotify.json + spotify_token.json) and the cool-off file
(spotify_backoff.json) are read and written here in EXACTLY the format the other
two use - epoch SECONDS in `until`, plus `reason` and `at` - so a 429 earned by
any one of them silences all three for precisely the Retry-After Spotify asked
for. Nothing here invents a second backoff mechanism; there is one file and it
is authoritative.

One deliberate difference from the other two: _set_backoff() never SHORTENS a
cool-off that is already running. A 429 answered with Retry-After: 5 while a
300-second cool-off is in force must not release the other two processes early.
The other two overwrite unconditionally; that is a one-line change for their
owners, and this side is safe either way.

Refresh-token rotation race (documented in bridge/spotify_link.py): any of the
three may refresh. So the token file is re-read immediately before the POST and
written back atomically, and if Spotify rejects the refresh token we re-read
once - if somebody else's newer pair is on disk we lost the race and retry with
theirs, and if it is unchanged the grant is genuinely dead and we stop rather
than burn shared quota in a loop.

NOT SPOTIFY
Anything else making noise on this box - YouTube in a browser, VLC - speaks
MPRIS, and playerctl is the only client for it. playerctl is NOT installed on
the runtime box, so every one of those paths degrades to a spoken sentence that
says so. "pause" therefore means "pause whatever is playing": Spotify gets the
press only when Spotify is what is actually playing.

Volume goes through pactl to the DEFAULT SINK, not through Spotify. The master
sink covers spotifyd, the browser and VLC at once, answers instantly, and costs
no quota. It also cannot collide with audio_ducking.py, which owns sink-INPUT
volumes and restores the exact value it captured - a second writer there gets
silently reverted the moment ducking releases.

Everything returned from here is spoken aloud, so every return value is a short
prose sentence. No dicts, no status codes, no URLs.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "Dashboard" / "app"
CONFIG_FILE = APP_DIR / "spotify.json"
TOKEN_FILE = APP_DIR / "spotify_token.json"
BACKOFF_FILE = APP_DIR / "spotify_backoff.json"    # shared with Electron + bridge
# Written by Dashboard/app/spotify.js. Read-only here - one writer per state
# file, same rule as every other state file in this repo. It exists because
# Spotify answers 204 once a device idles out, which is indistinguishable
# from "nothing loaded" unless somebody remembered.
LAST_FILE = APP_DIR / "spotify_last.json"
# A track from yesterday is not an answer to "what's playing". Matches the
# window Dashboard/app/spotify.js keeps it for.
LAST_MAX_AGE = 24 * 3600

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
HTTP_TIMEOUT = 8          # Spotify answers well inside a second when it is up

# The Spotify Connect endpoint spotifyd advertises on this workstation (see
# Dashboard/spotifyd.conf). Hardcoded to match spotify.js rather than made
# configurable: the two must agree, and a name that drifts between them sends
# the music to whichever device Spotify last thought was active - in practice
# the phone in your pocket.
CORTANA_DEVICE = "Cortana"
DEVICE_TTL = 60.0

SINK = "@DEFAULT_SINK@"
VOLUME_STEP = "10%"

ACTIONS = ("play", "pause", "next", "previous", "status", "play_query", "volume")

# Spoken synonyms the model reaches for. Deliberately short - this is not a
# natural-language layer, it exists so "skip" does not become a router error the
# user hears as a refusal.
_ALIASES = {"resume": "play", "unpause": "play", "stop": "pause",
            "skip": "next", "forward": "next", "back": "previous",
            "prev": "previous", "search": "play_query", "now_playing": "status"}

# Discovered device id and when it goes stale. Cached because it sits in front
# of every press and the account is already close to the limit.
_device = {"id": None, "until": 0.0}


class _SpotifyDown(Exception):
    """Carries the SENTENCE to speak, not a status code.

    Every Spotify failure - no config, no token, rate limited, no device, socket
    refused - ends the action immediately with something the user can act on.
    Threading error tuples back up through four call layers is how half of those
    cases end up spoken aloud as "None"."""


# -- files the three processes share ----------------------------------------
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
    """tempfile + os.replace, same discipline as hud_state.py. Two other
    processes read these files on a poll loop; a half-written token file caught
    at the wrong moment is an auth failure they cannot diagnose."""
    try:
        tmp = tempfile.NamedTemporaryFile("w", dir=path.parent,
                                          delete=False, suffix=".tmp")
        json.dump(payload, tmp)
        tmp.close()
        os.replace(tmp.name, path)
        return True
    except Exception:
        return False


def backoff_remaining():
    """Seconds left on the SHARED cool-off, 0 when clear."""
    try:
        until = float(json.loads(BACKOFF_FILE.read_text()).get("until", 0))
    except Exception:
        return 0.0
    return max(0.0, until - time.time())


def _set_backoff(seconds, reason=""):
    """Extend the shared cool-off. Never shortens one already in force - see the
    module docstring."""
    until = time.time() + max(1.0, float(seconds))
    if until <= time.time() + backoff_remaining():
        return
    _atomic_write(BACKOFF_FILE, {"until": until, "reason": reason,
                                 "at": time.time()})


def _note_429(resp):
    """Honour Retry-After; 30s when the header is missing or junk."""
    try:
        wait = float(resp.headers.get("Retry-After", "") or 30)
    except (ValueError, TypeError, AttributeError):
        wait = 30.0
    _set_backoff(wait, "429 from Spotify")
    return wait


def _cooling_sentence(wait):
    return (f"Spotify is rate limited right now - I can try again in about "
            f"{max(1, int(round(wait)))} seconds.")


# -- the network, behind one door -------------------------------------------
def _http(method, url, **kw):
    """The ONLY place this module touches the network.

    Two reasons everything funnels through here. `requests` stays a lazy import,
    per the convention stated in agents.py - CI installs pytest, python-dotenv
    and python-dateutil and nothing else, so importing it at module scope would
    fail collection of the whole suite. And the tests get exactly one seam to
    monkeypatch, which means no test in test_media.py can reach the real Spotify
    even by accident."""
    import requests
    kw.setdefault("timeout", HTTP_TIMEOUT)
    return requests.request(method, url, **kw)


def _json(r):
    """Response body as a dict, {} when there isn't a readable one.

    The case that matters is a 200 carrying something that is not JSON - a
    captive portal, a proxy error page, a truncated body. requests' .json()
    raises then, and an unguarded call site turns that into a traceback the
    user hears as dead air."""
    try:
        return r.json() or {}
    except Exception:
        return {}


def _request(method, url, **kw):
    """_http plus the two failures every caller handles identically."""
    try:
        r = _http(method, url, **kw)
    except Exception as e:
        # str(e) on a requests transport error is a urllib3 dump: host, port,
        # errno, retry counts, a repr with a memory address. This sentence is
        # read ALOUD, so the detail goes to the journal and the user gets prose
        # - the same split audio_ducking.py makes when pactl fails.
        print("[media] spotify request failed:", type(e).__name__, e)
        raise _SpotifyDown("I couldn't reach Spotify just then - the network or "
                           "the service looks to be down.")
    if getattr(r, "status_code", 0) == 429:
        raise _SpotifyDown(_cooling_sentence(_note_429(r)))
    return r


# -- auth -------------------------------------------------------------------
def _refresh(retry=True):
    t = _load_token()      # re-read: the dashboard or bridge may have rotated it
    if not t or not t.get("refresh_token"):
        return None
    tried = t["refresh_token"]
    r = _request("POST", TOKEN_URL,
                 data={"grant_type": "refresh_token", "refresh_token": tried,
                       "client_id": _client_id()})
    if not getattr(r, "ok", False):
        # Rejected. Either we lost the rotation race - somebody refreshed
        # between our read and this POST, invalidating the token we sent - or
        # the grant is actually dead. Re-read once to tell them apart: a
        # DIFFERENT refresh token on disk means it was the race and theirs will
        # work. An unchanged one means a retry only burns shared quota.
        if retry:
            other = (_load_token() or {}).get("refresh_token")
            if other and other != tried:
                return _refresh(retry=False)
        return None
    try:
        j = r.json()
        nt = {"access_token": j["access_token"],
              # A refresh response usually omits both of these. Dropping the
              # refresh token logs all three processes out; dropping the scope
              # makes a later scope shortfall indistinguishable from a
              # Premium 403.
              "refresh_token": j.get("refresh_token") or tried,
              "scope": j.get("scope") or t.get("scope", ""),
              "expires_at": int(time.time() * 1000) + int(j["expires_in"]) * 1000}
    except Exception:
        return None
    _atomic_write(TOKEN_FILE, nt)
    return nt


def _access_token():
    t = _load_token()
    if not t:
        return None
    if time.time() * 1000 > (t.get("expires_at") or 0) - 60000:
        t = _refresh() or _load_token()   # lost the race? take whatever they wrote
        if not t or time.time() * 1000 > (t.get("expires_at") or 0) - 5000:
            return None
    return t.get("access_token")


def _need_token():
    """An access token, or a sentence naming the setup step that is missing."""
    if not _client_id():
        raise _SpotifyDown("Spotify isn't set up yet - the client ID still needs "
                           "filling in on the dashboard.")
    at = _access_token()
    if not at:
        wait = backoff_remaining()        # a 429 during the refresh sets this
        if wait > 0:
            raise _SpotifyDown(_cooling_sentence(wait))
        raise _SpotifyDown("Spotify isn't connected. Connect it once on the "
                           "dashboard and I'll be able to drive it from here.")
    return at


def _guard():
    """Refuse to spend a request while the shared cool-off is running."""
    wait = backoff_remaining()
    if wait > 0:
        raise _SpotifyDown(_cooling_sentence(wait))


def _call(method, path, at, **kw):
    return _request(method, API + path,
                    headers={"Authorization": "Bearer " + at}, **kw)


# -- Spotify ----------------------------------------------------------------
def _device_id(at):
    """Id of the local spotifyd endpoint, or None when it isn't running.

    Without this the Web API acts on whatever device Spotify last considered
    active, which for this account is the phone - saying "play" at the desk
    started the music in your pocket."""
    if _device["id"] and time.time() < _device["until"]:
        return _device["id"]
    r = _call("GET", "/me/player/devices", at)
    if not getattr(r, "ok", False):
        return None
    devices = _json(r).get("devices")
    if devices is None:
        # Unreadable body. Distinct from a real empty list: caching "no local
        # device" for a full TTL because one response was garbled would send a
        # minute of presses to the phone.
        return None
    want = CORTANA_DEVICE.lower()
    match = next((d for d in devices
                  if str(d.get("name", "")).lower() == want), None)
    _device["id"] = match.get("id") if match else None
    _device["until"] = time.time() + DEVICE_TTL
    return _device["id"]


def _target(at):
    """Query params aiming a press at the local device, or {} to let Spotify
    pick. Empty is the correct fallback when spotifyd is stopped: it keeps
    working instead of failing closed."""
    try:
        did = _device_id(at)
    except _SpotifyDown:
        raise
    except Exception:
        did = None
    return {"device_id": did} if did else {}


def _playback(at):
    """What Spotify believes is happening, or None when no device is active.

    ONE request, deliberately. spotify.js and spotify_link.py each pay a second
    one for the cross-device fallback because they drive a live UI on a loop. A
    voice turn is one question asked once, so a 204 is answered honestly as
    "nothing I can see" rather than by spending another slot of a quota three
    processes share."""
    r = _call("GET", "/me/player", at)
    if getattr(r, "status_code", 0) == 204:
        return None
    if not getattr(r, "ok", False):
        raise _SpotifyDown(_http_sentence(r))
    j = _json(r)
    item = j.get("item")
    if not item:
        return None
    dev = j.get("device") or {}
    # The id, not just the name: a press aimed at the local device while
    # Spotify is actually playing on the phone is refused with a 403 that reads
    # as a Premium problem. Whoever is playing is who we press.
    return {"playing": bool(j.get("is_playing")),
            "track": item.get("name", ""),
            "artist": ", ".join(a.get("name", "") for a in item.get("artists", [])),
            "device": dev.get("name", ""),
            "device_id": dev.get("id") or None}


# Everything the playback endpoints need. Mirrors SCOPES in
# Dashboard/app/spotify.js, which is what actually asks for them.
PLAY_SCOPES = ("user-read-playback-state", "user-modify-playback-state",
               "user-read-currently-playing")


def _err_detail(r):
    """Spotify's own words for a failure: {"error":{"message","reason"}}.

    Worth the parse. A 403 from the player endpoints is USUALLY "Player command
    failed: Restriction violated", which has nothing to do with Premium - but
    this function did not exist, so every 403 was reported as a Premium problem
    and sent the user off checking a subscription that was fine all along.
    """
    try:
        j = r.json()
        err = (j or {}).get("error") or {}
        msg = err.get("message") or ""
        reason = err.get("reason") or ""
        if msg and reason and reason.lower() not in msg.lower():
            return f"{msg} ({reason})"
        return msg or reason
    except Exception:
        try:
            return (getattr(r, "text", "") or "").strip()[:160]
        except Exception:
            return ""


def _missing_scopes():
    """Playback scopes the stored grant does NOT carry."""
    scope = str((_load_token() or {}).get("scope") or "")
    return [s for s in PLAY_SCOPES if s not in scope]


def _http_sentence(r):
    code = getattr(r, "status_code", 0)
    if code == 401:
        return "Spotify rejected my login - it needs reconnecting on the dashboard."
    if code == 403:
        # Check the grant BEFORE quoting Spotify: a scope shortfall is the one
        # 403 with an action attached, and it is indistinguishable from the
        # others by message alone.
        missing = _missing_scopes()
        if missing:
            return ("Spotify refused that, and the connection is missing the "
                    f"{missing[0]} permission. Reconnect Spotify on the dashboard "
                    "to re-grant it.")
        detail = _err_detail(r)
        if detail:
            # Verbatim. Guessing at the cause is what produced a whole
            # investigation into a Premium subscription that was never expired.
            return f"Spotify refused that: {detail}."
        return ("Spotify refused that and gave no reason. If the desk speaker "
                "is idle, naming a track works where a bare play does not.")
    if code == 404:
        return "There's no active Spotify device - is cortana-spotifyd running?"
    return f"Spotify answered {code}."


def _played(r):
    """True when a transport press landed. 204 is Spotify's success for these."""
    return bool(getattr(r, "ok", False)) or getattr(r, "status_code", 0) == 204


def _press(action, at, device=None, body=None):
    """One transport press. Aimed at `device` when the caller already knows
    which endpoint is playing, otherwise at the local spotifyd device."""
    verbs = {"play": ("PUT", "/me/player/play"),
             "pause": ("PUT", "/me/player/pause"),
             "next": ("POST", "/me/player/next"),
             "previous": ("POST", "/me/player/previous")}
    method, path = verbs[action]
    params = {"device_id": device} if device else _target(at)
    kw = {"json": body} if body is not None else {}
    r = _call(method, path, at, params=params, **kw)
    code = getattr(r, "status_code", 0)
    if code == 404:
        # The cached id outlives the device when spotifyd restarts or the box
        # sleeps. Drop it so the next press rediscovers instead of retrying a
        # dead endpoint for a full TTL.
        _device.update(id=None, until=0.0)
    if not _played(r) and params and code in (403, 404):
        # We aimed at a device that will not take the press: spotifyd went away
        # (404), or it is registered but idle while the phone is the thing
        # actually playing (403 restriction violated). Spend ONE more request
        # untargeted and let Spotify act on whatever is genuinely active. Only
        # on a targeted press, so the failure case cannot loop.
        r = _call(method, path, at, params={}, **kw)
    if not _played(r) and action == "play" and             getattr(r, "status_code", 0) in (403, 404):
        # Still refused, and this is a play. The remaining explanation is a
        # registered-but-idle device with no context: it will not accept a play
        # until something makes it the ACTIVE device. Transfer, then re-press.
        #
        # This lives here rather than in _do_play so that play_query gets it
        # too. Naming a track was the case that kept failing after bare play
        # was fixed - same cause, different entry point, and putting the repair
        # in each action is how you end up fixing it three times.
        did = _device_id(at)
        if did:
            if _transfer(at, did, play=False):
                r = _call(method, path, at, params={"device_id": did}, **kw)
    if not _played(r):
        raise _SpotifyDown(_http_sentence(r))
    return True


# -- everything that is not Spotify -----------------------------------------
def _transfer(at, did, play=True):
    """Move playback onto `did` and start it. The only call that wakes an IDLE
    device.

    PUT /me/player/play asks a device to resume ITS OWN context. A spotifyd
    endpoint that has never played has no context, so that request returns 204
    and NOTHING HAPPENS - a silent success. That is the whole reason "play" did
    nothing at the desk unless the phone had already started something: the
    press was fine, there was simply nothing for it to resume.
    """
    r = _call("PUT", "/me/player", at, json={"device_ids": [did], "play": play})
    return _played(r)


def _mpris(args):
    """(ok, text) from playerctl - the only MPRIS client that would reach a
    browser tab or VLC on this box.

    It is NOT installed on the runtime box (measured), so the honest answer here
    is a sentence naming the missing binary, the same way voice/mic.py refuses
    rather than crashing."""
    if not shutil.which("playerctl"):
        return False, ("playerctl isn't installed on this machine, so Spotify is "
                       "the only player I can reach. Installing playerctl would "
                       "let me control the browser and VLC too.")
    try:
        p = subprocess.run(["playerctl"] + list(args),
                           capture_output=True, text=True, timeout=3)
    except Exception as e:
        return False, "playerctl didn't answer - " + str(e)[:80]
    if p.returncode != 0:
        return False, "There's nothing playing that I can reach."
    return True, (p.stdout or "").strip()


def _local_state():
    """"Playing" / "Paused" / "Stopped" from the local MPRIS player, or None.

    This is the whole speed fix. A Spotify press costs 2-3 HTTPS round trips to
    api.spotify.com (token check, /me/player read, then the press itself), which
    is one to two seconds before she even opens her mouth. playerctl is a local
    D-Bus call answering in single-digit milliseconds, so asking it FIRST makes
    controlling a browser tab instant and costs the Spotify path ~5ms.

    It also fixes a correctness problem, not just latency: with music on the
    phone via Connect and a video in the browser, "pause" previously always
    meant Spotify, so it silenced the wrong thing and reported success.
    """
    if not shutil.which("playerctl"):
        return None
    try:
        p = subprocess.run(["playerctl", "status"],
                           capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    if p.returncode != 0:
        return None                       # no MPRIS player on the bus at all
    return (p.stdout or "").strip() or None


def _pactl(args):
    if not shutil.which("pactl"):
        return False
    try:
        p = subprocess.run(["pactl"] + list(args),
                           capture_output=True, text=True, timeout=3)
        return p.returncode == 0
    except Exception:
        return False


# -- actions ----------------------------------------------------------------
def _do_play(player="auto"):
    # A locally PAUSED player is what "play" means when there is one - resuming
    # the video you just paused, not waking Spotify on the phone.
    if player == "local" or (player == "auto" and _local_state() == "Paused"):
        ok, said = _mpris(["play"])
        if ok:
            return "Playing."
        if player == "local":
            return said
    if player == "local":
        return "There's no local player paused that I can resume."
    try:
        _guard()
        at = _need_token()
        now = _playback(at)
        if now and now["playing"]:
            return "Already playing."
        if now:
            # Loaded and paused somewhere - resume it where it already is,
            # rather than dragging it to a device the user did not ask for.
            _press("play", at, device=now.get("device_id"))
            return "Playing."
        # Nothing active anywhere. This is the case that used to do nothing at
        # all: wake the desk speaker explicitly instead of asking an idle
        # device to resume a context it does not have.
        did = _device_id(at)
        if did and _transfer(at, did):
            # One confirming read, only on this cold path. A transfer succeeds
            # even when the account has no context to give the speaker, and
            # reporting "Playing." into a silent room is the failure this whole
            # change exists to remove.
            after = _playback(at)
            if after and after["playing"]:
                what = " by ".join(x for x in (after["track"], after["artist"]) if x)
                return f"Playing {what}." if what else "Playing."
            return ("I woke the desk speaker, but Spotify has nothing queued to "
                    "resume. Tell me what to play.")
        return ("Spotify has nothing playing and I can't see the desk speaker. "
                "Tell me what to play, or check spotifyd is running.")
    except _SpotifyDown as e:
        ok, said = _mpris(["play"])
        return "Playing." if ok else f"{e} {said}"


def _do_pause(player="auto"):
    """Pause whatever is making the noise.

    Order matters. The local player is asked first because it can answer in
    milliseconds and because, when a browser tab is the thing talking, it is
    also the right answer - pausing Spotify instead would silence nothing and
    cheerfully report success."""
    if player == "local" or (player == "auto" and _local_state() == "Playing"):
        ok, said = _mpris(["pause"])
        if ok:
            return "Paused."
        if player == "local":
            return said
    if player == "local":
        return "Nothing is playing locally that I can reach."

    spotify_seen = False
    try:
        _guard()
        at = _need_token()
        if player == "spotify":
            # Named explicitly, so skip the /me/player read that only existed to
            # decide Spotify-vs-local. One request instead of two.
            _press("pause", at)
            return "Paused."
        now = _playback(at)
        spotify_seen = now is not None
        if now and now["playing"]:
            # Press the device that is playing, which is not always the local
            # one. _playback just told us, so this costs nothing extra.
            _press("pause", at, device=now.get("device_id"))
            return "Paused."
    except _SpotifyDown as e:
        ok, said = _mpris(["pause"])
        return "Paused." if ok else f"{e} {said}"
    ok, said = _mpris(["pause"])
    if ok:
        return "Paused."
    if spotify_seen:
        return "Spotify is already paused."
    return "Nothing is playing on Spotify. " + said


def _do_skip(action, player="auto"):
    if player == "local" or (player == "auto" and _local_state() == "Playing"):
        ok, said = _mpris([action if action == "next" else "previous"])
        if ok:
            return "Skipped forward." if action == "next" else "Back one track."
        if player == "local":
            return said
    return _do_skip_spotify(action)


def _do_skip_spotify(action):
    done = "Skipped forward." if action == "next" else "Back one track."
    try:
        _guard()
        at = _need_token()
        _press(action, at)
        return done
    except _SpotifyDown as e:
        ok, _ = _mpris([action])
        return done if ok else str(e)


def _last_track():
    """What Spotify last had loaded, from the dashboard's record, or None.

    Spotify reports 204 within a minute or two of pausing, so without this
    "what's playing" answered "nothing" while a paused track sat there - which
    is the wrong answer even though the API technically said so."""
    try:
        j = json.loads(LAST_FILE.read_text())
    except Exception:
        return None
    if not j.get("track") or time.time() - (j.get("at", 0) / 1000.0) > LAST_MAX_AGE:
        return None
    return j


def _spotify_clause():
    """One clause about Spotify, whatever state it is in. Never raises."""
    try:
        _guard()
        at = _need_token()
        now = _playback(at)
    except _SpotifyDown as e:
        return str(e).rstrip(".")
    except Exception:
        return "I couldn't reach Spotify"
    if now:
        what = " by ".join(x for x in (now["track"], now["artist"]) if x) or "something"
        where = f" on {now['device']}" if now.get("device") else ""
        return (f"Spotify is playing {what}{where}" if now["playing"]
                else f"Spotify is paused on {what}{where}")
    # 204: no active device. A remembered track still answers the question
    # honestly, and is what the user actually wants to hear.
    last = _last_track()
    if last:
        artist = last.get("artist") or ""
        what = " by ".join(x for x in (last.get("track", ""), artist) if x)
        return f"Spotify is paused on {what}"
    return "Spotify has nothing loaded"


def _local_clause():
    """(clause, can_see) about whatever else on this machine makes noise.

    can_see=False means playerctl is not installed, so silence here is IGNORANCE
    and not evidence. Reporting "nothing else is playing" off the back of a
    missing binary would be exactly the kind of confident wrong answer this
    repo keeps getting bitten by."""
    if not shutil.which("playerctl"):
        return "", False
    state = _local_state()
    if state is None:
        return "nothing else is loaded here", True
    ok, out = _mpris(["metadata", "--format", "{{artist}} - {{title}}"])
    title = (out or "").strip(" -") if ok else ""
    if state == "Playing":
        return (f"{title} is playing here" if title else "something is playing here"), True
    if state == "Paused":
        return (f"{title} is paused here" if title else "something is paused here"), True
    return "nothing else is loaded here", True


def _do_status(player="auto"):
    """Answer for EVERY source, not the first one that says yes.

    The local-first shortcut used for transport presses is wrong for a
    question: a YouTube tab playing does not mean Spotify is silent, and
    reporting only one of them was reporting half the room.
    """
    if player == "local":
        clause, can_see = _local_clause()
        if not can_see:
            return ("playerctl isn't installed, so Spotify is the only player I "
                    "can see.")
        return clause.capitalize() + "."
    if player == "spotify":
        return _spotify_clause() + "."

    spotify = _spotify_clause()
    local, can_see = _local_clause()
    if can_see:
        return f"{spotify}, and {local}."
    # Only own up to the blind spot when the answer would otherwise be a bare
    # "nothing" - saying it every time a track IS playing is noise.
    if "nothing loaded" in spotify:
        return (spotify + ", and playerctl isn't installed so I can't see any "
                "other player.")
    return spotify + "."



def _pick(j):
    """(play body, spoken label) for the best search hit.

    Tracks win when there is one, because "play X" means a song far more often
    than it means an artist page, and a wrong track is one "next" away from
    fixed. Album, artist and playlist are context_uri fallbacks in that order -
    an artist context shuffles their popular tracks, which is the right reading
    of "play some Radiohead" when no track title matched."""
    tracks = [t for t in ((j.get("tracks") or {}).get("items") or []) if t]
    if tracks and tracks[0].get("uri"):
        t = tracks[0]
        who = ", ".join(a.get("name", "") for a in t.get("artists", []))
        return ({"uris": [t["uri"]]},
                t.get("name", "that") + (f" by {who}" if who else ""))
    for key, shape in (("albums", "the album {}"), ("artists", "{}"),
                       ("playlists", "the playlist {}")):
        # Spotify returns literal nulls inside these item lists often enough
        # that an unfiltered [0] is a real AttributeError, not a hypothetical.
        items = [x for x in ((j.get(key) or {}).get("items") or []) if x]
        if items and items[0].get("uri"):
            return ({"context_uri": items[0]["uri"]},
                    shape.format(items[0].get("name", "that")))
    return None, ""


def _do_play_query(query):
    query = (query or "").strip()
    if not query:
        return "What would you like me to play?"
    try:
        _guard()
        at = _need_token()
        r = _call("GET", "/search", at,
                  params={"q": query, "type": "track,album,artist,playlist",
                          # market matters: without it search happily returns
                          # tracks this account cannot play, and the press then
                          # fails for a reason nobody watching can see.
                          "market": "from_token", "limit": 3})
        if not getattr(r, "ok", False):
            raise _SpotifyDown(_http_sentence(r))
        found = _json(r)
        if not found:
            # An empty dict here is a body that would not parse, NOT a search
            # with no hits - a real miss still carries {"tracks": {"items": []}}.
            # Reporting it as "nothing found" would send the user off inventing
            # different words for a query that was never actually run.
            raise _SpotifyDown("Spotify's answer to that search didn't make "
                               "sense to me - worth trying again in a moment.")
        body, label = _pick(found)
        if not body:
            return f"I couldn't find anything on Spotify for {query}."
        # Through _press, so this path gets the same cache-drop and untargeted
        # retry the transport buttons get instead of its own copy of them.
        _press("play", at, body=body)
        return f"Playing {label}."
    except _SpotifyDown as e:
        return str(e)


def _do_volume(percent, word):
    """System volume, not Spotify's.

    The default sink covers spotifyd, the browser and VLC in one press, costs no
    Spotify quota, and keeps working while the shared cool-off is running. It
    also stays clear of audio_ducking.py, which owns sink-INPUT volumes and
    restores the exact value it captured - a second writer there is silently
    reverted the moment ducking releases."""
    word = (word or "").strip().lower()
    if not shutil.which("pactl"):
        return "pactl isn't installed, so I can't change the volume from here."
    if "unmute" in word:
        return "Unmuted." if _pactl(["set-sink-mute", SINK, "0"]) else _vol_failed()
    if "mute" in word:
        return "Muted." if _pactl(["set-sink-mute", SINK, "1"]) else _vol_failed()
    if percent is not None:
        try:
            pct = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            return "I need a number between zero and a hundred for the volume."
        # Unmute first: setting a level on a muted sink changes nothing audible,
        # which reads to the user as the command having been ignored.
        _pactl(["set-sink-mute", SINK, "0"])
        return (f"Volume {pct} percent."
                if _pactl(["set-sink-volume", SINK, f"{pct}%"]) else _vol_failed())
    if "up" in word or "louder" in word:
        return ("Turned it up." if _pactl(["set-sink-volume", SINK, "+" + VOLUME_STEP])
                else _vol_failed())
    if "down" in word or "quieter" in word or "lower" in word:
        return ("Turned it down." if _pactl(["set-sink-volume", SINK, "-" + VOLUME_STEP])
                else _vol_failed())
    return "Tell me a level from zero to a hundred, or say up, down or mute."


def _vol_failed():
    return "The volume control didn't take - PulseAudio may not be running."


def media(action, query="", percent=None, player="auto"):
    """The whole tool. Always returns a string; never raises.

    Same contract as tools/desktop.py, and for the same reason: the return
    value is read aloud. Every action below already turns the EXPECTED failures
    into prose, but an unexpected one - a payload shaped differently from the
    docs, a bad argument from the model - would otherwise surface as
    "TOOL ERROR (media): ..." spoken at the user verbatim."""
    try:
        return _media(action, query, percent, player)
    except Exception as e:
        print("[media] unhandled:", type(e).__name__, e)
        return "The media control hit a problem I didn't expect - it's in the log."


def _media(action, query="", percent=None, player="auto"):
    player = (player or "auto").strip().lower()
    if player in ("youtube", "browser", "chrome", "firefox", "vlc", "mpris"):
        player = "local"          # what the user actually says out loud
    if player not in ("auto", "local", "spotify"):
        player = "auto"
    key = (action or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = _ALIASES.get(key, key)
    if key not in ACTIONS:
        return ("I can play, pause, skip to the next or previous track, tell you "
                "what's playing, search for something to play, or set the volume.")
    if key == "volume":
        return _do_volume(percent, query)
    if key == "play_query":
        return _do_play_query(query)
    if key == "status":
        return _do_status(player)
    if key == "pause":
        return _do_pause(player)
    if key in ("next", "previous"):
        return _do_skip(key, player)
    # A bare "play" carrying a query is what the model does when it means
    # play_query; honouring it beats resuming whatever was on before.
    if (query or "").strip():
        return _do_play_query(query)
    return _do_play()
