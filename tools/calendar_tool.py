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


def calendars():
    """Every calendar the user actually sees in Google Calendar, primary first.

    `selected` is the checkbox in Google's sidebar, so honouring it picks up the
    work / shared / family calendars the user lives in while leaving out ones
    they unticked. Paginated: an account can have more than one page of them.
    """
    out, page = [], None
    while True:
        res = _svc().calendarList().list(
            pageToken=page, showDeleted=False, showHidden=False,
            maxResults=250).execute()
        for c in res.get("items", []):
            if c.get("deleted") or c.get("hidden"):
                continue
            out.append({"id": c.get("id", ""), "summary": c.get("summary", ""),
                        "primary": bool(c.get("primary")),
                        "selected": bool(c.get("selected") or c.get("primary")),
                        "timeZone": c.get("timeZone", "")})
        page = res.get("nextPageToken")
        if not page:
            break
    # Primary first: when the same meeting exists on two calendars we keep the
    # copy carrying the user's own RSVP.
    out.sort(key=lambda c: (not c["primary"], c["summary"].lower()))
    return out


def raw_today(calendar_id="primary", max_results=250):
    """Unfiltered events for today from ONE calendar - what Google actually
    returns, before our phantom-slot filtering. Diagnostic use only."""
    time_min, time_max, _tz = _local_day_bounds()
    res = _svc().events().list(
        calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
        singleEvents=True, orderBy="startTime", maxResults=int(max_results),
        showDeleted=False).execute()
    return res.get("items", [])


def today_events(max_results=12, calendar_ids=None):
    """Today's events as [{time, title, allDay, past}], time-ordered.

    Reads EVERY calendar the user has selected, not just "primary" - an event
    added to a work/shared/family calendar is invisible to a primary-only query,
    which looks exactly like "the agenda missed the event I just added".

    `max_results` caps what we RETURN, applied after filtering. Google applies
    its own cap before filtering, so we fetch generously (250) and trim here;
    otherwise working-location and declined junk eats the budget and silently
    truncates real later-in-the-day events.
    """
    time_min, time_max, tz = _local_day_bounds()
    if calendar_ids is None:
        try:
            calendar_ids = [c["id"] for c in calendars() if c["selected"]]
        except Exception:
            calendar_ids = ["primary"]
    if not calendar_ids:
        calendar_ids = ["primary"]

    items, errors = [], []
    for cid in calendar_ids:
        try:
            page = None
            while True:
                res = _svc().events().list(
                    calendarId=cid, timeMin=time_min, timeMax=time_max,
                    singleEvents=True, orderBy="startTime", maxResults=250,
                    showDeleted=False, pageToken=page).execute()
                items.extend(res.get("items", []))
                page = res.get("nextPageToken")
                if not page:
                    break
        except HttpError as e:
            errors.append(f"{cid}: {_explain(e)}")
    if errors and not items:
        raise RuntimeError("; ".join(errors)[:400])

    now = datetime.datetime.now().astimezone()
    today = now.date()
    out, seen = [], set()
    for e in items:
        if not _is_real_event(e):
            continue
        # The same meeting on two calendars appears twice; iCalUID identifies it.
        key = e.get("iCalUID") or e.get("id")
        if key in seen:
            continue
        seen.add(key)
        start = e.get("start", {})
        title = (e.get("summary") or "(no title)").strip()
        if start.get("date"):                       # all-day event
            # Trust our own day window: Google decides overlap using the
            # calendar's timezone, which can differ from this machine's.
            try:
                d = datetime.date.fromisoformat(start["date"])
                end_raw = (e.get("end") or {}).get("date")
                d_end = datetime.date.fromisoformat(end_raw) if end_raw else d
                if not (d <= today < d_end or d == today):
                    continue
            except Exception:
                pass
            out.append({"time": "all-day", "title": title, "allDay": True,
                        "past": False, "_sort": -1})
            continue
        dt = datetime.datetime.fromisoformat(start["dateTime"]).astimezone()
        end = e.get("end", {}).get("dateTime")
        past = bool(end) and datetime.datetime.fromisoformat(end).astimezone() < now
        out.append({"time": dt.strftime("%H:%M"), "title": title,
                    "allDay": False, "past": past, "_sort": dt.timestamp()})
    # Merging calendars breaks each call's ordering: all-day first, then by start.
    out.sort(key=lambda x: x["_sort"])
    for x in out:
        x.pop("_sort", None)
    return out[:int(max_results)] if max_results else out


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
