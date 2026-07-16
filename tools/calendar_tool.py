"""Google Calendar (read-only). Today's events for the dashboard Agenda, and a
spoken summary for Cortana. Uses the shared Google auth (see google_auth.py).
"""
import datetime

from googleapiclient.discovery import build

from tools import google_auth

_service = None


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


def today_events(max_results=12):
    """Return today's events as [{time, title, allDay, past}], time-ordered.
    `time` is HH:MM (24h) or 'all-day'; `past` marks events already ended."""
    time_min, time_max, tz = _local_day_bounds()
    res = _svc().events().list(
        calendarId="primary", timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime", maxResults=int(max_results)).execute()
    now = datetime.datetime.now().astimezone()
    out = []
    for e in res.get("items", []):
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
