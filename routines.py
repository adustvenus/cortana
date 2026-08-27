"""Routines: declarative "when TRIGGER, do ACTION" rules, plus the morning brief.

Evaluated from the SAME scheduler tick as schedule.py - there is no second
daemon and no second thread. On a box with 2 GB free and a laptop battery, a
second poller is a cost with no matching benefit.

The design decision that shapes everything below: **clock is not a trigger
here**. "At 7am daily" is a schedules row with an RRULE whose payload names a
routine; this module only owns what that routine DOES. Two engines expanding
recurrence would mean two places to fix the same DST bug, and schedule.py has
already paid for that work.

So a trigger is only ever calendar | health | presence, and all three share one
property: they are EDGE-triggered. A routine fires on the TRANSITION into its
condition, then goes quiet until the condition has gone away and come back (and
min_gap has elapsed). That single property is the whole difference between a
sentinel and a nag - a level-triggered "disk is over 90%" would say so every
thirty seconds until the disk was fixed.

The `edge` column stores the last evaluated value, so the quiet persists across
a restart rather than re-firing every time the process comes up.
"""
import datetime
import json
import time
from pathlib import Path

import calendar_state
import memory
import notify
import presence
import schedule

try:
    from config import BRIEF_ZIP, ROUTINE_TICK
except ImportError:
    # config.py is owned by another agent and may lag this module. A missing
    # tunable must degrade to a default, never make routines un-importable -
    # an ImportError here would take the scheduler thread down with it.
    BRIEF_ZIP, ROUTINE_TICK = "", 30.0

SENTINEL_FILE = Path(__file__).resolve().parent / "sentinel_state.json"

TRIGGERS = ("calendar", "health", "presence")
ACTIONS = ("say", "turn", "delegate", "brief")
_URGENCIES = ("ambient", "normal", "urgent", "critical")
# presence.read_desk()'s whole vocabulary, mirrored so a typo in a cond is
# refused at create time instead of never matching.
_DESK_STATES = ("present", "away", "asleep", "unknown")

# Per-trigger default quiet period. Health is 6h because a health condition
# that flaps (a disk hovering on 90%) would otherwise fire on every crossing;
# presence is 30 min so "welcome back" is not said twice on one trip to the
# kitchen; calendar is 0 because each event is deduplicated by key anyway.
_DEFAULT_GAP = {"health": 6 * 3600, "presence": 1800, "calendar": 0}

# sentinel_state.json older than this is treated as unreadable rather than as
# current. A stale "ok" would silence a real alarm; a stale "bad" would raise a
# false one. Unreadable does nothing AND leaves the edge alone, so the routine
# resumes where it left off when the sentinel comes back.
_SENTINEL_STALE = 900

# How far back the brief looks for finished overnight work. A fixed window and
# NOT a meta cursor: a cursor would make "say that again" come back empty,
# because reading the brief would consume what it was reporting.
_BRIEF_LOOKBACK = 14 * 3600
_WEATHER_TTL = 1800
# A failed lookup is remembered for a shorter spell: long enough that a dead
# DNS resolver is not re-dialled on every brief, short enough that a laptop
# that has just found wifi gets its weather back within the hour.
_WEATHER_FAIL_TTL = 600

# Ordering for the three sentinel states, so a routine can say ">= warn"
# instead of enumerating every bad-ish value.
_HEALTH_ORDER = {"ok": 0, "warn": 1, "bad": 2}

# Hard cap on the `edge` column. Only the calendar edge is variable-length,
# and a fired-event key is a fixed 29 characters, so this is room for roughly
# 130 events in one day - past anything a human calendar holds. See
# _pack_keys() for what happens if it is ever reached anyway.
_EDGE_MAX = 4000
# Most events one calendar pass will ever announce. See _eval_calendar.
_CAL_BURST = 3

# Guards the evaluation rate. main.py ticks every 5s for the scheduler's sake;
# re-reading three state files and a sqlite table twelve times a minute forever
# is exactly the kind of idle burn that got a previous version walked back.
_last_eval = {"ts": 0.0}


# ── row access ─────────────────────────────────────────────────────────────
_COLS = ("id, created, name, trigger, cond, action, payload, urgency, enabled, "
         "edge, last_fired, min_gap, fires")


def _dict(row):
    d = dict(zip([c.strip() for c in _COLS.split(",")], row))
    for k in ("cond", "payload"):
        try:
            d[k] = json.loads(d[k] or "{}")
        except Exception:
            d[k] = {}
    return d


def get(name):
    con = memory.connect()
    row = con.execute(f"SELECT {_COLS} FROM routines WHERE name=?",
                      (str(name).strip().lower(),)).fetchone()
    con.close()
    return _dict(row) if row else None


def items():
    """The /local/routines contract, exactly. Extra keys are deliberately NOT
    added here - the dashboard and the phone both parse this shape."""
    con = memory.connect()
    rows = con.execute(f"SELECT {_COLS} FROM routines ORDER BY name").fetchall()
    con.close()
    return [{"name": r[2], "trigger": r[3], "enabled": bool(r[8]),
             "lastFired": r[10], "fires": r[12] or 0} for r in rows]


# ── creation and editing (the two tools) ───────────────────────────────────
def create(args):
    """Handle the `routine` tool. Returns a spoken-style line for the lead.

    An `rrule` means the user asked for a clock routine, which is a schedules
    row pointing back at this one - see the module docstring. The routines row
    still exists (it holds the action), but with trigger 'clock' it is skipped
    by the edge evaluator, so there is genuinely only one recurrence engine.
    """
    name = (args.get("name") or "").strip().lower()[:60]
    if not name:
        return "A routine needs a short name so you can turn it off later."

    action = (args.get("action") or "say").strip().lower()
    if action not in ACTIONS:
        return f"Unknown routine action '{action}'. Use say, brief, delegate or turn."

    rule = (args.get("rrule") or "").strip()
    trigger = (args.get("trigger") or "").strip().lower()
    if rule:
        trigger = "clock"
    elif trigger not in TRIGGERS:
        return ("A routine triggers on calendar, health or presence - or pass an "
                "rrule for a time of day. Use remind for a one-off time.")

    cond = args.get("cond") or {}
    if isinstance(cond, str):
        try:
            cond = json.loads(cond)
        except Exception:
            return 'The condition must be an object, e.g. {"from":"away","to":"present"}.'
    err = _validate_cond(trigger, cond)
    if err:
        return err

    if action == "delegate":
        agent, task = args.get("agent"), args.get("task")
        if not agent or not task:
            return "A delegate routine needs both an agent and a task."
        payload = {"agent": agent, "task": task}
    elif action == "turn":
        payload = {"prompt": args.get("prompt") or args.get("text") or name}
    elif action == "brief":
        payload = {}
    else:
        text = (args.get("text") or "").strip()
        if not text:
            return "A say routine needs the words to say."
        payload = {"text": text}

    urgency = (args.get("urgency") or "").strip().lower()
    if urgency not in _URGENCIES:
        urgency = "normal"
    gap = args.get("min_gap")
    gap = int(gap) if isinstance(gap, (int, float)) else _DEFAULT_GAP.get(trigger, 0)

    # The clock half is resolved BEFORE the INSERT, not after it. Validating
    # afterwards returned the error but left the routines row committed, so a
    # routine the user was told had been refused showed up in the list, with no
    # schedules row behind it and therefore no way to ever run.
    first = None
    if trigger == "clock":
        err = schedule.validate_rrule(rule)
        if err:
            return err
        try:
            first = schedule.parse_when(args["when"]) if args.get("when") else _next_of(rule)
        except Exception as e:
            return f"Could not read '{args.get('when')}' as a timestamp ({e}). Use ISO-8601."
        if first is None:
            return f"That recurrence rule never fires: {rule}"

    # INSERT OR REPLACE keyed on the existing id so re-creating a routine by the
    # same name EDITS it. Two routines called "morning brief" differing only in
    # a typo'd condition is the failure mode this avoids; carrying `fires` over
    # keeps the dashboard's count from resetting on every tweak.
    con = memory.connect()
    con.execute(
        "INSERT OR REPLACE INTO routines(id, created, name, trigger, cond, action,"
        " payload, urgency, enabled, edge, last_fired, min_gap, fires)"
        " VALUES((SELECT id FROM routines WHERE name=?), ?,?,?,?,?,?,?,1,'',NULL,?,"
        " COALESCE((SELECT fires FROM routines WHERE name=?),0))",
        (name, time.time(), name, trigger, json.dumps(cond), action,
         json.dumps(payload), urgency, gap, name))
    con.commit()
    con.close()

    if trigger != "clock":
        return f"Routine '{name}' is on, watching {trigger}."

    _arm_clock(name, rule, first)
    # Reaching for schedule's private phraser on purpose: the alternative is a
    # second copy of "tomorrow at 07:00" logic that drifts from the one the
    # user hears from `remind`, and two ways of saying the same time is exactly
    # the confusion this line exists to prevent.
    return f"Routine '{name}' is on, first run {schedule._spoken_time(first)}."


def _next_of(rule):
    now = time.time()
    return schedule.next_occurrence(rule, None, now, now)


def _arm_clock(name, rule, first_ts):
    """The clock half of a routine: one schedules row whose payload names it.

    Inserted straight into schedules rather than through schedule.create()
    because create() validates `action` against its own vocabulary and would
    reject a routine pointer. action='say' with the routine named in the
    payload is the safe degradation - a build where main.py has not yet learnt
    about routine payloads speaks the title instead of raising.

    Existing rows are cancelled first so editing a routine twice does not leave
    two alarms stacked behind it. 'paused' is in that list because a DISABLED
    routine parks its row there: without it, editing a routine while it was off
    left the old row behind, and re-enabling armed both - one 7am brief spoken
    twice, from a state the user could no longer see.
    """
    from config import SCHED_CATCHUP, SCHED_TZ
    con = memory.connect()
    con.execute("UPDATE schedules SET state='cancelled', next_ts=NULL"
                " WHERE kind='routine' AND title=? AND state IN ('pending','firing','paused')",
                (name,))
    con.execute(
        "INSERT INTO schedules(created, kind, title, action, payload, rrule, tz,"
        " next_ts, state, urgency, catchup, require_ack, nag_after, nag_count,"
        " nag_max, fired_ts, ack_ts, owner, last_error)"
        " VALUES(?,'routine',?,'say',?,?,?,?,'pending','normal',?,0,600,0,0,"
        " NULL,NULL,'','')",
        (time.time(), name, json.dumps({"routine": name}), rule, SCHED_TZ,
         first_ts, int(SCHED_CATCHUP.get("routine", 900))))
    con.commit()
    con.close()


def _validate_cond(trigger, cond):
    """Reject a malformed condition while the model can still fix it, rather
    than on a background thread at 7am three weeks later - same reasoning as
    schedule.validate_rrule()."""
    if trigger == "clock":
        return ""
    if not isinstance(cond, dict):
        return "The condition must be an object."
    if trigger == "presence":
        if cond.get("to") not in _DESK_STATES:
            return ("A presence routine needs to: present, away, asleep or unknown, "
                    "and optionally from.")
        # A typo'd `from` is the quietest possible failure: the routine is
        # accepted, listed, enabled, and can never match. Catch it at the door,
        # the same reason validate_rrule() runs at create time.
        if cond.get("from") is not None and cond["from"] not in _DESK_STATES:
            return (f"'{cond['from']}' is not a desk state. from must be "
                    "present, away, asleep or unknown.")
    elif trigger == "health":
        if not cond.get("metric"):
            return ('A health routine needs a metric, e.g. '
                    '{"metric":"worst","op":">=","value":"warn"}.')
        if cond.get("op", "==") not in ("==", "!=", ">", ">=", "<", "<="):
            return "The op must be one of == != > >= < <=."
        # Same class of silent never-fires: without a value every comparison
        # runs against the string "None", which no metric ever equals, and
        # _compare returns a plain False so not even _note_error records why.
        if cond.get("value") is None:
            return ('A health routine needs a value to compare against, e.g. '
                    '{"metric":"worst","op":">=","value":"warn"}.')
    elif trigger == "calendar":
        if cond.get("when", "starts_in") != "starts_in":
            return "The only calendar condition is starts_in."
        try:
            int(cond.get("minutes", 10))
        except Exception:
            return "minutes must be a number."
    return ""


def set_state(name=None, enabled=None, delete=False):
    """Handle the `routine_set` tool. With no name, lists what exists - the
    model needs the names before it can turn one off, and folding the listing
    in here keeps the lead's tool count down."""
    if not name:
        return summary()
    name = str(name).strip().lower()
    row = get(name)
    if not row:
        return f"No routine called '{name}'. {summary()}"
    con = memory.connect()
    if delete:
        con.execute("DELETE FROM routines WHERE name=?", (name,))
        # 'paused' too: deleting a routine that was switched off used to leave
        # its clock row parked forever, and re-creating the same name later
        # armed a second one alongside it.
        con.execute("UPDATE schedules SET state='cancelled', next_ts=NULL"
                    " WHERE kind='routine' AND title=? AND state IN ('pending','firing','paused')",
                    (name,))
        # The error note is keyed by name and nothing else prunes `meta`, so a
        # deleted routine would otherwise leave a row behind for good.
        con.execute("DELETE FROM meta WHERE k=?", (f"routine_err_{name}",))
        con.commit()
        con.close()
        return f"Deleted the '{name}' routine."
    want = 1 if (enabled is None or enabled) else 0
    # Clearing the edge on re-enable is deliberate: a routine that was off
    # through a whole away-and-back cycle holds a stale last-value, and
    # honouring it would swallow the next real transition.
    con.execute("UPDATE routines SET enabled=?, edge=CASE WHEN ?=1 THEN '' ELSE edge END"
                " WHERE name=?", (want, want, name))
    con.execute("UPDATE schedules SET state=? WHERE kind='routine' AND title=?"
                " AND state IN ('pending','paused')",
                ("pending" if want else "paused", name))
    con.commit()
    con.close()
    return f"Routine '{name}' is {'on' if want else 'off'}."


# schedule.summary() caps its list at 25 for the same reason: this string is
# handed to TTS, and sixty entries is a two-minute monologue nobody can
# interrupt usefully.
_SUMMARY_MAX = 25


def summary():
    rows = items()
    if not rows:
        return "No routines set up yet."
    more = len(rows) - _SUMMARY_MAX
    out = []
    for r in rows[:_SUMMARY_MAX]:
        when = ("never" if not r["lastFired"]
                else schedule.to_local(r["lastFired"]).strftime("%a %H:%M"))
        out.append(f"{r['name']} ({r['trigger']}, "
                   f"{'on' if r['enabled'] else 'off'}, last {when})")
    if more > 0:
        out.append(f"and {more} more")
    return "; ".join(out)


# ── the tick ───────────────────────────────────────────────────────────────
def tick(turn=None, run_agent=None, now=None, min_interval=None):
    """One routines pass. Called from the scheduler thread right after
    schedule.tick(), so there is no second daemon.

    `turn` and `run_agent` are INJECTED for the same reason schedule.tick takes
    its fire argument: orchestrator keeps _turn, _stalled, _restart_flag and
    _shutdown_flag in module globals, and calling into it from a background
    thread without main.py's _turn_lock corrupts a live voice turn. main.py
    supplies a locked, flag-clearing wrapper; this module stays testable on a
    box with no audio and no API key.

    Returns the number of routines that actually fired.
    """
    now = now or time.time()
    gap = ROUTINE_TICK if min_interval is None else min_interval
    # 0 <= , not just < : time.time() is not monotonic. An NTP step or a
    # laptop resuming with a corrected RTC can put the last stamp in the
    # FUTURE, and a bare `now - last < gap` is then true for as long as the
    # jump lasted - routines stop being evaluated at all, silently, with the
    # tick still being called on schedule. A stamp ahead of now is nonsense,
    # so it is discarded rather than waited out.
    if 0 <= now - _last_eval["ts"] < gap:
        return 0
    _last_eval["ts"] = now

    con = memory.connect()
    rows = con.execute(
        f"SELECT {_COLS} FROM routines WHERE enabled=1 AND trigger IN"
        " ('calendar','health','presence') ORDER BY id").fetchall()
    con.close()

    fired = 0
    for raw in rows:
        row = _dict(raw)
        try:
            new_edge, hits = _evaluate(row, now)
        except Exception as e:
            _note_error(row["name"], f"evaluate: {e}")
            continue
        if new_edge is not None and new_edge != row["edge"]:
            # Conditional on the edge we READ, for the same reason
            # schedule.claim() is conditional on next_ts: phase 2 moves the
            # tick into the bridge and both processes tick during the cutover.
            # sqlite serialises writers, so exactly one of them can see
            # rowcount==1 and therefore exactly one can speak the transition.
            if not _set_edge(row["id"], new_edge, row["edge"]):
                continue
        elif hits:
            # Every fire is supposed to be preceded by an edge change. If one
            # is not, the edge cannot record that we spoke, so speaking would
            # repeat on every tick forever. Refuse rather than nag.
            _note_error(row["name"], "hit without an edge change; not fired")
            continue
        if not hits:
            continue
        # min_gap gates the SPEAKING, never the edge update above. If it gated
        # both, a condition that flapped during the quiet period would look
        # like a fresh transition once the period ended and fire twice.
        last = row["last_fired"] or 0
        # EVERY hit, not just the first: two meetings inside one lookahead
        # window both get their key written into the edge, so announcing only
        # hits[0] marked the second one as delivered and then never said it.
        # Back-to-back meetings are exactly when that matters.
        for hit in hits:
            # 0 <= again, per the tick guard: a last_fired stamped after `now`
            # by a clock correction would otherwise mute the routine until the
            # wall clock caught up with it.
            if last and 0 <= now - last < (row["min_gap"] or 0):
                break
            ok = _fire(row, hit, turn=turn, run_agent=run_agent)
            _mark_fired(row["id"], now, counted=ok)
            last = now
            fired += 1 if ok else 0
    return fired


def _set_edge(rid, edge, was):
    """Advance the edge, but only from the value we read. True = it is ours.

    The cap is a backstop only - _pack_keys() already keeps the calendar edge
    (the one variable-length value) inside it. It must never actually bite,
    because a truncated value would not match `was` on the next pass and the
    routine would stop advancing altogether.
    """
    con = memory.connect()
    cur = con.execute("UPDATE routines SET edge=? WHERE id=? AND edge=?",
                      (str(edge)[:_EDGE_MAX], rid, was))
    con.commit()
    won = cur.rowcount == 1
    con.close()
    return won


def _mark_fired(rid, now, counted=True):
    """last_fired advances even when the action failed, so a permanently broken
    action re-nags at min_gap rather than on every single flap. `fires` counts
    only real deliveries, so the dashboard's number stays honest."""
    con = memory.connect()
    con.execute("UPDATE routines SET last_fired=?, fires=fires+? WHERE id=?",
                (now, 1 if counted else 0, rid))
    con.commit()
    con.close()


def _note_error(name, msg):
    # No last_error column exists on routines and memory.init() belongs to
    # another owner, so this goes to `meta` - never `kv`, which recall_all()
    # dumps into the system prompt verbatim every turn.
    memory.meta_set(f"routine_err_{name}", str(msg)[:300])
    print(f"[routines] {name}:", msg)


def last_error(name):
    return memory.meta_get(f"routine_err_{name}", "")


# ── evaluation, one function per trigger ───────────────────────────────────
def _evaluate(row, now):
    """-> (new_edge or None, [context dicts]).

    new_edge None means "could not measure" - the edge is left exactly as it
    was, so an unreadable state file postpones the decision instead of faking
    a transition in either direction.
    """
    trigger = row["trigger"]
    if trigger == "presence":
        return _eval_presence(row, now)
    if trigger == "health":
        return _eval_health(row, now)
    if trigger == "calendar":
        return _eval_calendar(row, now)
    return None, []


def _eval_presence(row, now):
    cur = presence.read_desk(now)
    cond, prev = row["cond"], row["edge"]
    want_to, want_from = cond.get("to"), cond.get("from")
    fires = []
    if cur == want_to and prev != cur:
        # An empty edge is a routine that has never been evaluated. It matches
        # no `from`, so creating "welcome back when I return" while you are
        # sitting at the desk does not greet you immediately.
        if not want_from or want_from == prev:
            fires.append({"from": prev or "unknown", "to": cur, "state": cur})
    return cur, fires


def _eval_health(row, now):
    state = _read_sentinel(now)
    if state is None:
        return None, []
    cond = row["cond"]
    val = _metric(state, cond.get("metric"))
    if val is None:
        return None, []
    hit = _compare(val, cond.get("op", "=="), cond.get("value"))
    if hit is None:
        _note_error(row["name"], f"cannot compare {val!r} to {cond.get('value')!r}")
        return None, []
    fires = []
    # "" (never evaluated) counts as a transition INTO a true condition: a
    # routine created while the disk is already full is news the user just
    # asked for, and staying silent until the disk recovered and re-filled
    # would be indistinguishable from the feature not working.
    if hit and row["edge"] != "1":
        fires.append({"metric": cond.get("metric"), "value": val,
                      "detail": _detail(state, cond.get("metric")),
                      "worst": state.get("worst", "")})
    return ("1" if hit else "0"), fires


def _eval_calendar(row, now):
    """Fire once per event, `minutes` before it starts.

    Two things guard the midnight case, deliberately overlapping. First,
    calendar_state only ever holds TODAY's events, and each start is resolved
    to an ABSOLUTE epoch from that file's own `day` stamp - so an event listed
    as 00:05 is twenty-three hours in the past at 23:55, not ten minutes ahead,
    and cannot fire. Second, the fired-key set resets on a day change to bound
    its growth, but every key is day-qualified anyway, so a reset landing at
    the wrong moment still cannot re-fire yesterday's event.
    """
    data = calendar_state.read()
    day = data.get("day") or ""
    try:
        prev = json.loads(row["edge"] or "{}")
    except Exception:
        prev = {}
    keys = set(prev.get("keys") or []) if prev.get("day") == day else set()

    minutes = int(row["cond"].get("minutes", 10))
    due = []
    for ev in (data.get("events") or []):
        if ev.get("allDay") or ev.get("past"):
            continue
        ts = _event_ts(day, ev.get("time"))
        if ts is None:
            continue
        ahead = ts - now
        if not 0 <= ahead <= minutes * 60:
            continue
        key = f"{day}T{ev.get('time')}|{_title_key(ev.get('title'))}"
        if key not in keys:
            due.append((ts, key, ev, ahead))

    # Soonest first, and at most _CAL_BURST per pass. A routine created at noon
    # with a wide window has every remaining meeting inside it at once, and a
    # key is only written for an event actually announced - so the overflow is
    # said on the next pass rather than marked delivered and lost. The cap is
    # what keeps a rule with a bad `minutes` from becoming a monologue.
    fires = []
    for ts, key, ev, ahead in sorted(due)[:_CAL_BURST]:
        keys.add(key)
        fires.append({"event": ev.get("title") or "an event",
                      "time": _clock(ev.get("time")),
                      "minutes": max(1, int(round(ahead / 60.0)))})
    return _pack_keys(day, keys), fires


def _pack_keys(day, keys):
    """The calendar edge as JSON, trimmed by dropping whole keys.

    The edge column used to be trimmed by truncating the STRING, which turns
    valid JSON into a fragment json.loads rejects - so the fired-key set read
    back empty and every event still inside the window was announced again on
    the next tick, and the next. A day busy enough to overflow the cap is
    exactly the day on which that is loudest.

    _title_key() keeps a realistic day well inside _EDGE_MAX, so this trim is a
    backstop and not a normal path. When it does run the earliest key goes
    first: those events are the ones already past, so forgetting them is the
    only trim that cannot re-announce anything.
    """
    ordered = sorted(keys)
    while True:
        blob = json.dumps({"day": day, "keys": ordered})
        if len(blob) <= _EDGE_MAX or not ordered:
            return blob
        ordered = ordered[1:]


def _title_key(title):
    """Eight hex characters standing in for the title in a fired-event key.

    The title was carried whole (60 characters of it), which put a day of
    meetings over the edge cap - and the cap can only be enforced by
    FORGETTING a key, which re-announces whichever event it belonged to. A
    fixed-width digest keeps a full day inside the cap instead, so the trim
    never has to run. hashlib and not the builtin hash(): PYTHONHASHSEED is
    randomised per process, so builtin hashes would change every restart and
    every key would look new.
    """
    import hashlib
    return hashlib.sha1(str(title or "").encode("utf-8")).hexdigest()[:8]


def _event_ts(day, hhmm):
    """'2026-08-27' + '09:15' -> epoch, in the scheduler's zone.

    calendar_tool drops the absolute timestamp (it pops _sort before returning)
    so the wall-clock string plus the file's own day stamp is all there is.
    """
    try:
        d = datetime.date.fromisoformat(day)
        h, m = str(hhmm).split(":")
        return schedule.to_epoch(datetime.datetime(d.year, d.month, d.day,
                                                   int(h), int(m)))
    except Exception:
        return None


def _clock(hhmm):
    """'09:15' -> '9:15'. TTS reads a leading zero as "oh nine"."""
    txt = str(hhmm or "")
    return txt.lstrip("0") or txt


def _read_sentinel(now):
    """The /local/sentinel contract as written to disk by sentinel.py. Read as
    a file, not imported: sentinel.py is another owner's module, and importing
    it would couple two poll schedules together for no gain."""
    try:
        d = json.loads(SENTINEL_FILE.read_text())
    except Exception:
        return None
    try:
        ts = float(d.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if not ts:
        # sentinel.py is another owner's module and `ts` is only an agreed key,
        # not an enforced one. Treating its absence as "fresh" meant a file
        # written by a sentinel that died weeks ago was believed indefinitely -
        # exactly the stale "ok" this guard exists to refuse. mtime is a worse
        # clock than the writer's own stamp, and a far better one than none.
        try:
            ts = SENTINEL_FILE.stat().st_mtime
        except OSError:
            return None
    if now - ts > _SENTINEL_STALE:
        return None
    return d


def _metric(state, name):
    if not name:
        return None
    if name in ("worst", "state"):
        return state.get("worst")
    # A numeric extra, if the sentinel publishes one alongside its checks. The
    # agreed contract is only {worst, checks}, so this is opportunistic and its
    # absence costs nothing.
    metrics = state.get("metrics")
    if isinstance(metrics, dict) and name in metrics:
        return metrics[name]
    for c in state.get("checks") or []:
        if c.get("key") == name:
            return c.get("state")
    return None


def _detail(state, name):
    for c in state.get("checks") or []:
        if c.get("key") == name:
            return c.get("detail") or c.get("label") or ""
    return ""


def _compare(left, op, right):
    """True/False, or None when the two sides are not comparable at all.

    ok/warn/bad are ordered, so ">= warn" works without enumerating states.
    Anything else falls back to numbers, then to string equality - and returns
    None rather than a silent False when none of those apply, because a routine
    that can never fire should be reported, not merely quiet.
    """
    if isinstance(left, str) and isinstance(right, str):
        l, r = left.strip().lower(), right.strip().lower()
        if l in _HEALTH_ORDER and r in _HEALTH_ORDER:
            left, right = _HEALTH_ORDER[l], _HEALTH_ORDER[r]
    try:
        left, right = float(left), float(right)
    except (TypeError, ValueError):
        if op == "==":
            return str(left).strip().lower() == str(right).strip().lower()
        if op == "!=":
            return str(left).strip().lower() != str(right).strip().lower()
        return None
    return {"==": left == right, "!=": left != right, ">": left > right,
            ">=": left >= right, "<": left < right, "<=": left <= right}.get(op)


# ── actions ────────────────────────────────────────────────────────────────
class _Blank(dict):
    """format_map source that never raises. A routine whose text mentions
    {event} on a presence trigger should lose the word, not the whole line."""

    def __missing__(self, key):
        return ""


def _fill(text, ctx):
    # str.format also blows up on a stray brace ("{" in a task description);
    # a routine is not a format-string tutorial, so an unparseable template
    # falls back to itself verbatim.
    try:
        return str(text).format_map(_Blank(ctx))
    except Exception:
        return str(text)


def _fire(row, ctx, turn=None, run_agent=None):
    """Run one routine's action. Returns True if something actually reached a
    surface. Never raises: a failing action must not strand the routine."""
    name, action, payload = row["name"], row["action"], row["payload"]
    try:
        if action == "brief":
            text = brief()
        elif action == "say":
            text = _fill(payload.get("text") or name, ctx)
        elif action == "delegate":
            return _delegate(row, ctx, run_agent)
        elif action == "turn":
            return _turn(row, ctx, turn)
        else:
            _note_error(name, f"unknown action {action}")
            return False
        notify.deliver(text, row["urgency"], src=f"routine:{name}", ref=row["id"])
        return True
    except Exception as e:
        _note_error(name, f"{type(e).__name__}: {e}")
        return False


def _delegate(row, ctx, run_agent):
    import tasks
    if run_agent is None:
        _note_error(row["name"], "no agent runner injected; delegate skipped")
        return False
    task = _fill(row["payload"].get("task") or "", ctx)
    msg = tasks.start(row["payload"].get("agent") or "research", task,
                      runner=lambda a, t, c: run_agent(a, t, cancel=c))
    # tasks.start returns a line written for the LEAD to relay. Nobody is in
    # the loop here, so a refusal ("All 4 task slots are busy") would vanish
    # entirely unless it is put on a surface deliberately.
    if msg and msg.lstrip().lower().startswith(("all ", "can't", "cannot")):
        notify.deliver(msg, "normal", src=f"routine:{row['name']}", ref=row["id"])
    return True


def _turn(row, ctx, turn):
    if turn is None:
        _note_error(row["name"], "no turn runner injected; turn skipped")
        return False
    reply = turn(_fill(row["payload"].get("prompt") or row["name"], ctx))
    if reply:
        notify.deliver(reply, row["urgency"], src=f"routine:{row['name']}",
                       ref=row["id"])
    return True


def run(name, turn=None, run_agent=None):
    """Execute a named routine's action right now, ignoring its trigger.

    This is what the clock path calls: a schedules row fires, main.py sees
    {"routine": name} in the payload and lands here. It is also how a human
    tests a routine without waiting until 7am.
    """
    row = get(name)
    if not row:
        return f"No routine called '{name}'."
    ok = _fire(row, {}, turn=turn, run_agent=run_agent)
    _mark_fired(row["id"], time.time(), counted=ok)
    return (f"Ran '{name}'." if ok
            else f"Routine '{name}' did not complete - {last_error(name)}")


# ── the morning brief ──────────────────────────────────────────────────────
def brief(now=None):
    """One spoken paragraph: date, weather, calendar, overnight work, what is
    still open, and anything the sentinel flagged.

    Every clause is independent and every source is allowed to be missing. A
    brief that blanks because the calendar token expired is worse than useless
    - it is the one morning you find out, at the moment you have no time to
    fix it. So each clause is composed inside its own try, and a source that
    cannot answer simply drops out of the sentence.
    """
    now = now or time.time()
    out = []
    for fn in (_c_greeting, _c_weather, _c_calendar, _c_overnight,
               _c_open, _c_sentinel):
        try:
            line = (fn(now) or "").strip()
        except Exception as e:
            print(f"[routines] brief clause {fn.__name__} failed:", e)
            continue
        if line:
            out.append(line)
    return " ".join(out) or "Good morning."


def _c_greeting(now):
    local = schedule.to_local(now)
    part = "morning" if local.hour < 12 else ("afternoon" if local.hour < 18
                                              else "evening")
    # Built by hand: %-d is a glibc extension that raises ValueError on
    # Windows, where half of this repo's tests run.
    return f"Good {part}. It's {local:%A}, {local.day} {local:%B}."


def _c_calendar(now):
    data = calendar_state.read()
    if data.get("error") and not data.get("events"):
        return ""            # a broken calendar drops its clause, silently
    evs = [e for e in (data.get("events") or []) if not e.get("past")]
    if not evs:
        return "Your calendar is clear."
    named = []
    for e in evs[:3]:
        title = (e.get("title") or "something").strip()
        named.append(f"{title} all day" if e.get("allDay")
                     else f"{title} at {_clock(e.get('time'))}")
    rest = len(evs) - len(named)
    tail = f", and {rest} more" if rest > 0 else ""
    lead = ("One thing on the calendar" if len(evs) == 1
            else f"{len(evs)} things on the calendar")
    return f"{lead}: {'; '.join(named)}{tail}."


def _c_overnight(now):
    done = [r for r in _task_rows()
            if r["status"] in ("done", "failed") and r["ts"] >= now - _BRIEF_LOOKBACK]
    if not done:
        return ""
    ok = [r for r in done if r["status"] == "done"]
    failed = [r for r in done if r["status"] == "failed"]
    bits = []
    if ok:
        which = "; ".join(f"{r['agent']} finished {r['description'][:60]}"
                          for r in ok[:3])
        bits.append(f"Overnight, {which}")
    if failed:
        bits.append(f"{len(failed)} task{'s' if len(failed) > 1 else ''} failed")
    return ". ".join(bits) + "."


def _c_open(now):
    rows = [r for r in _task_rows() if r["status"] in ("started", "running", "queued")]
    if not rows:
        return ""
    if len(rows) == 1:
        return f"Still running: {rows[0]['agent']} on {rows[0]['description'][:60]}."
    return f"{len(rows)} tasks are still running."


def _c_sentinel(now):
    state = _read_sentinel(now)
    if not state:
        return ""
    bad = [c for c in (state.get("checks") or [])
           if str(c.get("state", "")).lower() in ("warn", "bad")]
    if not bad:
        return ""
    named = "; ".join(f"{c.get('label') or c.get('key')} {c.get('detail') or ''}".strip()
                      for c in bad[:2])
    more = f", and {len(bad) - 2} more" if len(bad) > 2 else ""
    lead = "One thing flagged" if len(bad) == 1 else f"{len(bad)} things flagged"
    return f"{lead}: {named}{more}."


def _c_weather(now):
    wx = _weather(now)
    if not wx:
        return ""
    line = f"It's {wx['temp']} and {str(wx['desc']).lower()}"
    if wx.get("hi") is not None:
        line += f", high of {wx['hi']}"
    return line + "."


def _task_rows():
    """Latest row per task id. The tasks table is an append-only audit trail -
    one row per state change - so the newest row is the task's real state."""
    con = memory.connect()
    rows = con.execute(
        "SELECT t.id, t.ts, t.agent, t.description, t.status FROM tasks t"
        " JOIN (SELECT id, MAX(ts) AS ts FROM tasks GROUP BY id) m"
        "   ON t.id=m.id AND t.ts=m.ts ORDER BY t.ts DESC LIMIT 40").fetchall()
    con.close()
    return [{"id": r[0], "ts": float(r[1] or 0), "agent": r[2] or "",
             "description": r[3] or "", "status": r[4] or ""} for r in rows]


# ── weather (stdlib only, no key, cached) ──────────────────────────────────
_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "foggy", 48: "foggy", 51: "drizzling", 53: "drizzling",
        55: "drizzling", 61: "raining", 63: "raining", 65: "raining hard",
        66: "freezing rain", 67: "freezing rain", 71: "snowing", 73: "snowing",
        75: "snowing hard", 77: "snow grains", 80: "showery", 81: "showery",
        82: "heavy showers", 85: "snow showers", 86: "snow showers",
        95: "thundery", 96: "thundery", 99: "thundery"}


def _weather(now):
    """Current conditions for BRIEF_ZIP, or None.

    The same two keyless endpoints the dashboard already uses, so this adds no
    dependency and no account. Cached for half an hour in `meta` because the
    brief can legitimately be re-run ("say that again") and a scheduler thread
    should not make two network calls a minute apart. No ZIP configured means
    no network call at all and no weather clause.
    """
    if not BRIEF_ZIP:
        return None
    try:
        cached = json.loads(memory.meta_get("routine_weather", "") or "{}")
        age = now - float(cached.get("ts") or 0)
        ttl = _WEATHER_TTL if cached.get("wx") else _WEATHER_FAIL_TTL
        if cached.get("zip") == BRIEF_ZIP and age < ttl:
            return cached.get("wx")
    except Exception:
        pass
    wx = _fetch_weather(BRIEF_ZIP)
    # The FAILURE is cached as well, on a shorter clock. Two urlopen calls with
    # a 6s timeout each is a 12s stall on the scheduler thread, and a box with
    # no DNS pays it on every single brief - including the ones a human asks
    # for by saying "say that again".
    memory.meta_set("routine_weather",
                    json.dumps({"zip": BRIEF_ZIP, "ts": now, "wx": wx}))
    return wx


def _fetch_weather(zip_code, timeout=6):
    # Lazy import per the house convention: this is needed once a day at most,
    # and urllib drags in ssl and http.client behind it.
    from urllib.request import urlopen

    def _json(url):
        with urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        p = _json(f"https://api.zippopotam.us/us/{zip_code}")["places"][0]
        d = _json("https://api.open-meteo.com/v1/forecast"
                  f"?latitude={p['latitude']}&longitude={p['longitude']}"
                  "&current=temperature_2m,weather_code"
                  "&daily=temperature_2m_max&temperature_unit=fahrenheit"
                  "&timezone=auto&forecast_days=1")
        cur = d["current"]
        hi = (d.get("daily") or {}).get("temperature_2m_max") or [None]
        return {"temp": int(round(cur["temperature_2m"])),
                "desc": _WMO.get(int(cur.get("weather_code", -1)), "out there"),
                "hi": int(round(hi[0])) if hi and hi[0] is not None else None,
                "place": p.get("place name", "")}
    except Exception as e:
        # Offline, DNS down, rate-limited: the clause simply disappears.
        print("[routines] weather unavailable:", e)
        return None
