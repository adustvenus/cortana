"""Where the user is, merged from the three things this process can see.

Three signals, none of them sufficient alone:

  desk    presence_desk.json, written ONLY by the cortana process (it has
          DISPLAY and XAUTHORITY; this service has neither, so an X11 idle
          probe from here reads as "away" and would be a lie). Read, never
          written - one writer per state file.
  phone   POST /api/presence: a coarse place, plus driving/charging/screen.
  socket  hub.online_idents(). LinkClient is FOREGROUND-ONLY, so a live socket
          means the user is literally looking at the phone right now. It is the
          strongest presence signal in the system and it costs nothing.

Coordinates are not kept. The phone sends lat/lon so the home/out decision can
be made against a radius that lives on the workstation rather than shipped to
the app, and what survives the call is a coarse place and nothing else - no
history, no track, no raw fix in the log line.

Two staleness rules, both copied from presence.read_desk() because the failure
they prevent is identical: an old reading degrades to `unknown`, never to its
last value, and nothing here may ever infer "asleep".
"""
import json
import math
import time

import presence
from bridge import hub
from bridge.settings import HOME_LAT, HOME_LON, HOME_RADIUS_M, log

# A phone report older than this tells us nothing about where you are now.
PHONE_STALE = 1800
# ...but it still says the app was alive recently, which is a weaker, separate
# claim and the one the `phone` field reports.
PHONE_RECENT = 900

MARK_KEY = "presence_phone"     # memory.meta, not kv: kv is read into the prompt

_report = {"data": None, "loaded": False}


# -- deriving a place ------------------------------------------------------
def _meters(lat1, lon1, lat2, lon2):
    """Equirectangular approximation. Good to a couple of metres at the scale
    of "is this my house", needs no numpy, and cannot raise on a pole."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371000.0 * math.hypot(dlat, dlon)


def home_configured():
    return bool(HOME_LAT or HOME_LON)


def coarse_place(lat, lon, said="", driving=False):
    """The only thing derived from a fix, and the only thing kept.

    The phone's own label wins when it has one - it can see geofences and
    Wi-Fi SSIDs that a lat/lon comparison here cannot. Coordinates are the
    fallback, and with no home fix configured the honest answer is `unknown`
    rather than a guess.
    """
    if driving:
        return "driving"
    said = str(said or "").strip().lower()
    if said in ("home", "out", "driving"):
        return said
    if lat is None or lon is None or not home_configured():
        return "unknown"
    try:
        return "home" if _meters(float(lat), float(lon), HOME_LAT, HOME_LON) \
                         <= HOME_RADIUS_M else "out"
    except (TypeError, ValueError):
        return "unknown"


def record(payload, now=None):
    """Accept one report from the phone. Blocking (sqlite) - call in a thread.

    Returns the stored dict. The raw fix is used here and then dropped; it is
    never written to state.db and never logged.
    """
    now = now or time.time()
    payload = payload if isinstance(payload, dict) else {}
    driving = bool(payload.get("driving"))
    kept = {"place": coarse_place(payload.get("lat"), payload.get("lon"),
                                  payload.get("place"), driving),
            "driving": driving,
            "charging": bool(payload.get("charging")),
            "screenOn": bool(payload.get("screenOn")),
            "zone": str(payload.get("zone") or "unknown").strip().lower(),
            "ts": now}
    _report["data"], _report["loaded"] = kept, True
    try:
        import memory
        memory.meta_set(MARK_KEY, json.dumps(kept))
    except Exception as e:
        # Only the survival of this reading across a bridge restart is lost,
        # and cortana-bridge.service restarts often enough for that to matter -
        # so it is worth a line, and not worth failing the request over.
        log("phone presence not persisted", e)
    return kept


def report(now=None):
    """Last phone report, from memory or - once, after a restart - from
    memory.meta. None when there has never been one."""
    if not _report["loaded"]:
        _report["loaded"] = True
        try:
            import memory
            raw = memory.meta_get(MARK_KEY, "")
            data = json.loads(raw) if raw else None
            _report["data"] = data if isinstance(data, dict) else None
        except Exception:
            _report["data"] = None
    return _report["data"]


# -- the merge -------------------------------------------------------------
def merge(desk="unknown", online=False, phone=None, now=None):
    """The whole policy, as a pure function so the tests can pin every input
    combination. `online` is "this device has a live WebSocket right now"."""
    now = now or time.time()
    phone = phone if isinstance(phone, dict) else None
    age = now - float((phone or {}).get("ts") or 0) if phone else None

    # `online` USED to mean "the user is looking at the phone", because
    # LinkClient held the socket only while an Activity was foregrounded. The
    # v2.5.0 foreground service holds it with the app closed, so that inference
    # is now permanently true and reported "open" forever. screenOn is what
    # still carries the original meaning - the phone sends it on every report
    # and, until now, nothing on this side ever read it back.
    fresh = age is not None and age <= PHONE_RECENT
    if fresh and (phone or {}).get("screenOn"):
        phone_state = "open"
    elif online or fresh:
        # Reachable, but nobody is looking at it.
        phone_state = "recent"
    else:
        phone_state = "closed"

    if phone is None or age is None or age > PHONE_STALE:
        place = "unknown"
    else:
        place = phone.get("place") or "unknown"
    if place not in ("home", "out", "driving", "unknown"):
        place = "unknown"

    # The phone knows home from work; `place` cannot say so, because its
    # vocabulary is home/out/driving/unknown and Presence.kt collapses "work"
    # into "out" before it goes on the wire. `zone` is the phone's own label
    # from its saved places, and it was being dropped on the floor here - which
    # is why setting a work location appeared to do nothing at all.
    zone = (phone or {}).get("zone") or "unknown"
    if zone not in ("home", "work", "elsewhere", "unknown"):
        zone = "unknown"

    return {"desk": desk if desk in ("present", "away", "asleep") else "unknown",
            "phone": phone_state,
            "place": place,
            "zone": zone,
            "driving": place == "driving",
            "charging": bool((phone or {}).get("charging")),
            "reportAge": int(age) if age is not None else None,
            "ts": now}


def snapshot(now=None):
    """The /local/presence answer. Does file and sqlite IO - call in a thread."""
    try:
        desk = presence.read_desk(now)
    except Exception as e:
        log("desk presence unreadable", e)
        desk = "unknown"
    return merge(desk=desk, online=bool(hub.online_idents()),
                 phone=report(), now=now)
