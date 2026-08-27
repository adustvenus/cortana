"""Scheduler: reminders, timers, alarms and recurring jobs.

All the logic lives here; a thin tick thread (main.py in phase 1, the bridge
later) calls due() -> claim() -> fire -> advance(). Keeping it out of bridge/
is deliberate: this module is pure Python + sqlite, so the whole engine is
testable on the Windows dev box and in CI, where neither Electron nor Kotlin
nor a live systemd unit can be reached.

Two rules the rest of the file exists to enforce:

  * Exactly one ticker fires any given occurrence. Not by convention - by a
    conditional UPDATE that sqlite serialises. That is what lets both processes
    tick during a cutover without anyone firing twice.
  * A machine that was asleep for six hours wakes up and fires AT MOST ONE
    missed occurrence, not six. See roll_forward().
"""
import datetime
import json
import time
from zoneinfo import ZoneInfo

from config import (SCHED_CATCHUP, SCHED_CLAIM_STALE, SCHED_TZ)
import memory

_ACTIONS = ("say", "turn", "delegate")
_URGENCIES = ("ambient", "normal", "urgent", "critical")
# An alarm you set deliberately should wake you even if the desk thinks you are
# asleep; a timer is urgent but not worth overriding every surface.
_DEFAULT_URGENCY = {"timer": "urgent", "alarm": "critical",
                    "reminder": "normal", "routine": "normal"}


# ── time helpers ───────────────────────────────────────────────────────────
def _zone(tz=None):
    """Resolve a zone, falling back to the system's REAL offset - never to UTC.

    Defaulting to UTC on a machine whose clock is not UTC is a silent
    catastrophe: every naive "in 20 minutes" gets read as a UTC wall time and
    shifts by the whole local offset, with nothing raising. The fixed-offset
    fallback loses DST-rule fidelity, which matters only on a box with no IANA
    database (the Windows dev box) and not on the Linux runtime box.
    """
    try:
        return ZoneInfo(tz or SCHED_TZ)
    except Exception:
        return datetime.datetime.now().astimezone().tzinfo


def to_epoch(local_naive, tz=None):
    """Naive local wall-clock -> epoch. On a fall-back DST repeat this picks the
    FIRST of the two identical wall times (fold=0); on a spring-forward gap the
    stdlib maps the nonexistent time forward. Both are acceptable for an alarm
    and neither is silent."""
    return local_naive.replace(tzinfo=_zone(tz)).timestamp()


def to_local(ts, tz=None):
    return datetime.datetime.fromtimestamp(ts, _zone(tz))


def parse_when(when, tz=None):
    """ISO-8601 -> epoch. Offset-aware strings are honoured as given; naive ones
    are interpreted in the scheduler's zone, which is what the model produces
    when it reads the clock out of its system prompt."""
    dt = datetime.datetime.fromisoformat(str(when).strip())
    if dt.tzinfo is not None:
        return dt.timestamp()
    return to_epoch(dt, tz)


def _local(ts, zone):
    return datetime.datetime.fromtimestamp(ts, zone).replace(tzinfo=None)


def _build(rule_str, tz, dtstart_ts):
    """Compile a rule once, anchored in NAIVE LOCAL WALL CLOCK.

    Local-not-UTC is the whole ballgame: expanding FREQ=DAILY;BYHOUR=7 in UTC
    passes every test that does not cross a DST boundary, then quietly turns a
    7am alarm into a 6am one on the first Sunday in November.
    """
    from dateutil.rrule import rrulestr
    zone = _zone(tz)
    return rrulestr(rule_str, dtstart=_local(dtstart_ts, zone)), zone


def next_occurrence(rule_str, tz, after_ts, dtstart_ts):
    """First occurrence strictly after after_ts, or None if exhausted."""
    rule, zone = _build(rule_str, tz, dtstart_ts)
    nxt = rule.after(_local(after_ts, zone), inc=False)
    return None if nxt is None else to_epoch(nxt, tz)


def validate_rrule(rule_str, tz=None):
    """Return an error string, or '' if the rule parses. Called at CREATE time so
    a malformed rule is rejected while the model can still fix it, instead of
    detonating on a background thread at 7am three weeks later."""
    if not rule_str:
        return ""
    try:
        from dateutil.rrule import rrulestr
    except ImportError:
        return "python-dateutil is not installed, so recurring items are unavailable."
    # SECONDLY is never what anyone means by a reminder, and it makes every
    # catch-up expansion scan millions of occurrences. Refuse it at the door.
    if "SECONDLY" in rule_str.upper():
        return "Per-second recurrence isn't supported. Use MINUTELY or slower."
    try:
        rule = rrulestr(rule_str, dtstart=datetime.datetime(2024, 1, 1))
        if rule.after(datetime.datetime(2024, 1, 1)) is None:
            return f"That recurrence rule never fires: {rule_str}"
    except Exception as e:
        return f"Bad recurrence rule ({e}). Use RFC 5545, e.g. FREQ=DAILY;BYHOUR=7;BYMINUTE=0."
    return ""


# ── row access ─────────────────────────────────────────────────────────────
_COLS = ("id, created, kind, title, action, payload, rrule, tz, next_ts, state, "
         "urgency, catchup, require_ack, nag_after, nag_count, nag_max, "
         "fired_ts, ack_ts, owner, last_error")


def _dict(row):
    d = dict(zip([c.strip() for c in _COLS.split(",")], row))
    try:
        d["payload"] = json.loads(d["payload"] or "{}")
    except Exception:
        d["payload"] = {}
    return d


def get(sid):
    con = memory.connect()
    row = con.execute(f"SELECT {_COLS} FROM schedules WHERE id=?", (sid,)).fetchone()
    con.close()
    return _dict(row) if row else None


# ── creation ───────────────────────────────────────────────────────────────
def create(args):
    """Handle the `remind` tool. Returns a spoken-style line for the model to
    relay - including the RESOLVED local time, so a misread relative time is
    caught out loud by the user rather than silently at fire time."""
    text = (args.get("text") or "").strip()
    if not text:
        return "A reminder needs something to say."
    when = args.get("when")
    if not when:
        return "A reminder needs a time. Read the clock in your system prompt and pass an ISO timestamp."
    try:
        ts = parse_when(when)
    except Exception as e:
        return f"Could not read '{when}' as a timestamp ({e}). Use ISO-8601, e.g. 2026-08-26T07:00:00."

    rule = (args.get("rrule") or "").strip()
    err = validate_rrule(rule)
    if err:
        return err

    kind = (args.get("kind") or "reminder").strip().lower()
    if kind not in SCHED_CATCHUP:
        kind = "reminder"
    urgency = (args.get("urgency") or "").strip().lower()
    if urgency not in _URGENCIES:
        urgency = _DEFAULT_URGENCY[kind]
    action = (args.get("action") or "say").strip().lower()
    if action not in _ACTIONS:
        action = "say"

    if action == "turn":
        payload = {"prompt": args.get("prompt") or text}
    elif action == "delegate":
        agent, task = args.get("agent"), args.get("task")
        if not agent or not task:
            return "A delegate action needs both an agent and a task."
        payload = {"agent": agent, "task": task}
    else:
        payload = {"text": text}

    now = time.time()
    # A one-shot already in the past is almost always a timezone or AM/PM slip
    # on the model's part. Refuse it loudly rather than firing instantly and
    # leaving the user wondering why their 7am alarm went off at 3pm.
    if not rule and ts < now - 60:
        return (f"That time is already past ({_date_phrase(to_local(ts))} "
                f"{to_local(ts):%H:%M}). Check the date, or say when you actually meant.")

    con = memory.connect()
    cur = con.execute(
        "INSERT INTO schedules(created, kind, title, action, payload, rrule, tz,"
        " next_ts, state, urgency, catchup, require_ack, nag_after, nag_count,"
        " nag_max, fired_ts, ack_ts, owner, last_error)"
        " VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?,0,?,NULL,NULL,'','')",
        (now, kind, text[:200], action, json.dumps(payload), rule, SCHED_TZ, ts,
         urgency, int(SCHED_CATCHUP[kind]),
         1 if args.get("require_ack") else 0, 600,
         3 if args.get("require_ack") else 0))
    con.commit()
    sid = cur.lastrowid
    con.close()

    when_txt = _spoken_time(ts)
    if rule:
        return f"Set, repeating. First one {when_txt}. (id {sid})"
    return f"Set for {when_txt}. (id {sid})"


def _date_phrase(local):
    """'Mon 3 Nov'. Built by hand because the obvious %-d is a glibc extension:
    it raises ValueError on Windows, where half of this repo's tests are run."""
    return f"{local:%a} {local.day} {local:%b}"


def _spoken_time(ts):
    """Human phrasing for a confirmation line. TTS reads this aloud, so it is
    prose, not an ISO string."""
    local = to_local(ts)
    today = datetime.date.today()
    delta_days = (local.date() - today).days
    clock = local.strftime("%H:%M")
    if delta_days == 0:
        mins = (ts - time.time()) / 60.0
        if 0 < mins < 90:
            return f"in {int(round(mins))} minutes, at {clock}"
        return f"today at {clock}"
    if delta_days == 1:
        return f"tomorrow at {clock}"
    if 1 < delta_days < 7:
        return f"{local:%A} at {clock}"
    return f"{_date_phrase(local)} at {clock}"


# ── the tick: due -> claim -> advance ──────────────────────────────────────
def recover(owner=""):
    """Sweep rows stranded in 'firing' by a crash or restart back to pending.

    The row's next_ts is untouched, so the next tick re-evaluates it normally
    and roll_forward() decides whether it is still worth speaking. Duplicate
    delivery in the crash window is accepted deliberately: for a reminder,
    hearing it twice is a smaller failure than never hearing it.
    """
    con = memory.connect()
    cur = con.execute(
        "UPDATE schedules SET state='pending', owner='' "
        "WHERE state='firing' AND fired_ts IS NOT NULL AND fired_ts < ?",
        (time.time() - SCHED_CLAIM_STALE,))
    con.commit()
    n = cur.rowcount
    con.close()
    return n


def due(now=None):
    """Pending rows whose time has come. Cheap - the schedules_due index covers
    exactly this predicate."""
    now = now or time.time()
    con = memory.connect()
    rows = con.execute(
        f"SELECT {_COLS} FROM schedules"
        " WHERE state='pending' AND next_ts IS NOT NULL AND next_ts<=?"
        " ORDER BY next_ts", (now,)).fetchall()
    con.close()
    return [_dict(r) for r in rows]


def claim(sid, next_ts, owner):
    """Take exclusive ownership of one occurrence. True = it is yours to fire.

    This single statement is the whole concurrency design. sqlite serialises
    writers, so of any number of tickers in any number of processes exactly one
    can see rowcount==1. Matching on next_ts as well as state means a row that
    was rescheduled out from under us is not claimed by mistake.
    """
    con = memory.connect()
    cur = con.execute(
        "UPDATE schedules SET state='firing', fired_ts=?, owner=?"
        " WHERE id=? AND state='pending' AND next_ts=?",
        (time.time(), str(owner)[:40], sid, next_ts))
    con.commit()
    won = cur.rowcount == 1
    con.close()
    return won


def roll_forward(row, now=None):
    """Decide what a due row does now. Returns (fire_ts or None, next_ts or None).

    fire_ts None means "do not speak this one" - it was missed by more than its
    grace period. next_ts None means the row is exhausted and should be closed.

    The loop below is the catch-up-storm guard: a laptop asleep from 01:00 to
    07:00 with an hourly rule has six missed occurrences, and firing all six on
    wake is indistinguishable from a malfunction. Only the most recent is ever
    a candidate.
    """
    now = now or time.time()
    catchup = float(row.get("catchup") or 0)
    candidate = row["next_ts"]
    rule_str = (row.get("rrule") or "").strip()

    if not rule_str:
        fire = candidate if (now - candidate) <= catchup else None
        return fire, None

    try:
        rule, zone = _build(rule_str, row.get("tz"), row["created"])
        now_local = _local(now, zone)
        # before()/after() ask the question directly instead of stepping through
        # the backlog one occurrence at a time. Stepping meant re-compiling the
        # rule per step and rescanning from dtstart on every call - O(missed^2),
        # which on an hourly rule is invisible and on a per-minute rule is a
        # hang. Two calls answer it regardless of how far behind the row is.
        latest_local = rule.before(now_local, inc=True)
        next_local = rule.after(now_local, inc=False)
    except Exception:
        # Parsed at create time but blows up expanding: close the row rather
        # than re-raising on every tick forever.
        return None, None

    nxt = to_epoch(next_local, row.get("tz")) if next_local else None
    if latest_local is None:
        return None, nxt
    latest = to_epoch(latest_local, row.get("tz"))
    fire = latest if (now - latest) <= catchup else None
    return fire, nxt


def advance(sid, next_ts, fired, missed=False):
    """Close out a fired (or skipped) occurrence and arm the next one."""
    con = memory.connect()
    if next_ts:
        state = "pending"
    elif missed:
        state = "missed"
    else:
        state = "delivered" if fired else "done"
    con.execute("UPDATE schedules SET next_ts=?, state=?, owner='' WHERE id=?",
                (next_ts, state, sid))
    con.commit()
    con.close()


def tick(fire, owner="cortana", now=None):
    """One scheduler pass: claim -> roll forward -> fire -> advance.

    `fire(row, fire_ts)` is injected rather than imported because the action
    runner is process-specific (the cortana process can speak and delegate; the
    bridge can only reach the phone). Keeping the loop here and the runner out
    there is also what makes this testable on a box with no audio hardware.

    Returns the number of occurrences actually fired.
    """
    now = now or time.time()
    fired = 0
    for row in due(now):
        # Claim BEFORE computing anything: if another ticker owns this
        # occurrence there is nothing to work out, and this conditional UPDATE
        # is the only thing standing between the user and a doubled reminder.
        if not claim(row["id"], row["next_ts"], owner):
            continue
        fire_ts, next_ts = roll_forward(row, now)
        try:
            if fire_ts is not None:
                fire(row, fire_ts)
                fired += 1
        except Exception as e:
            note_error(row["id"], e)
            print(f"[sched] item {row['id']} failed:", e)
        finally:
            advance(row["id"], next_ts, fired=fire_ts is not None,
                    missed=fire_ts is None and next_ts is None)
    return fired


def note_error(sid, msg):
    con = memory.connect()
    con.execute("UPDATE schedules SET last_error=? WHERE id=?", (str(msg)[:300], sid))
    con.commit()
    con.close()


# ── user-facing operations ─────────────────────────────────────────────────
def set_state(sid, ack=False, cancel=False):
    row = get(sid)
    if not row:
        return f"No scheduled item with id {sid}."
    con = memory.connect()
    if cancel:
        con.execute("UPDATE schedules SET state='cancelled', next_ts=NULL WHERE id=?", (sid,))
        con.commit()
        con.close()
        return f"Cancelled: {row['title']}"
    if ack:
        con.execute("UPDATE schedules SET state='acked', ack_ts=? WHERE id=?",
                    (time.time(), sid))
        con.commit()
        con.close()
        return f"Got it - {row['title']}"
    con.close()
    return "Nothing to change - pass ack or cancel."


def upcoming(limit=6):
    """Next few live items, for the dashboard tile and the phone snapshot."""
    con = memory.connect()
    rows = con.execute(
        f"SELECT {_COLS} FROM schedules"
        " WHERE state IN ('pending','firing','delivered') AND next_ts IS NOT NULL"
        " ORDER BY next_ts LIMIT ?", (int(limit),)).fetchall()
    con.close()
    return [{"id": r[0], "kind": r[2], "title": r[3], "at": r[8],
             "urgency": r[10], "repeats": bool(r[6])} for r in rows]


def summary(include_done=False):
    """Spoken-style list for the schedule_list tool."""
    states = ("pending", "firing", "delivered")
    if include_done:
        states += ("acked", "missed", "done", "cancelled")
    con = memory.connect()
    rows = con.execute(
        f"SELECT {_COLS} FROM schedules WHERE state IN ({','.join('?' * len(states))})"
        " ORDER BY next_ts IS NULL, next_ts LIMIT 25", states).fetchall()
    con.close()
    if not rows:
        return "Nothing scheduled."
    out = []
    for r in rows:
        d = _dict(r)
        when = _spoken_time(d["next_ts"]) if d["next_ts"] else d["state"]
        rep = " (repeats)" if d["rrule"] else ""
        out.append(f"{d['id']}: {d['title']} - {when}{rep}")
    return "\n".join(out)


def log_delivery(src, ref, urgency, surfaces, presence, text):
    con = memory.connect()
    con.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)",
                (time.time(), src, int(ref or 0), urgency,
                 ",".join(surfaces), presence, str(text)[:300]))
    con.commit()
    con.close()
