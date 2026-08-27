"""Scheduler tests.

These exist because the scheduler's worst failures are all SILENT: an alarm
that drifts an hour when the clocks change, six missed reminders firing at once
on wake, or two tickers both speaking the same line. None of them raise, none
show up in a journal, and all of them are months away from when the code was
written. Every test below is one of those.
"""
import datetime
import threading
import time

import pytest

import memory
import schedule

TZ = "America/New_York"          # a zone that actually observes DST
DST_RULE = "FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the whole persistence layer at a throwaway file. memory.DB_PATH is
    read inside _c(), so patching the module attribute is enough - no need to
    reimport or to touch the real state.db."""
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.init()
    return tmp_path


def _insert(next_ts, rrule="", kind="reminder", catchup=86400, created=None):
    con = memory.connect()
    cur = con.execute(
        "INSERT INTO schedules(created, kind, title, action, payload, rrule, tz,"
        " next_ts, state, urgency, catchup, require_ack, nag_after, nag_count,"
        " nag_max, fired_ts, ack_ts, owner, last_error)"
        " VALUES(?,?,?,'say','{}',?,?,?,'pending','normal',?,0,600,0,0,NULL,NULL,'','')",
        (created or time.time(), kind, "test item", rrule, TZ, next_ts, catchup))
    con.commit()
    sid = cur.lastrowid
    con.close()
    return sid


# ── the one a human cannot check by reading ────────────────────────────────
def test_daily_rule_holds_7am_across_a_dst_boundary():
    """FREQ=DAILY;BYHOUR=7 must stay 07:00 WALL CLOCK either side of the change.

    Expanding the rule in UTC instead of local time passes every test that does
    not cross a boundary, then quietly turns a 7am alarm into a 6am one on the
    first Sunday in November.
    """
    start = schedule.to_epoch(datetime.datetime(2026, 10, 29, 7, 0), TZ)
    ts, seen = start, []
    for _ in range(6):
        ts = schedule.next_occurrence(DST_RULE, TZ, ts, start)
        assert ts is not None
        seen.append(schedule.to_local(ts, TZ).strftime("%Y-%m-%d %H:%M"))

    assert all(s.endswith(" 07:00") for s in seen), seen
    # and the window must genuinely span the 2026-11-01 fall-back, or the test
    # proves nothing at all
    assert any(s.startswith("2026-11-0") for s in seen), seen


def test_dst_boundary_gap_is_really_23_or_25_hours():
    """Sanity-check the fixture itself: if these two 07:00s are exactly 24h
    apart then this zone did not change, and the test above is vacuous."""
    a = schedule.to_epoch(datetime.datetime(2026, 10, 31, 7, 0), TZ)
    b = schedule.to_epoch(datetime.datetime(2026, 11, 1, 7, 0), TZ)
    assert (b - a) == 25 * 3600


# ── concurrency ────────────────────────────────────────────────────────────
def test_claim_is_exclusive_under_a_thread_race(db):
    """Both processes tick during the phase-1 -> phase-2 cutover. Exactly one
    may win, or the user hears the same reminder twice."""
    when = time.time()
    sid = _insert(when)
    wins, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()                       # maximise the overlap
        wins.append(schedule.claim(sid, when, "racer"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wins.count(True) == 1, wins
    assert schedule.get(sid)["state"] == "firing"


def test_claim_refuses_a_row_rescheduled_underneath_it(db):
    when = time.time()
    sid = _insert(when)
    assert schedule.claim(sid, when + 999, "racer") is False
    assert schedule.get(sid)["state"] == "pending"


# ── catch-up policy ────────────────────────────────────────────────────────
def test_six_hours_asleep_fires_the_latest_occurrence_only(db):
    """An hourly rule with six missed occurrences must yield ONE fire, and it
    must be the most recent - not the six-hours-stale first one."""
    now = time.time()
    row = {"next_ts": now - 6 * 3600, "rrule": "FREQ=HOURLY", "tz": TZ,
           "created": now - 7 * 3600, "catchup": 86400}

    fire, nxt = schedule.roll_forward(row, now)

    assert fire is not None
    assert now - fire < 3600, "fired a stale occurrence instead of the newest"
    assert nxt > now, "did not arm a future occurrence"


def test_a_timer_missed_by_hours_is_dropped_not_spoken(db):
    """catchup for a timer is 15 minutes: four hours late it is noise."""
    now = time.time()
    row = {"next_ts": now - 4 * 3600, "rrule": "", "tz": TZ,
           "created": now - 5 * 3600, "catchup": 900}
    fire, nxt = schedule.roll_forward(row, now)
    assert fire is None
    assert nxt is None


def test_a_timer_missed_by_seconds_still_fires(db):
    now = time.time()
    row = {"next_ts": now - 30, "rrule": "", "tz": TZ,
           "created": now - 600, "catchup": 900}
    fire, nxt = schedule.roll_forward(row, now)
    assert fire == now - 30
    assert nxt is None


def test_a_month_long_backlog_resolves_fast_and_fires_once(db):
    """A per-minute rule on a box that was off for a month is ~57k missed
    occurrences. Stepping through them re-compiles the rule per step and
    rescans from dtstart on every call - O(missed^2), i.e. a hang. This is the
    regression test for that: one recent fire, one future arm, quickly."""
    now = time.time()
    row = {"next_ts": now - 40 * 86400, "rrule": "FREQ=MINUTELY", "tz": TZ,
           "created": now - 41 * 86400, "catchup": 86400}

    started = time.time()
    fire, nxt = schedule.roll_forward(row, now)
    elapsed = time.time() - started

    assert elapsed < 5, f"backlog took {elapsed:.1f}s - the O(n^2) walk is back"
    assert fire is not None and now - fire < 120, "did not pick the newest occurrence"
    assert nxt is not None and nxt > now


def test_per_second_recurrence_is_refused(db):
    """SECONDLY is never what anyone means, and it makes every catch-up
    expansion scan millions of occurrences."""
    out = schedule.create({"text": "x", "when": "2099-01-01T07:00:00",
                           "rrule": "FREQ=SECONDLY"})
    assert "second" in out.lower()
    assert schedule.upcoming() == []


# ── crash recovery ─────────────────────────────────────────────────────────
def test_recover_returns_a_stranded_firing_row_to_pending(db):
    when = time.time()
    sid = _insert(when)
    schedule.claim(sid, when, "dead-owner")
    con = memory.connect()                   # age the claim past the stale window
    con.execute("UPDATE schedules SET fired_ts=? WHERE id=?", (time.time() - 600, sid))
    con.commit()
    con.close()

    assert schedule.recover() == 1
    row = schedule.get(sid)
    assert row["state"] == "pending"
    assert row["next_ts"] == when, "recovery must not move the due time"


def test_recover_leaves_a_fresh_claim_alone(db):
    when = time.time()
    sid = _insert(when)
    schedule.claim(sid, when, "live-owner")
    assert schedule.recover() == 0
    assert schedule.get(sid)["state"] == "firing"


# ── input validation ───────────────────────────────────────────────────────
def test_a_garbage_rrule_is_rejected_at_create_time(db):
    out = schedule.create({"text": "x", "when": "2099-01-01T07:00:00",
                           "rrule": "FREQ=FORTNIGHTLY;BYHOUR=squid"})
    assert "recurrence" in out.lower()
    assert schedule.upcoming() == [], "a bad rule must not leave a row behind"


def test_a_one_shot_in_the_past_is_refused_out_loud(db):
    past = datetime.datetime.now() - datetime.timedelta(hours=3)
    out = schedule.create({"text": "x", "when": past.isoformat()})
    assert "past" in out.lower()
    assert schedule.upcoming() == []


def test_a_valid_reminder_round_trips(db):
    soon = datetime.datetime.now() + datetime.timedelta(minutes=20)
    out = schedule.create({"text": "call the dentist", "when": soon.isoformat()})
    assert "id" in out
    up = schedule.upcoming()
    assert len(up) == 1
    assert up[0]["title"] == "call the dentist"
    assert up[0]["repeats"] is False


def test_cancel_closes_the_row_and_clears_the_due_time(db):
    soon = datetime.datetime.now() + datetime.timedelta(minutes=20)
    schedule.create({"text": "x", "when": soon.isoformat()})
    sid = schedule.upcoming()[0]["id"]
    assert "Cancelled" in schedule.set_state(sid, cancel=True)
    assert schedule.upcoming() == []


def test_due_only_returns_rows_that_are_actually_due(db):
    now = time.time()
    _insert(now - 10)
    _insert(now + 3600)
    assert len(schedule.due(now)) == 1


# ── the tick, end to end ───────────────────────────────────────────────────
def test_a_due_reminder_fires_exactly_once(db):
    """The whole point of the feature, and the thing a user would notice
    instantly if it were wrong."""
    fired = []
    _insert(time.time() - 5)

    assert schedule.tick(lambda row, ts: fired.append(row["title"])) == 1
    assert fired == ["test item"]

    # Second pass must be a no-op: a one-shot is spent.
    assert schedule.tick(lambda row, ts: fired.append(row["title"])) == 0
    assert fired == ["test item"]


def test_a_recurring_item_fires_and_rearms(db):
    now = time.time()
    sid = _insert(now - 5, rrule="FREQ=DAILY;BYHOUR=7;BYMINUTE=0;BYSECOND=0",
                  created=now - 86400)
    fired = []

    assert schedule.tick(lambda row, ts: fired.append(ts), now=now) == 1
    row = schedule.get(sid)
    assert row["state"] == "pending", "recurring item was closed instead of re-armed"
    assert row["next_ts"] > now, "next occurrence is not in the future"


def test_two_tickers_fire_a_due_item_once_between_them(db):
    """Both processes tick during the phase-1 -> phase-2 cutover."""
    _insert(time.time() - 5)
    fired = []

    def run(owner):
        schedule.tick(lambda row, ts: fired.append(owner), owner=owner)

    threads = [threading.Thread(target=run, args=(f"t{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fired) == 1, f"fired {len(fired)} times: {fired}"


def test_a_failing_action_does_not_strand_the_row_in_firing(db):
    """If a raised action left state='firing', the row would sit there until the
    60s recovery sweep and then re-fire forever. The finally: in tick() is what
    stops that, and this is its regression test."""
    sid = _insert(time.time() - 5)

    def boom(row, ts):
        raise RuntimeError("tts is down")

    schedule.tick(boom)
    row = schedule.get(sid)
    assert row["state"] != "firing", "row stranded mid-fire"
    assert "tts is down" in (row["last_error"] or "")


def test_a_missed_timer_is_marked_missed_and_never_spoken(db):
    fired = []
    _insert(time.time() - 4 * 3600, kind="timer", catchup=900)
    assert schedule.tick(lambda row, ts: fired.append(row)) == 0
    assert fired == []


# ── the kv-pollution guard ─────────────────────────────────────────────────
def test_meta_does_not_leak_into_the_prompt(db):
    """recall_all() is injected into the system prompt verbatim. Internal
    cursors must live in `meta`, never in `kv`, or every turn reads them aloud
    to the model as if the user had asked to remember them."""
    memory.meta_set("inbox_seq", 42)
    memory.remember("trades", "NQ")
    assert memory.meta_get("inbox_seq") == "42"
    assert "inbox_seq" not in memory.recall_all()
    assert "trades" in memory.recall_all()
