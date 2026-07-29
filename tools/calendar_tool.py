"""Google Calendar (read-only). Today's events for the dashboard Agenda, and a
spoken summary for Cortana. Uses the shared Google auth (see google_auth.py).
"""
import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tools import google_auth

_service = None


def _explain(e):
    """Turn a Google API error into an actionable message. The two common 403s
    look identical to users but need different fixes."""
    s = str(e)
    low = s.lower()
    if "insufficient" in low or "scope" in low or "acl" in low:
        return ("403: your Google login didn't grant Calendar access. Fix on the "
                "box: rm ~/cortana/token.json && ./venv/bin/python main.py "
                "--google-auth  (make sure the consent screen lists Calendar). " + s[:200])
    if ("accessnotconfigured" in low or "has not been used" in low
            or "not been used in project" in low or "is disabled" in low):
        return ("403: the Google Calendar API is not enabled for your Cloud "
                "project. Enable it at console.cloud.google.com -> APIs & Services "
                "-> Enable APIs -> Google Calendar API (same project as "
                "credentials.json), wait a minute, then retry. " + s[:200])
    return s[:280]


def _svc():
    global _service
    if _service is None:
        _service = build("calendar", "v3", credentials=google_auth.creds())
    return _service


def _local_day_bounds():
    """Start/end of today in the machine's local timezone, as RFC3339 with offset."""
    now = datetime.datetime.now().astimezone()
    tz = now.tzinfo
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=1)
    return start.isoformat(), end.isoformat(), tz


# Google returns more than appointments on the primary calendar. These types
# render as time blocks in the API but are NOT things the user booked, so they
# show up as phantom slots the user can't find in Google's UI.
_SKIP_TYPES = {"workingLocation", "focusTime", "birthday", "fromGmail"}


def _is_real_event(e):
    """False for phantom slots: working-location/focus blocks, events the user
    declined, and cancelled entries."""
    if e.get("eventType") in _SKIP_TYPES:
        return False
    if e.get("status") == "cancelled":
        return False
    # Declined invitations stay in the feed - drop them, the user said no.
    for a in e.get("attendees", []) or []:
        if a.get("self") and a.get("responseStatus") == "declined":
            return False
    return True


def today_events(max_results=12):
    """Return today's events as [{time, title, allDay, past}], time-ordered.
    `time` is HH:MM (24h) or 'all-day'; `past` marks events already ended.
    Filters phantom entries (working location, focus time, declined) that the
    API returns but the user never sees as events in Google Calendar."""
    time_min, time_max, tz = _local_day_bounds()
    try:
        res = _svc().events().list(
            calendarId="primary", timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy="startTime", maxResults=int(max_results),
            showDeleted=False).execute()
    except HttpError as e:
        raise RuntimeError(_explain(e)) from None
    now = datetime.datetime.now().astimezone()
    out = []
    for e in res.get("items", []):
        if not _is_real_event(e):
            continue
        start = e.get("start", {})
        title = (e.get("summary") or "(no title)").strip()
        if start.get("date"):                       # all-day event
            out.append({"time": "all-day", "title": title, "allDay": True, "past": False})
            continue
        dt = datetime.datetime.fromisoformat(start["dateTime"]).astimezone()
        end = e.get("end", {}).get("dateTime")
        past = bool(end) and datetime.datetime.fromisoformat(end).astimezone() < now
        out.append({"time": dt.strftime("%H:%M"), "title": title,
                    "allDay": False, "past": past})
    return out


def summary_line():
    """Short spoken summary of what's left today (for Cortana)."""
    try:
        evs = [e for e in today_events() if not e["past"]]
    except Exception as e:
        return f"Couldn't reach your calendar: {e}"
    if not evs:
        return "Nothing left on your calendar today."
    parts = [f"{e['title']} at {e['time']}" if not e["allDay"] else f"{e['title']} all day"
             for e in evs[:4]]
    return "Today: " + "; ".join(parts) + "."
