"""Delivery-routing and presence tests.

The routing table decides whether a fired alarm reaches you or gets spoken into
an empty room, and presence decides which column of that table is used. Both
are pure functions precisely so this file can pin every cell of them.
"""
import json
import time

import pytest

import memory
import notify
import presence

URGENCIES = ("ambient", "normal", "urgent", "critical")
DESK_STATES = ("present", "away", "asleep", "unknown")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.init()
    return tmp_path


@pytest.fixture
def legs(monkeypatch):
    """Record what each surface was asked to say, without touching hardware."""
    seen = {"desk": [], "phone": [], "board": []}
    monkeypatch.setattr(notify, "_legs", {
        "desk": lambda t, u: seen["desk"].append(t),
        "phone": lambda t, u: seen["phone"].append(t),
        "board": lambda t, u: seen["board"].append(t),
    })
    return seen


# ── the routing table, cell by cell ────────────────────────────────────────
def test_every_cell_returns_known_surfaces():
    valid = {"desk", "phone", "board", "hold"}
    for u in URGENCIES:
        for d in DESK_STATES:
            got = notify.surfaces_for(u, d)
            assert got, f"{u}/{d} routes nowhere"
            assert set(got) <= valid, f"{u}/{d} -> {got}"


def test_critical_always_reaches_desk_and_phone():
    """A redundant alarm is a far smaller failure than a missed one, so this
    holds even when we believe the user is asleep or we have no idea."""
    for d in DESK_STATES:
        got = notify.surfaces_for("critical", d)
        assert "desk" in got and "phone" in got, f"critical/{d} -> {got}"


def test_anything_worth_interrupting_you_with_reaches_the_phone(db):
    """Everything above ambient reaches the phone in every desk state. You walk
    away from the desk thirty seconds after setting a reminder, and the routing
    decision was made when you were still sitting there."""
    for urgency in ("normal", "urgent", "critical"):
        for desk in DESK_STATES:
            got = notify.surfaces_for(urgency, desk)
            if "hold" in got:
                continue            # asleep+normal is deliberately deferred
            assert "phone" in got, f"{urgency}/{desk} -> {got}"


def test_ambient_never_speaks_anywhere():
    for d in DESK_STATES:
        got = notify.surfaces_for("ambient", d)
        assert "desk" not in got and "phone" not in got, f"ambient/{d} -> {got}"


def test_normal_goes_to_the_phone_when_you_are_not_at_the_desk():
    assert "desk" in notify.surfaces_for("normal", "present")
    assert "phone" in notify.surfaces_for("normal", "away")


def test_normal_is_held_rather_than_spoken_into_a_dark_room():
    got = notify.surfaces_for("normal", "asleep")
    assert "hold" in got and "desk" not in got


def test_unknown_presence_never_silently_holds():
    """Unknown means cortana is down or the file is stale - it is NOT evidence
    the user is asleep, so nothing may be quietly deferred on that basis."""
    for u in URGENCIES:
        assert "hold" not in notify.surfaces_for(u, "unknown")


def test_an_unrecognised_urgency_falls_back_to_normal():
    assert notify.surfaces_for("SCREAMING", "present") == \
           notify.surfaces_for("normal", "present")


# ── deliver() ──────────────────────────────────────────────────────────────
def test_deliver_reaches_the_expected_legs(db, legs):
    """A present desk now reaches the phone too, and this test used to pin the
    opposite.

    That table entry is why push notifications looked completely broken: you
    set a reminder while sitting at your machine, the desk reads "present", and
    the phone was never selected - no leg lookup, not even an audit row naming
    it. The phone worked all along; nothing ever asked it.
    """
    notify.deliver("timer done", "urgent", desk_state="present")
    assert legs["desk"] == ["timer done"]
    assert legs["phone"] == ["timer done"]
    # ambient stays board-only: that is where genuinely quiet things belong.
    notify.deliver("indexed 40 files", "ambient", desk_state="present")
    assert legs["phone"] == ["timer done"]


def test_a_missing_leg_is_audited_not_faked(db, monkeypatch):
    """The cortana process cannot reach the phone - the bridge owns that socket.
    That must show up as a recorded miss, not as a silent success."""
    monkeypatch.setattr(notify, "_legs", {})
    reached = notify.deliver("hello", "critical", desk_state="away")
    assert "phone:unavailable" in reached
    assert "desk:unavailable" in reached

    con = memory.connect()
    rows = con.execute("SELECT surfaces, text FROM deliveries").fetchall()
    con.close()
    assert rows and "unavailable" in rows[0][0]


def test_a_throwing_leg_does_not_stop_the_others(db, monkeypatch):
    seen = []
    monkeypatch.setattr(notify, "_legs", {
        "desk": lambda t, u: (_ for _ in ()).throw(RuntimeError("no speaker")),
        "board": lambda t, u: seen.append(t)})
    reached = notify.deliver("x", "normal", desk_state="present")
    assert "desk:failed" in reached
    assert seen == ["x"], "a broken speaker silenced the dashboard too"


def test_empty_text_is_dropped(db, legs):
    assert notify.deliver("   ", "critical", desk_state="present") == []


# ── held items ─────────────────────────────────────────────────────────────
def test_held_items_are_spoken_when_you_come_back(db, legs):
    notify.deliver("parcel arrived", "normal", desk_state="asleep")
    assert legs["desk"] == [], "spoke into a dark room"
    assert notify.held_count() == 1

    assert notify.release_held() == ["parcel arrived"]
    assert legs["desk"] == ["parcel arrived"]
    assert notify.held_count() == 0, "held queue not cleared after release"


def test_stale_held_items_are_dropped_not_read_back(db, legs):
    notify.deliver("old news", "normal", desk_state="asleep")
    later = time.time() + notify._HELD_MAX_AGE + 60
    assert notify.release_held(now=later) == []
    assert legs["desk"] == []


# ── presence classification ────────────────────────────────────────────────
def test_screen_off_means_asleep():
    assert presence.classify(idle_sec=0, screen=False, last_voice=time.time()) == "asleep"


def test_recent_input_means_present():
    assert presence.classify(idle_sec=5, screen=True, last_voice=0) == "present"


def test_long_idle_means_asleep():
    assert presence.classify(idle_sec=99999, screen=True, last_voice=0) == "asleep"


def test_without_an_idle_probe_asleep_is_unreachable():
    """xprintidle is confirmed missing on the runtime box, so this degraded path
    is the live one. Guessing 'asleep' would silence an alarm, which is the
    worst error this component can make - it must be structurally impossible."""
    now = time.time()
    for last_voice in (0, now, now - 100000):
        got = presence.classify(idle_sec=None, screen=None, last_voice=last_voice)
        assert got in ("present", "away")


def test_recent_speech_counts_as_present_when_idle_is_unmeasurable():
    assert presence.classify(None, None, time.time()) == "present"
    assert presence.classify(None, None, time.time() - 5000) == "away"


# ── staleness ──────────────────────────────────────────────────────────────
def test_a_stale_presence_file_reads_unknown_not_its_last_value(tmp_path, monkeypatch):
    """Serving a six-hour-old 'present' would aim an alarm at a speaker nobody
    is near. Same rule calendar_state already applies to yesterday's agenda."""
    f = tmp_path / "presence_desk.json"
    f.write_text(json.dumps({"state": "present", "ts": time.time() - 99999}))
    monkeypatch.setattr(presence, "STATE_FILE", f)
    assert presence.read_desk() == "unknown"


def test_a_fresh_presence_file_is_trusted(tmp_path, monkeypatch):
    f = tmp_path / "presence_desk.json"
    f.write_text(json.dumps({"state": "away", "ts": time.time()}))
    monkeypatch.setattr(presence, "STATE_FILE", f)
    assert presence.read_desk() == "away"


def test_a_missing_presence_file_reads_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, "STATE_FILE", tmp_path / "nope.json")
    assert presence.read_desk() == "unknown"
