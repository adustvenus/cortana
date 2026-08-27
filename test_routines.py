"""Routines tests.

A routine's failures are the kind you only notice weeks later, in the wrong
direction. Level-triggering instead of edge-triggering does not raise, does not
log, and turns a health check into something you mute. A calendar reminder that
re-fires on the next tick is indistinguishable from the feature working until
it has said the same thing eight times. A brief that returns "" because Google
revoked a token is silence on the one morning you needed it.

Every test below is one of those, named after the failure it prevents.
"""
import datetime
import json
import sys
import time
import types

import pytest

import calendar_state
import memory
import notify
import presence
import routines
import schedule


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Throwaway database, throwaway state files, and a captured delivery.

    routines._last_eval and the three state-file paths are all module globals;
    without this fixture one test's leftovers decide the next test's result,
    which is worse than no test at all.

    notify.deliver is captured rather than routed, deliberately. WHERE a line
    lands is notify's policy table and test_notify.py already pins it
    exhaustively; asserting it again here would only mean these tests break
    when that table changes, which tells nobody anything about routines.
    """
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.init()
    monkeypatch.setattr(calendar_state, "STATE_FILE", tmp_path / "calendar_state.json")
    monkeypatch.setattr(presence, "STATE_FILE", tmp_path / "presence_desk.json")
    monkeypatch.setattr(routines, "SENTINEL_FILE", tmp_path / "sentinel_state.json")
    routines._last_eval["ts"] = 0.0

    said = []

    def _capture(text, urgency="normal", src="", ref=0, desk_state=None):
        said.append(text)
        return ["desk"]

    monkeypatch.setattr(notify, "deliver", _capture)
    return said


# ── helpers ────────────────────────────────────────────────────────────────
def _sentinel(worst="ok", checks=(), metrics=None, now=None, age=0.0,
              stamped=True):
    """Write sentinel_state.json as the sentinel would have `age` seconds ago.

    `now` is the clock the TEST is driving, not the wall clock. Stamping the
    real time here while the test ticks at now+4000 makes a fresh file read as
    stale, which is a test artifact that looks exactly like the staleness guard
    working - the most expensive kind of green test.
    """
    payload = {"worst": worst, "checks": list(checks)}
    if stamped:
        payload["ts"] = (time.time() if now is None else now) - age
    if metrics is not None:
        payload["metrics"] = metrics
    routines.SENTINEL_FILE.write_text(json.dumps(payload))


def _desk(state):
    presence.STATE_FILE.write_text(json.dumps({"state": state, "ts": time.time()}))


def _calendar(events, day=None):
    calendar_state.STATE_FILE.write_text(json.dumps({
        "events": list(events), "error": "", "ts": time.time(),
        "day": day or datetime.date.today().isoformat()}))


def _today_at(hh, mm):
    d = datetime.date.today()
    return schedule.to_epoch(datetime.datetime(d.year, d.month, d.day, hh, mm))


# ── edge vs level: the whole difference between a sentinel and a nag ───────
def test_a_health_routine_fires_once_and_stays_quiet_while_the_condition_holds(db):
    """Level-triggering would say "disk is full" every thirty seconds forever.

    The condition below is true on every single tick; the routine must speak on
    the first one and never again until it clears.
    """
    said = db
    _sentinel("bad", [{"key": "disk", "label": "Disk", "state": "bad",
                       "detail": "92 percent full"}])
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.",
                     "cond": {"metric": "disk", "op": "==", "value": "bad"}})

    now = time.time()
    assert routines.tick(now=now, min_interval=0) == 1
    for i in range(1, 6):
        assert routines.tick(now=now + i, min_interval=0) == 0
    assert said == ["Disk is in trouble."]
    assert routines.get("disk")["fires"] == 1


def test_a_condition_that_clears_and_returns_fires_again(db):
    """The other half of edge-triggering: going quiet must not mean going deaf."""
    said = db
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 0,
                     "cond": {"metric": "worst", "op": ">=", "value": "warn"}})

    now = time.time()
    _sentinel("bad", now=now)
    assert routines.tick(now=now, min_interval=0) == 1
    _sentinel("ok", now=now + 10)
    assert routines.tick(now=now + 10, min_interval=0) == 0
    _sentinel("bad", now=now + 20)
    assert routines.tick(now=now + 20, min_interval=0) == 1
    assert len(said) == 2


def test_min_gap_suppresses_a_second_fire_after_a_flap(db):
    """A metric hovering on its threshold crosses it repeatedly. Edge-triggering
    alone would fire on every crossing; min_gap is what makes six hours of
    flapping into one sentence."""
    said = db
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 3600,
                     "cond": {"metric": "worst", "op": ">=", "value": "warn"}})
    now = time.time()

    _sentinel("bad", now=now)
    assert routines.tick(now=now, min_interval=0) == 1
    _sentinel("ok", now=now + 10)
    routines.tick(now=now + 10, min_interval=0)
    _sentinel("bad", now=now + 20)
    assert routines.tick(now=now + 20, min_interval=0) == 0, "min_gap did not suppress"
    assert said == ["Disk is in trouble."]

    # ... and once the gap has elapsed it is allowed to speak again.
    _sentinel("ok", now=now + 4000)
    routines.tick(now=now + 4000, min_interval=0)
    _sentinel("bad", now=now + 4010)
    assert routines.tick(now=now + 4010, min_interval=0) == 1
    assert len(said) == 2


def test_a_flap_inside_the_quiet_period_does_not_bank_a_second_fire(db):
    """min_gap gates the speaking, never the edge update.

    If it gated both, the routine would still believe the condition was false
    when the gap expired, read the next tick as a fresh transition, and fire
    immediately - turning a quiet period into a delayed nag.
    """
    said = db
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 600,
                     "cond": {"metric": "worst", "op": ">=", "value": "warn"}})
    now = time.time()
    _sentinel("bad", now=now)
    routines.tick(now=now, min_interval=0)
    _sentinel("ok", now=now + 5)
    routines.tick(now=now + 5, min_interval=0)
    _sentinel("bad", now=now + 10)
    routines.tick(now=now + 10, min_interval=0)          # suppressed by the gap

    # The gap has now expired and the condition never changed since the
    # suppressed tick. Nothing new happened, so nothing may be said.
    _sentinel("bad", now=now + 700)
    assert routines.tick(now=now + 700, min_interval=0) == 0
    assert said == ["Disk is in trouble."]


def test_an_unreadable_sentinel_leaves_the_edge_exactly_where_it_was(db):
    """A stale or missing state file is not evidence that anything changed.

    Treating a missing file as "condition false" would clear the edge and make
    the very next reading look like a fresh transition, so every sentinel
    restart would re-announce whatever was already wrong.
    """
    said = db
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 0,
                     "cond": {"metric": "worst", "op": ">=", "value": "warn"}})
    now = time.time()
    _sentinel("bad", now=now)
    routines.tick(now=now, min_interval=0)
    assert routines.get("disk")["edge"] == "1"

    _sentinel("bad", now=now, age=99999)        # sentinel died hours ago
    assert routines.tick(now=now + 10, min_interval=0) == 0
    assert routines.get("disk")["edge"] == "1", "a stale file rewrote the edge"

    _sentinel("bad", now=now + 20)              # sentinel comes back, unchanged
    assert routines.tick(now=now + 20, min_interval=0) == 0
    assert said == ["Disk is in trouble."]


def test_a_sentinel_file_with_no_timestamp_still_goes_stale(db):
    """`ts` is an agreed key on sentinel_state.json, not an enforced one, and
    sentinel.py belongs to another owner. Trusting a file that omits it meant a
    sentinel dead for three weeks was still believed - the stale "ok" this
    guard exists to refuse, arriving through the one door it did not check.
    mtime is a worse clock than the writer's own stamp and a far better one
    than none at all.
    """
    said = db
    _sentinel("bad", stamped=False)
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 0,
                     "cond": {"metric": "worst", "op": "==", "value": "bad"}})
    assert routines.tick(now=time.time() + 30 * 86400, min_interval=0) == 0
    assert said == []
    # ...and the same file is still honoured while it is genuinely fresh.
    assert routines.tick(now=time.time(), min_interval=0) == 1


def test_a_health_condition_with_no_value_is_refused_not_silently_never_fired(db):
    """{"metric":"worst","op":"=="} with nothing to compare against compares
    every reading to the string "None". Nothing ever equals it, _compare
    returns a plain False rather than "uncomparable", so not even last_error
    records why - the routine sits in the list looking healthy forever.
    """
    out = routines.create({"name": "d", "trigger": "health", "action": "say",
                           "text": "x", "cond": {"metric": "worst", "op": "=="}})
    assert "value" in out
    assert routines.items() == [], "a rejected routine left a row behind"


def test_an_uncomparable_condition_is_reported_rather_than_silently_never_firing(db):
    """{"metric":"worst","op":">","value":"tuesday"} can never be true. A routine
    that can never fire looks exactly like a routine that is working, so the
    reason has to be written down somewhere a human can find it."""
    _sentinel("bad")
    routines.create({"name": "nonsense", "trigger": "health", "action": "say",
                     "text": "x", "cond": {"metric": "worst", "op": ">",
                                           "value": "tuesday"}})
    assert routines.tick(now=time.time(), min_interval=0) == 0
    assert "compare" in routines.last_error("nonsense")


def test_a_numeric_metric_compares_as_a_number_not_as_a_string(db):
    """'9' > '85' is True for strings and False for numbers. Getting this wrong
    means a disk alarm that fires at 9 percent and stays silent at 92."""
    said = db
    now = time.time()
    _sentinel("ok", metrics={"disk_pct": 9}, now=now)
    routines.create({"name": "disk pct", "trigger": "health", "action": "say",
                     "text": "Disk is filling up.",
                     "cond": {"metric": "disk_pct", "op": ">", "value": 85}})
    assert routines.tick(now=now, min_interval=0) == 0

    _sentinel("ok", metrics={"disk_pct": 92}, now=now + 10)
    assert routines.tick(now=now + 10, min_interval=0) == 1
    assert said == ["Disk is filling up."]


# ── presence ───────────────────────────────────────────────────────────────
def test_presence_fires_on_the_named_transition_only(db):
    said = db
    routines.create({"name": "welcome", "trigger": "presence", "action": "say",
                     "text": "Welcome back.", "min_gap": 0,
                     "cond": {"from": "away", "to": "present"}})
    now = time.time()

    _desk("present")            # first evaluation ever: no `from` to match
    assert routines.tick(now=now, min_interval=0) == 0
    _desk("asleep")
    assert routines.tick(now=now + 10, min_interval=0) == 0
    _desk("present")            # asleep -> present is not away -> present
    assert routines.tick(now=now + 20, min_interval=0) == 0
    _desk("away")
    routines.tick(now=now + 30, min_interval=0)
    _desk("present")
    assert routines.tick(now=now + 40, min_interval=0) == 1
    assert said == ["Welcome back."]


def test_a_routine_created_while_the_state_already_holds_stays_quiet(db):
    """Creating "tell me when I get back" while sitting at the desk must not
    greet you on the spot - the empty edge matches no `from`."""
    said = db
    _desk("present")
    routines.create({"name": "welcome", "trigger": "presence", "action": "say",
                     "text": "Welcome back.",
                     "cond": {"from": "away", "to": "present"}})
    assert routines.tick(now=time.time(), min_interval=0) == 0
    assert said == []


def test_a_misspelt_from_state_is_refused_rather_than_never_matching(db):
    """`to` was validated and `from` was not, so {"from":"awya"} was accepted,
    listed and enabled - and could never match, because read_desk() only ever
    returns four words. Silence is indistinguishable from working."""
    out = routines.create({"name": "welcome", "trigger": "presence",
                           "action": "say", "text": "hi",
                           "cond": {"from": "awya", "to": "present"}})
    assert "awya" in out
    assert routines.items() == []


# ── calendar ───────────────────────────────────────────────────────────────
def test_a_calendar_event_fires_exactly_once(db):
    """The lookahead window is true on every tick inside it. Without the fired-key
    set the same meeting would be announced every thirty seconds for ten
    minutes."""
    said = db
    now = _today_at(9, 0)
    _calendar([{"time": "09:10", "title": "Standup", "allDay": False, "past": False}])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event} in {minutes} minutes.",
                     "cond": {"when": "starts_in", "minutes": 15}})

    assert routines.tick(now=now, min_interval=0) == 1
    for i in range(1, 6):
        assert routines.tick(now=now + i * 60, min_interval=0) == 0
    assert said == ["Standup in 10 minutes."]


def test_an_event_outside_the_window_does_not_fire_yet(db):
    said = db
    now = _today_at(9, 0)
    _calendar([{"time": "11:30", "title": "Dentist", "allDay": False, "past": False}])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event} soon.",
                     "cond": {"when": "starts_in", "minutes": 15}})
    assert routines.tick(now=now, min_interval=0) == 0
    assert said == []


def test_a_five_past_midnight_event_cannot_fire_at_five_to(db):
    """The documented midnight wrinkle, pinned.

    calendar_state only ever holds TODAY's events, so an event listed as 00:05
    is twenty-three hours BEHIND 23:55, not ten minutes ahead. Comparing clock
    faces instead of absolute epochs would announce it - and then announce it
    again after the day rolled over and the fired-key set reset.
    """
    said = db
    _calendar([{"time": "00:05", "title": "Backup window",
                "allDay": False, "past": False}])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event} in {minutes} minutes.",
                     "cond": {"when": "starts_in", "minutes": 30}})

    assert routines.tick(now=_today_at(23, 55), min_interval=0) == 0
    assert said == []


def test_an_all_day_event_never_triggers_a_starts_in_routine(db):
    said = db
    _calendar([{"time": "all-day", "title": "Holiday", "allDay": True, "past": False}])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event}.", "cond": {"when": "starts_in", "minutes": 30}})
    assert routines.tick(now=_today_at(9, 0), min_interval=0) == 0
    assert said == []


def test_both_events_in_one_window_are_announced(db):
    """Back-to-back meetings put two events inside one lookahead pass, and both
    keys go into the fired set. Announcing only the first therefore marked the
    second as delivered and then never said it - the 9:05 review vanished, with
    no error anywhere, on exactly the mornings it mattered."""
    said = db
    now = _today_at(8, 50)
    _calendar([{"time": "09:00", "title": "Standup", "allDay": False, "past": False},
               {"time": "09:05", "title": "Review", "allDay": False, "past": False}])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event} in {minutes} minutes.", "min_gap": 0,
                     "cond": {"when": "starts_in", "minutes": 15}})
    routines.tick(now=now, min_interval=0)
    assert said == ["Standup in 10 minutes.", "Review in 15 minutes."]
    assert routines.tick(now=now + 60, min_interval=0) == 0


def test_a_busy_day_cannot_overflow_the_edge_into_re_announcing_everything(db):
    """The edge column is capped. Capping it by truncating the STRING turned
    valid JSON into a fragment json.loads rejects, so the fired-key set read
    back empty and every event still inside the window was announced again on
    the next tick, and the next. A day with two dozen meetings is both what
    overflows the cap and what makes that failure loudest.
    """
    said = db
    now = _today_at(9, 0)
    _calendar([{"time": "09:%02d" % i,
                "title": "Long meeting title number %d " % i + "x" * 40,
                "allDay": False, "past": False} for i in range(30)])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event}.", "min_gap": 0,
                     "cond": {"when": "starts_in", "minutes": 60}})
    for i in range(15):
        routines.tick(now=now + i, min_interval=0)

    edge = routines.get("meetings")["edge"]
    assert len(edge) <= routines._EDGE_MAX
    json.loads(edge)                       # raises if the cap corrupted it
    assert len(said) == len(set(said)), "an event was announced twice"


def test_a_wide_window_is_spread_over_ticks_rather_than_said_all_at_once(db):
    """A routine created at nine with a whole-day window has every remaining
    meeting inside it on its first pass. Speaking them all is a monologue; the
    old alternative - key them all and say one - lost the rest silently. The
    cap defers instead, so nothing is dropped and nothing is a monologue."""
    said = db
    now = _today_at(9, 0)
    _calendar([{"time": "1%d:00" % i, "title": "Meeting %d" % i,
                "allDay": False, "past": False} for i in range(6)])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event}.", "min_gap": 0,
                     "cond": {"when": "starts_in", "minutes": 600}})
    assert routines.tick(now=now, min_interval=0) == routines._CAL_BURST
    assert routines.tick(now=now + 30, min_interval=0) == routines._CAL_BURST
    assert routines.tick(now=now + 60, min_interval=0) == 0
    assert len(said) == 6 and len(set(said)) == 6


def test_yesterdays_fired_keys_do_not_survive_into_today(db):
    """The key set is reset on a day change so it cannot grow without bound. It
    must survive within the day, which is what the fire-once test covers, and
    be gone across one, which is this."""
    now = _today_at(9, 0)
    _calendar([{"time": "09:10", "title": "Standup", "allDay": False, "past": False}])
    routines.create({"name": "meetings", "trigger": "calendar", "action": "say",
                     "text": "{event}.", "cond": {"when": "starts_in", "minutes": 15}})
    routines.tick(now=now, min_interval=0)

    edge = json.loads(routines.get("meetings")["edge"])
    assert edge["day"] == datetime.date.today().isoformat()
    assert len(edge["keys"]) == 1
    assert edge["keys"][0].startswith(edge["day"]), "key is not day-qualified"


# ── failure containment ────────────────────────────────────────────────────
def test_a_failing_action_does_not_strand_the_routine(db):
    """A raised action must not leave the routine disabled, un-edged, or able to
    retry on every tick for the rest of the day."""
    def boom(prompt):
        raise RuntimeError("orchestrator is down")

    _sentinel("bad")
    routines.create({"name": "brief me", "trigger": "health", "action": "turn",
                     "prompt": "summarise the failure", "min_gap": 0,
                     "cond": {"metric": "worst", "op": ">=", "value": "warn"}})
    now = time.time()

    assert routines.tick(turn=boom, now=now, min_interval=0) == 0
    row = routines.get("brief me")
    assert row["enabled"] == 1, "a failure disabled the routine"
    assert row["edge"] == "1", "the edge was not advanced, so it will retry forever"
    assert row["fires"] == 0, "a failed action was counted as a delivery"
    assert row["last_fired"], "min_gap has nothing to work from"
    assert "orchestrator is down" in routines.last_error("brief me")

    # And the next tick is a plain no-op rather than a second explosion.
    assert routines.tick(turn=boom, now=now + 5, min_interval=0) == 0


def test_a_turn_routine_with_no_runner_injected_is_skipped_not_crashed(db):
    """main.py owns the _turn_lock; a process that cannot supply one must not
    reach into orchestrator's module globals from a background thread."""
    _sentinel("bad")
    routines.create({"name": "thinker", "trigger": "health", "action": "turn",
                     "prompt": "think", "cond": {"metric": "worst", "op": "==",
                                                 "value": "bad"}})
    assert routines.tick(now=time.time(), min_interval=0) == 0
    assert "turn runner" in routines.last_error("thinker")


def test_a_delegate_refusal_is_put_on_a_surface(db):
    """tasks.start returns a line written for the LEAD to relay. Fired from a
    routine there is no lead and no user, so "All 4 task slots are busy" would
    vanish completely unless it is delivered deliberately."""
    said = db
    # A stub rather than the real tasks module: this test is about the refusal
    # STRING reaching a surface, and spinning up real worker threads to prove
    # that would make it slow and flaky for nothing.
    stub = types.ModuleType("tasks")
    stub.start = lambda agent, task, runner: "All 4 task slots are busy: task 1 (dev)."
    real = sys.modules.get("tasks")
    sys.modules["tasks"] = stub
    try:
        _sentinel("bad")
        routines.create({"name": "digger", "trigger": "health", "action": "delegate",
                         "agent": "research", "task": "find out why",
                         "cond": {"metric": "worst", "op": "==", "value": "bad"}})
        assert routines.tick(run_agent=lambda a, t, cancel=None: "",
                             now=time.time(), min_interval=0) == 1
    finally:
        if real is None:
            sys.modules.pop("tasks", None)
        else:
            sys.modules["tasks"] = real
    assert any("slots are busy" in line for line in said)


# ── the one engine rule ────────────────────────────────────────────────────
def test_a_clock_routine_is_a_schedules_row_and_not_a_second_engine(db):
    """Two places expanding "7am daily" means two places to fix the same DST
    bug. A clock routine must leave the recurrence entirely to schedule.py and
    be skipped by the edge evaluator."""
    pytest.importorskip("dateutil")
    out = routines.create({"name": "morning brief", "action": "brief",
                           "rrule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0"})
    assert "morning brief" in out

    up = schedule.upcoming()
    assert len(up) == 1 and up[0]["repeats"] is True
    row = schedule.get(up[0]["id"])
    assert row["payload"] == {"routine": "morning brief"}
    assert row["title"] == "morning brief", "the say fallback would speak nothing"

    assert routines.get("morning brief")["trigger"] == "clock"
    assert routines.tick(now=time.time(), min_interval=0) == 0


def test_editing_a_clock_routine_does_not_stack_two_alarms(db):
    pytest.importorskip("dateutil")
    for _ in range(3):
        routines.create({"name": "morning brief", "action": "brief",
                         "rrule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0"})
    assert len(schedule.upcoming()) == 1
    assert len(routines.items()) == 1


def test_a_rejected_recurrence_rule_leaves_no_routine_behind(db):
    """The rrule was validated AFTER the row was written. The user heard "bad
    recurrence rule" while the routine sat in the list, enabled, with no
    schedules row behind it - a thing that says it is on and can never run."""
    pytest.importorskip("dateutil")
    out = routines.create({"name": "morning brief", "action": "brief",
                           "rrule": "FREQ=NONSENSE"})
    assert "recurrence" in out.lower()
    assert routines.items() == [], "a rejected routine left a row behind"
    assert schedule.upcoming() == []


def test_editing_a_switched_off_clock_routine_does_not_arm_two_alarms(db):
    """A disabled routine parks its clock row in 'paused'. Only pending and
    firing rows were cancelled on re-arm, so editing a routine while it was off
    left the old row in place and turning it back on armed both - one 7am brief
    spoken twice, from a state nothing in the interface shows."""
    pytest.importorskip("dateutil")
    rule = "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0"
    routines.create({"name": "mb", "action": "brief", "rrule": rule})
    routines.set_state("mb", enabled=False)
    routines.create({"name": "mb", "action": "brief", "rrule": rule})
    routines.set_state("mb", enabled=True)
    assert len(schedule.upcoming()) == 1


def test_deleting_a_switched_off_routine_cancels_its_parked_clock_row(db):
    """The same 'paused' blind spot from the other end: the routine went away
    and its alarm stayed parked, ready to be re-armed alongside a new one the
    next time that name was used."""
    pytest.importorskip("dateutil")
    routines.create({"name": "mb", "action": "brief", "rrule": "FREQ=DAILY;BYHOUR=7"})
    routines.set_state("mb", enabled=False)
    routines.set_state("mb", delete=True)
    con = memory.connect()
    states = [r[0] for r in con.execute(
        "SELECT state FROM schedules WHERE kind='routine'").fetchall()]
    con.close()
    assert states and all(st == "cancelled" for st in states), states


def test_run_executes_a_named_routine_regardless_of_trigger(db):
    """This is the clock path: schedules fires, main.py sees {"routine": name}
    in the payload and lands here."""
    said = db
    routines.create({"name": "greeting", "trigger": "presence", "action": "say",
                     "text": "Hello.", "cond": {"to": "present"}})
    assert "greeting" in routines.run("greeting")
    assert said == ["Hello."]
    assert routines.get("greeting")["fires"] == 1


# ── enable / disable / delete ──────────────────────────────────────────────
def test_a_disabled_routine_is_not_evaluated(db):
    said = db
    _sentinel("bad")
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "x", "cond": {"metric": "worst", "op": "==",
                                           "value": "bad"}})
    routines.set_state("disk", enabled=False)
    assert routines.tick(now=time.time(), min_interval=0) == 0
    assert said == []


def test_re_enabling_clears_the_edge_so_the_next_transition_is_heard(db):
    """Turned off through a whole away-and-back cycle, the stored edge is a lie.
    Honouring it would swallow the first real transition after re-enabling,
    which reads as "turning it back on didn't work"."""
    said = db
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 0,
                     "cond": {"metric": "worst", "op": "==", "value": "bad"}})
    now = time.time()
    _sentinel("bad")
    routines.tick(now=now, min_interval=0)
    routines.set_state("disk", enabled=False)
    routines.set_state("disk", enabled=True)
    assert routines.get("disk")["edge"] == ""

    assert routines.tick(now=now + 10, min_interval=0) == 1
    assert len(said) == 2


def test_delete_removes_the_routine_and_its_clock_row(db):
    pytest.importorskip("dateutil")
    routines.create({"name": "morning brief", "action": "brief",
                     "rrule": "FREQ=DAILY;BYHOUR=7"})
    assert "Deleted" in routines.set_state("morning brief", delete=True)
    assert routines.items() == []
    assert schedule.upcoming() == [], "the alarm outlived the routine it ran"


def test_deleting_a_routine_takes_its_error_note_with_it(db):
    """Nothing else prunes `meta`. A note keyed by a user-supplied name that
    outlives the routine it describes is a row that can only accumulate."""
    routines.create({"name": "d", "trigger": "presence", "action": "say",
                     "text": "hi", "cond": {"to": "present"}})
    routines._note_error("d", "something broke")
    routines.set_state("d", delete=True)
    assert routines.last_error("d") == ""


def test_the_spoken_routine_list_is_bounded(db):
    """set_state() with no name is read aloud. schedule.summary() caps its list
    at 25 for the same reason: sixty entries is a two-minute monologue nobody
    can usefully interrupt."""
    for i in range(60):
        routines.create({"name": "r%d" % i, "trigger": "presence",
                         "action": "say", "text": "hi", "cond": {"to": "present"}})
    out = routines.set_state()
    assert len(out) < 1200
    assert "35 more" in out


def test_routine_set_with_no_name_lists_what_exists(db):
    routines.create({"name": "welcome", "trigger": "presence", "action": "say",
                     "text": "hi", "cond": {"to": "present"}})
    out = routines.set_state()
    assert "welcome" in out and "presence" in out


# ── input validation ───────────────────────────────────────────────────────
def test_a_bad_trigger_is_refused_before_a_row_exists(db):
    out = routines.create({"name": "x", "trigger": "weather", "action": "say",
                           "text": "hi"})
    assert "calendar" in out
    assert routines.items() == [], "a rejected routine left a row behind"


def test_a_presence_routine_without_a_target_state_is_refused(db):
    out = routines.create({"name": "x", "trigger": "presence", "action": "say",
                           "text": "hi", "cond": {"from": "away"}})
    assert "present" in out
    assert routines.items() == []


def test_a_say_routine_with_nothing_to_say_is_refused(db):
    out = routines.create({"name": "x", "trigger": "presence", "action": "say",
                           "cond": {"to": "present"}})
    assert "say" in out.lower()
    assert routines.items() == []


# ── the tick guard ─────────────────────────────────────────────────────────
def test_the_evaluator_does_not_run_on_every_five_second_scheduler_tick(db):
    """A prior bug in this repo spawned a systemctl process 24 times a minute
    forever. Three state-file reads and a sqlite query on the 5s scheduler tick
    is the same mistake in a different costume."""
    _sentinel("bad")
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "x", "cond": {"metric": "worst", "op": "==",
                                           "value": "bad"}})
    now = time.time()
    assert routines.tick(now=now, min_interval=30) == 1
    assert routines.tick(now=now + 5, min_interval=30) == 0
    assert routines.tick(now=now + 10, min_interval=30) == 0


def test_a_clock_that_steps_backwards_does_not_freeze_the_evaluator(db):
    """time.time() is not monotonic. An NTP correction, or a laptop resuming
    with a fixed RTC, can leave the last-evaluated stamp in the FUTURE - and a
    bare `now - last < gap` is true for the whole length of that jump. The tick
    keeps being called on schedule and every routine silently stops being
    evaluated, which is the hardest kind of dead to notice.
    """
    said = db
    now = time.time()
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 0,
                     "cond": {"metric": "worst", "op": "==", "value": "bad"}})
    # One quiet pass an hour into the future, then the clock is corrected back
    # and the disk goes bad. The correction must not swallow the alarm.
    _sentinel("ok", now=now + 3600)
    assert routines.tick(now=now + 3600, min_interval=30) == 0
    _sentinel("bad", now=now)
    assert routines.tick(now=now, min_interval=30) == 1
    assert said == ["Disk is in trouble."]


def test_a_last_fired_stamp_from_the_future_does_not_mute_a_routine(db):
    """The same non-monotonic clock, one layer down: min_gap compares against
    last_fired, so a stamp written before the correction mutes the routine
    until the wall clock catches up with it - hours of silence from a health
    alarm, with nothing recorded anywhere."""
    said = db
    now = time.time()
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 600,
                     "cond": {"metric": "worst", "op": "==", "value": "bad"}})
    con = memory.connect()
    con.execute("UPDATE routines SET last_fired=? WHERE name='disk'", (now + 7200,))
    con.commit()
    con.close()
    _sentinel("bad", now=now)
    assert routines.tick(now=now, min_interval=0) == 1
    assert said == ["Disk is in trouble."]


def test_only_one_ticker_can_ever_speak_a_transition(db):
    """Phase 2 moves the tick into the bridge and both processes tick during
    the cutover. schedule.claim() survives that because it is a conditional
    UPDATE; the routines edge has to be one for the same reason, or the disk
    alarm is spoken once per process."""
    said = db
    _sentinel("bad")
    routines.create({"name": "disk", "trigger": "health", "action": "say",
                     "text": "Disk is in trouble.", "min_gap": 0,
                     "cond": {"metric": "worst", "op": "==", "value": "bad"}})
    row = routines.get("disk")
    assert routines._set_edge(row["id"], "1", row["edge"]) is True
    assert routines._set_edge(row["id"], "1", row["edge"]) is False, \
        "the edge write is unconditional, so both tickers fire"
    # The loser's evaluation now finds nothing left to announce.
    assert routines.tick(now=time.time(), min_interval=0) == 0
    assert said == []


# ── the morning brief ──────────────────────────────────────────────────────
def test_the_brief_still_speaks_when_every_source_is_missing(db):
    """No calendar file, no sentinel file, no tasks, no weather ZIP. A brief
    that returns "" here is silence on the one morning you needed it."""
    out = routines.brief(now=_today_at(7, 0))
    assert out.startswith("Good morning.")
    assert len(out) > 10


def test_the_brief_drops_only_the_clause_whose_source_is_broken(db, monkeypatch):
    """One dead source must cost one clause, not the paragraph."""
    monkeypatch.setattr(routines, "_c_calendar",
                        lambda now: (_ for _ in ()).throw(RuntimeError("token expired")))
    _sentinel("bad", [{"key": "disk", "label": "Disk", "state": "bad",
                       "detail": "is 92 percent full"}], now=_today_at(7, 0))

    out = routines.brief(now=_today_at(7, 0))
    assert "Good morning" in out
    assert "Disk is 92 percent full" in out


def test_the_brief_reads_the_calendar_the_overnight_work_and_the_sentinel(db):
    _calendar([{"time": "09:15", "title": "Standup", "allDay": False, "past": False},
               {"time": "08:00", "title": "Gym", "allDay": False, "past": True}])
    _sentinel("warn", [{"key": "backup", "label": "Backup", "state": "warn",
                        "detail": "last ran three days ago"}], now=_today_at(7, 0))
    memory.log_task(1, "research", "the market open note", "started", "")
    memory.log_task(1, "research", "the market open note", "done", "ok")
    memory.log_task(2, "dev", "the dashboard refactor", "started", "")

    out = routines.brief(now=_today_at(7, 0))

    assert "One thing on the calendar: Standup at 9:15." in out
    assert "research finished the market open note" in out
    assert "Still running: dev on the dashboard refactor." in out
    assert "Backup last ran three days ago" in out
    # Spoken aloud, so it has to be prose all the way through.
    assert "\n" not in out and "*" not in out and "http" not in out


def test_the_brief_says_the_calendar_is_clear_rather_than_saying_nothing(db):
    _calendar([])
    out = routines.brief(now=_today_at(7, 0))
    assert "calendar is clear" in out


def test_a_stale_calendar_file_drops_the_clause_instead_of_reading_yesterday(db):
    """calendar_state.read() already refuses yesterday's agenda; the brief must
    not paper over that by inventing "your calendar is clear", which is a
    different and confidently wrong statement."""
    _calendar([{"time": "09:15", "title": "Standup", "allDay": False, "past": False}],
              day="2020-01-01")
    out = routines.brief(now=_today_at(7, 0))
    assert "Standup" not in out
    assert "calendar" not in out.lower()


def test_the_brief_makes_no_network_call_without_a_configured_zip(db, monkeypatch):
    """Idle burn and offline behaviour both. An unconfigured ZIP must not reach
    for the network at all, let alone block the scheduler thread on a DNS
    timeout."""
    def forbidden(*a, **k):
        raise AssertionError("the brief tried to fetch weather with no ZIP set")

    monkeypatch.setattr(routines, "BRIEF_ZIP", "")
    monkeypatch.setattr(routines, "_fetch_weather", forbidden)
    assert routines._weather(time.time()) is None
    assert routines.brief(now=_today_at(7, 0))


def test_a_weather_reading_is_cached_rather_than_fetched_per_brief(db, monkeypatch):
    calls = []

    def fake(zip_code, timeout=6):
        calls.append(zip_code)
        return {"temp": 62, "desc": "Overcast", "hi": 78, "place": "Denver"}

    monkeypatch.setattr(routines, "BRIEF_ZIP", "80202")
    monkeypatch.setattr(routines, "_fetch_weather", fake)
    now = time.time()

    assert "62 and overcast, high of 78" in routines.brief(now=now)
    routines.brief(now=now + 60)
    assert calls == ["80202"], f"fetched {len(calls)} times"


def test_an_unreachable_weather_service_drops_only_its_clause(db, monkeypatch):
    monkeypatch.setattr(routines, "BRIEF_ZIP", "80202")
    monkeypatch.setattr(routines, "_fetch_weather", lambda z, timeout=6: None)
    out = routines.brief(now=_today_at(7, 0))
    assert out.startswith("Good morning.")


def test_a_failed_weather_lookup_is_not_retried_on_every_brief(db, monkeypatch):
    """Only the success was cached. Two urlopen calls at six seconds each is a
    twelve-second stall on the SCHEDULER thread - the one that also owes the
    user their 7am alarm - and a box with dead DNS paid it on every brief,
    including the ones a human asks for by saying "say that again".
    """
    calls = []
    monkeypatch.setattr(routines, "BRIEF_ZIP", "80202")
    monkeypatch.setattr(routines, "_fetch_weather",
                        lambda z, timeout=6: calls.append(z))
    now = time.time()
    for i in range(4):
        routines.brief(now=now + i)
    assert calls == ["80202"], "re-dialled a dead resolver %d times" % len(calls)

    # ...and it is forgotten sooner than a success, so a laptop that has just
    # found wifi is not left without weather for the full half hour.
    routines.brief(now=now + routines._WEATHER_FAIL_TTL + 1)
    assert len(calls) == 2


# ── the contract the dashboard and the phone parse ─────────────────────────
def test_items_matches_the_local_routines_contract_exactly(db):
    routines.create({"name": "welcome", "trigger": "presence", "action": "say",
                     "text": "hi", "cond": {"to": "present"}})
    (item,) = routines.items()
    assert set(item) == {"name", "trigger", "enabled", "lastFired", "fires"}
    assert item["enabled"] is True and item["fires"] == 0


def test_routine_errors_never_reach_the_system_prompt(db):
    """recall_all() dumps every kv row into the system prompt verbatim. An
    internal error string stored there would be read to the model every turn as
    if the user had asked to remember it."""
    routines._note_error("disk", "something broke")
    assert "something broke" not in memory.recall_all()
    assert routines.last_error("disk") == "something broke"
