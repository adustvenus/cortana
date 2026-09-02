"""Bridge phase-2 tests: the phone command channel, reminder replay, the
presence merge, the comms store, and the confirmed SMS send.

Everything here is pure Python plus sqlite and asyncio, both stdlib. aiohttp is
NOT imported: CI installs pytest, python-dotenv and python-dateutil and nothing
else, and the logic worth testing was deliberately kept out of the aiohttp
handlers so this file can run anywhere.

Each test is named after the failure it prevents, because every one of them is a
failure that cannot be reproduced on the Windows dev box where this was written:
the bridge only ever runs on the Linux runtime box.
"""
import asyncio
import json
import time

import pytest

import memory
from bridge import cmdchan, comms, hub, inbox, presence_link, scheduler

# The comms table lives in memory.init(), which this agent does not own - it is
# handed over as a DDL snippet. Repeating it here is what lets these tests run
# before that snippet is applied, and it is also a check on the snippet itself:
# if the two ever disagree, the tests are testing a table that does not exist in
# production, which is worse than no test at all.
COMMS_DDL = """
CREATE TABLE IF NOT EXISTS comms(
  id INTEGER PRIMARY KEY,
  ts REAL, kind TEXT, app TEXT, sender TEXT, title TEXT, body TEXT,
  ext_id TEXT, unread INT);
CREATE INDEX IF NOT EXISTS comms_ts ON comms(ts);
CREATE UNIQUE INDEX IF NOT EXISTS comms_ext ON comms(kind, ext_id);
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.init()
    con = memory.connect()
    con.executescript(COMMS_DDL)
    con.commit()
    con.close()
    return tmp_path


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Recorder:
    """A phone that receives frames and answers nothing on its own."""

    def __init__(self):
        self.sent = []

    async def send_str(self, msg):
        self.sent.append(msg)


class DeadSocket:
    """A phone that dropped between two heartbeats - the normal failure."""

    async def send_str(self, msg):
        raise ConnectionResetError("peer went away")


@pytest.fixture
def sockets():
    hub._sockets.clear()
    yield hub._sockets
    hub._sockets.clear()


@pytest.fixture(autouse=True)
def quiet_seq(monkeypatch):
    """Announce ids persist to announce_seq.json in the checkout root. Nothing
    here may write it - test_hub_seq.py covers that path deliberately, and a
    test suite that scribbles in the repo is a test suite you stop trusting."""
    monkeypatch.setattr(hub, "_persist_seq", lambda *_a: None)


@pytest.fixture(autouse=True)
def clean_pending():
    cmdchan._pending.clear()
    yield
    cmdchan._pending.clear()


# -- the command channel ---------------------------------------------------
def test_no_phone_connected_refuses_immediately(sockets):
    """A refusal the user can act on, not a 20s wait that says nothing. The
    difference matters: 'no phone connected' is fixable, a timeout is not."""
    started = time.monotonic()
    reply = run(cmdchan.request("sms.send", {"to": "x"}))
    assert "no phone" in reply["error"]
    assert time.monotonic() - started < 1


def test_a_dropped_socket_fails_fast_instead_of_timing_out(sockets):
    """hub.send swallows per-socket failures and drops the socket rather than
    raising, so without the follow-up check a phone we already know is gone
    would still cost the full timeout."""
    hub.add(DeadSocket(), "dead")
    reply = run(cmdchan.request("sms.send", {"to": "x"}, timeout=5))
    assert "dropped" in reply["error"]
    assert cmdchan.pending_count() == 0


def test_timeout_answers_in_a_sentence_and_leaves_nothing_pending(sockets):
    """The old-app case: LinkClient's frame `when` has no else branch, so an
    unknown type is silently dropped and the only symptom is this timeout. The
    pending map must not keep the entry - this unit runs for weeks."""
    ws = Recorder()
    hub.add(ws, "phone")
    reply = run(cmdchan.request("sms.send", {"to": "x"}, timeout=0.05))
    assert "didn't answer" in reply["error"]
    assert cmdchan.pending_count() == 0
    frame = json.loads(ws.sent[0])
    assert frame["type"] == "cmd" and frame["cmd"] == "sms.send" and frame["id"]


def test_pending_map_is_bounded(sockets):
    """An unbounded request map on a Restart=always service is how a leak turns
    into a mystery. Past the ceiling, new requests are refused, not queued."""
    hub.add(Recorder(), "phone")

    async def scenario():
        loop = asyncio.get_running_loop()
        for i in range(cmdchan.MAX_PENDING):
            cmdchan._pending[f"held{i}"] = {"fut": loop.create_future(),
                                            "cmd": "noop", "ts": time.time(),
                                            "timeout": 60.0}
        reply = await cmdchan.request("sms.send", {"to": "x"}, timeout=0.05)
        # the held entries are still there; the refusal added nothing
        assert cmdchan.pending_count() == cmdchan.MAX_PENDING
        return reply

    assert "too many" in run(scenario())["error"]


def test_sweep_drops_entries_that_outlived_their_timeout(sockets):
    """Belt and braces for the case request()'s finally: never runs, which is
    not a hypothesis anyone can disprove on a box they cannot reach."""
    async def scenario():
        loop = asyncio.get_running_loop()
        cmdchan._pending["stale"] = {"fut": loop.create_future(), "cmd": "noop",
                                     "ts": time.time() - 1000, "timeout": 20.0}
        cmdchan._sweep()
        return cmdchan.pending_count()

    assert run(scenario()) == 0


def test_a_reply_resolves_the_waiting_request(sockets):
    ws = Recorder()
    hub.add(ws, "phone")

    async def scenario():
        async def answer():
            for _ in range(200):
                if ws.sent:
                    break
                await asyncio.sleep(0.005)
            rid = json.loads(ws.sent[0])["id"]
            assert cmdchan.resolve(rid, {"ok": True, "result": {"sent": 1}})

        task = asyncio.ensure_future(answer())
        reply = await cmdchan.request("sms.send", {"to": "x"}, timeout=3)
        await task
        return reply

    assert run(scenario())["ok"] is True
    assert cmdchan.pending_count() == 0


def test_a_late_or_duplicate_reply_is_dropped(sockets):
    """After a bridge restart the phone's reply arrives at a process that never
    heard of the id. It must be told no, so it stops - for an SMS a retry is a
    second text message."""
    assert cmdchan.resolve("never-issued", {"ok": True}) is False


# -- reminder replay -------------------------------------------------------
def _schedule_row(con, title, fired_ts, state="delivered", urgency="normal",
                  rrule="", next_ts=None, delivered=True):
    """One schedules row plus, by default, the `deliveries` row a real fire
    writes. Both are required: the replay reads state for what the row IS and
    deliveries for what was actually routed, because state alone cannot tell a
    delivered occurrence from one roll_forward() deliberately suppressed."""
    cur = con.execute(
        "INSERT INTO schedules(created, kind, title, action, payload, rrule, tz,"
        " next_ts, state, urgency, catchup, require_ack, nag_after, nag_count,"
        " nag_max, fired_ts, ack_ts, owner, last_error)"
        " VALUES(?,?,?,'say','{}',?,'',?,?,?,0,0,600,0,0,?,NULL,'','')",
        (fired_ts, "reminder", title, rrule, next_ts, state, urgency, fired_ts))
    if delivered:
        con.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)",
                    (fired_ts, f"schedule:{cur.lastrowid}", cur.lastrowid,
                     urgency, "phone,board", "away", title))
    con.commit()
    return cur.lastrowid


def test_first_sight_of_a_device_replays_nothing(db):
    """An absent mark means we have no idea what this phone has heard. The safe
    reading is 'you are current', not 'here is your whole afternoon'."""
    con = memory.connect()
    _schedule_row(con, "take the pasta off", time.time() - 600)
    con.close()
    assert scheduler.replay_for("phone-hash") == []
    # ...and the mark is now set, so the NEXT gap is replayable
    assert memory.meta_get(scheduler.MARK_PREFIX + "phone-hash", "") != ""


def test_replay_survives_a_bridge_restart(db):
    """THE regression this exists for. hub._announces is an in-memory deque and
    the unit is Restart=always: a phone offline across a restart got nothing
    from the deque, so an alarm that fired meanwhile was simply gone."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(time.time() - 3600))
    con = memory.connect()
    _schedule_row(con, "your flight is in an hour", time.time() - 600, urgency="urgent")
    con.close()

    hub._announces.clear()              # the restart: history lost, ids kept
    items = scheduler.replay_for("phone-hash")
    assert len(items) == 1
    assert "your flight is in an hour" in items[0]["text"]
    assert items[0]["urgency"] == "urgent"
    assert items[0]["type"] == "announce" and items[0]["id"] > 0


def test_replay_is_exactly_once_per_device(db):
    """Reconnects are constant - screen off, tailnet blip, app backgrounded. A
    replay that repeats on each one is worse than the bug it fixes."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(time.time() - 3600))
    con = memory.connect()
    _schedule_row(con, "call the dentist", time.time() - 600)
    con.close()
    assert len(scheduler.replay_for("phone-hash")) == 1
    assert scheduler.replay_for("phone-hash") == []


def test_replay_skips_acked_ambient_and_ancient_items(db):
    """Three separate ways an item stops being news: the user dealt with it, it
    was never meant to speak, or it is older than anyone wants read back."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(0.0))
    now = time.time()
    con = memory.connect()
    _schedule_row(con, "acked one", now - 600, state="acked")
    _schedule_row(con, "ambient one", now - 600, urgency="ambient")
    _schedule_row(con, "ancient one", now - scheduler.REPLAY_WINDOW - 60)
    con.close()
    assert scheduler.replay_for("phone-hash") == []


def test_replay_leaves_the_last_minute_to_live_delivery(db):
    """An item that fired seconds ago is still in the deque, and the hello
    replay already sends that. Replaying it here too would double it."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(0.0))
    con = memory.connect()
    _schedule_row(con, "just fired", time.time() - 2)
    con.close()
    assert scheduler.replay_for("phone-hash") == []


def test_replay_is_capped_per_reconnect(db):
    """If the mark ever fails to persist, the blast radius is one short burst
    and not an unbounded monologue."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(0.0))
    now = time.time()
    con = memory.connect()
    for i in range(scheduler.REPLAY_MAX + 5):
        _schedule_row(con, f"item {i}", now - 300 - i)
    con.close()
    assert len(scheduler.replay_for("phone-hash")) == scheduler.REPLAY_MAX


def test_a_replay_is_not_handed_to_the_next_phone_that_reconnects(db):
    """_announces is shared. A per-device replay appended to it would be
    delivered to the NEXT phone asking for everything after its own id - the
    same duplicate this mechanism exists to prevent, by a different route."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-a", repr(time.time() - 3600))
    con = memory.connect()
    _schedule_row(con, "yours alone", time.time() - 600)
    con.close()
    hub._announces.clear()
    assert len(scheduler.replay_for("phone-a")) == 1
    assert hub.pending_after(0) == []


def test_a_repeating_alarm_is_replayed_too(db):
    """THE gap the state filter hid. advance() puts a recurring row straight
    back to 'pending' the moment it arms the next occurrence, so filtering on
    ('delivered','firing') excluded every daily alarm while passing every
    one-shot test - the reminders people actually rely on were the ones that
    silently never replayed."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(time.time() - 3600))
    con = memory.connect()
    _schedule_row(con, "your 7am alarm", time.time() - 600, state="pending",
                  urgency="critical", rrule="FREQ=DAILY",
                  next_ts=time.time() + 80000)
    con.close()
    items = scheduler.replay_for("phone-hash")
    assert len(items) == 1 and "your 7am alarm" in items[0]["text"]


def test_an_occurrence_that_was_suppressed_is_not_replayed_as_delivered(db):
    """claim() stamps fired_ts BEFORE roll_forward() decides an occurrence is
    too late to speak, so a 7am alarm deliberately skipped after an overnight
    sleep is indistinguishable from a delivered one by state alone. Only
    `deliveries` knows, and a phone told 'while you were away: your 7am alarm'
    about an alarm that was suppressed on purpose is a lie."""
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(time.time() - 3600))
    con = memory.connect()
    _schedule_row(con, "your 7am alarm", time.time() - 600, state="pending",
                  rrule="FREQ=DAILY", next_ts=time.time() + 80000,
                  delivered=False)
    con.close()
    assert scheduler.replay_for("phone-hash") == []


def test_an_item_the_deque_still_holds_is_not_replayed_twice(db, monkeypatch):
    """hub._announces has no age limit, only a 50-item cap, so the hello
    handler's pending_after() covers hours - and the schedules replay ran right
    behind it over the same window. One reconnect delivered the same reminder
    twice, worded differently. Only what fired BEFORE this process started is
    beyond the deque's reach."""
    now = time.time()
    monkeypatch.setattr(scheduler, "_STARTED", now - 1800)
    memory.meta_set(scheduler.MARK_PREFIX + "phone-hash", repr(now - 7200))
    con = memory.connect()
    _schedule_row(con, "fired while we were up", now - 600)   # after _STARTED
    _schedule_row(con, "fired before the restart", now - 3600)
    con.close()
    texts = [i["text"] for i in scheduler.replay_for("phone-hash")]
    assert len(texts) == 1 and "before the restart" in texts[0]


def test_announce_frames_carry_a_sanitised_urgency():
    """The phone picks a notification channel from this, and hub.announce is
    monkeypatched over speech.announce, whose second positional argument is
    max_hold - an int. An unaudited value would end up in the frame."""
    item = hub.record("a line", 30)
    assert item["urgency"] == "normal"
    assert hub.record("another", "critical")["urgency"] == "critical"


# -- the presence merge ----------------------------------------------------
def test_a_live_socket_no_longer_means_the_user_is_looking_at_the_phone():
    """This test used to assert the opposite, and its name said so.

    That was correct while LinkClient held the socket ONLY for a foregrounded
    Activity. v2.5.0 added a foreground service that holds it with the app
    closed, so the socket became permanently live and presence reported "open"
    forever - the user watched their phone sit closed while the board insisted
    it was open. An inference can be invalidated by a change somewhere else
    entirely; this is what that looks like.
    """
    import time
    now = time.time()
    view = presence_link.merge(desk="away", online=True, phone=None, now=now)
    assert view["phone"] != "open", "socket liveness must no longer imply open"

    # screenOn is what still carries the original meaning.
    looking = presence_link.merge(desk="away", online=True, now=now,
                                  phone={"ts": now, "screenOn": True})
    assert looking["phone"] == "open"

    pocket = presence_link.merge(desk="away", online=True, now=now,
                                 phone={"ts": now, "screenOn": False})
    assert pocket["phone"] == "recent", "reachable, but nobody is looking at it"


def test_the_phones_own_home_work_label_survives_the_wire():
    """`place` cannot carry it - its vocabulary is home/out/driving/unknown and
    the phone collapses "work" into "out" before sending. `zone` is the phone's
    saved-place label, and it was being dropped, which is why setting a work
    location appeared to do nothing at all."""
    import time
    now = time.time()
    view = presence_link.merge(desk="present", online=True, now=now,
                               phone={"ts": now, "zone": "work", "place": "out"})
    assert view["zone"] == "work"
    assert presence_link.merge(desk="present", online=True, now=now,
                               phone={"ts": now, "zone": "nonsense"})["zone"] == "unknown"


def test_no_report_and_no_socket_reads_as_closed_and_unknown():
    view = presence_link.merge(desk="present", online=False, phone=None)
    assert view["phone"] == "closed" and view["place"] == "unknown"
    assert view["desk"] == "present"


def test_a_recent_report_without_a_socket_is_recent_not_open():
    now = time.time()
    view = presence_link.merge(online=False, now=now,
                               phone={"place": "home", "ts": now - 60})
    assert view["phone"] == "recent" and view["place"] == "home"


def test_a_stale_report_degrades_place_to_unknown_never_its_last_value():
    """Same rule as presence.read_desk(): serving a six-hour-old 'home' would
    aim a delivery at a house nobody is in."""
    now = time.time()
    view = presence_link.merge(online=False, now=now,
                               phone={"place": "home",
                                      "ts": now - presence_link.PHONE_STALE - 1})
    assert view["place"] == "unknown" and view["phone"] == "closed"


def test_driving_beats_every_other_label():
    now = time.time()
    view = presence_link.merge(online=True, now=now,
                               phone={"place": "driving", "driving": True,
                                      "ts": now})
    assert view["place"] == "driving" and view["driving"] is True


def test_an_unrecognised_desk_or_place_falls_back_to_unknown():
    view = presence_link.merge(desk="dancing", online=False,
                               phone={"place": "atlantis", "ts": time.time()})
    assert view["desk"] == "unknown" and view["place"] == "unknown"


def test_coordinates_are_classified_and_not_kept(db, monkeypatch):
    """The phone sends a fix; what survives the call is a coarse place. Nothing
    in the stored record may let the fix be reconstructed."""
    monkeypatch.setattr(presence_link, "HOME_LAT", 47.6062)
    monkeypatch.setattr(presence_link, "HOME_LON", -122.3321)
    monkeypatch.setattr(presence_link, "HOME_RADIUS_M", 150.0)
    kept = presence_link.record({"lat": 47.6063, "lon": -122.3322,
                                 "charging": True})
    assert kept["place"] == "home"
    assert "lat" not in kept and "lon" not in kept
    stored = json.loads(memory.meta_get(presence_link.MARK_KEY, "{}"))
    assert "lat" not in stored and "lon" not in stored


def test_a_far_away_fix_reads_as_out(monkeypatch):
    monkeypatch.setattr(presence_link, "HOME_LAT", 47.6062)
    monkeypatch.setattr(presence_link, "HOME_LON", -122.3321)
    assert presence_link.coarse_place(47.7, -122.4, "", False) == "out"


def test_without_a_home_fix_coordinates_stay_unknown(monkeypatch):
    """Guessing home from an unconfigured origin would put the user at
    latitude zero, in the Gulf of Guinea, and route deliveries accordingly."""
    monkeypatch.setattr(presence_link, "HOME_LAT", 0.0)
    monkeypatch.setattr(presence_link, "HOME_LON", 0.0)
    assert presence_link.coarse_place(47.6, -122.3, "", False) == "unknown"
    # ...but a label the phone worked out itself is still honoured
    assert presence_link.coarse_place(47.6, -122.3, "home", False) == "home"


# -- the comms store -------------------------------------------------------
def _note(i, ts):
    return {"id": f"n{i}", "app": "Signal", "title": "Someone",
            "text": f"message {i}", "ts": ts}


def test_the_store_is_bounded_by_row_count(db, monkeypatch):
    """A phone generates notifications all day and can re-sync its whole
    backlog after a reinstall. The ceiling has to be ours, not its.

    The cap is lowered here rather than inserting thousands of rows: the point
    under test is that the prune runs and keeps the NEWEST, not the number 500.
    """
    monkeypatch.setattr(comms, "MAX_ROWS", 20)
    now = time.time()
    comms.ingest({"notifications": [_note(i, now - i) for i in range(30)]})
    comms.ingest({"notifications": [_note(i, now - i) for i in range(30, 60)]})
    con = memory.connect()
    total = con.execute("SELECT COUNT(*) FROM comms").fetchone()[0]
    con.close()
    assert total == 20
    # ts descends as i climbs, so the survivors are items 0..19
    assert comms.recent("note")[0]["body"] == "message 0"


def test_the_store_is_bounded_by_age(db):
    now = time.time()
    comms.ingest({"notifications": [_note(1, now - comms.MAX_AGE - 60),
                                    _note(2, now)]})
    bodies = [r["body"] for r in comms.recent("note")]
    assert bodies == ["message 2"]


def test_resyncing_the_same_backlog_stores_nothing_new(db):
    """Phones re-send. Without the dedup key every reconnect would double the
    list the assistant reads back."""
    now = time.time()
    batch = {"sms": [{"id": "s1", "from": "Mum", "body": "call me", "ts": now}]}
    first = comms.ingest(batch)
    second = comms.ingest(batch)
    assert first["stored"] == 1 and second["stored"] == 0
    assert len(comms.recent("sms")) == 1


def test_notifications_without_an_id_dedup_on_content_and_minute(db):
    """Android often has no stable id, and a progress notification re-posts
    dozens of times a minute."""
    now = time.time()
    item = {"app": "Chrome", "title": "Downloading", "text": "40%", "ts": now}
    comms.ingest({"notifications": [item, dict(item), dict(item)]})
    assert len(comms.recent("note")) == 1


def test_millisecond_timestamps_are_normalised(db):
    """Android hands out epoch millis. Stored raw, every message would sit
    52,000 years in the future and sort above everything forever."""
    now = time.time()
    comms.ingest({"sms": [{"id": "s1", "from": "Mum", "body": "hi",
                           "ts": now * 1000}]})
    assert abs(comms.recent("sms")[0]["ts"] - now) < 5


def test_bodies_are_capped(db):
    comms.ingest({"sms": [{"id": "s1", "from": "x", "body": "y" * 5000,
                           "ts": time.time()}]})
    assert len(comms.recent("sms")[0]["body"]) == comms.BODY_CAP


def test_a_missing_table_degrades_instead_of_raising(tmp_path, monkeypatch):
    """state.db mid-migration is a normal state during a rollout, and a
    dashboard poller must never see a 500 because of it."""
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "bare.db")
    memory.init()
    # memory.init() now ships the comms DDL, so the missing-table case has to be
    # made deliberately rather than assumed. Dropping it is closer to the real
    # scenario anyway: an older state.db that predates the table, opened by new
    # code mid-rollout.
    con = memory.connect()
    con.executescript("DROP INDEX IF EXISTS comms_ext;"
                      "DROP INDEX IF EXISTS comms_ts;"
                      "DROP TABLE IF EXISTS comms;")
    con.commit()
    con.close()
    assert comms.recent("sms") == []
    result = comms.ingest({"sms": [{"id": "s1", "body": "hi", "ts": time.time()}]})
    assert result["ok"] is False and "comms table" in result["error"]


def test_an_incoming_text_with_no_sender_is_not_attributed_to_the_user(db):
    """'me' is written by _store_outgoing and by nothing else. Defaulting an
    EMPTY sender to it read a withheld number or a short code back as something
    the user had sent - "You texted just now: your code is 447122"."""
    comms.ingest({"sms": [{"id": "x1", "body": "your code is 447122",
                           "ts": time.time()}]})
    assert comms.local_view()["sms"][0]["from"] == "unknown"


# -- the confirmed SMS send ------------------------------------------------
@pytest.fixture(autouse=True)
def clear_draft():
    comms.clear_staged()
    yield
    comms.clear_staged()


@pytest.fixture
def phone_says_ok(monkeypatch):
    """Stand in for the phone answering a cmd frame."""
    seen = []

    async def fake_request(cmd, args=None, timeout=None):
        seen.append((cmd, args))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(cmdchan, "request", fake_request)
    return seen


def test_an_unconfirmed_send_only_composes(db, phone_says_ok):
    """The standing rule, mirrored from Gmail: composed, read back, and sent
    only on an explicit yes. Nothing reaches the phone on this path."""
    result = run(comms.send("Mum", "on my way"))
    assert result["ok"] is False and result["staged"] is True
    assert "on my way" in result["readback"]
    assert phone_says_ok == []


def test_confirming_without_composing_first_is_refused(db, phone_says_ok):
    """A model that skips the read-back finds there is nothing to confirm - the
    guarantee is structural, not an instruction it can decide to ignore."""
    result = run(comms.send("Mum", "on my way", confirm=True))
    assert result["ok"] is False
    assert "Nothing is composed" in result["error"]
    assert phone_says_ok == []


def test_confirming_different_text_than_was_read_back_is_refused(db, phone_says_ok):
    """Compose one message, confirm another. The user agreed to the first."""
    run(comms.send("Mum", "on my way"))
    result = run(comms.send("Mum", "wire me two thousand dollars", confirm=True))
    assert result["ok"] is False and phone_says_ok == []
    assert "not the message I read back" in result["error"]


def test_a_stale_draft_will_not_send(db, phone_says_ok):
    """A yes half an hour later is not a yes to this."""
    comms.stage("Mum", "on my way", now=time.time() - comms.STAGE_TTL - 1)
    result = run(comms.send("Mum", "on my way", confirm=True))
    assert result["ok"] is False and phone_says_ok == []
    assert "too old" in result["error"]


def test_the_exact_composed_message_sends(db, phone_says_ok):
    run(comms.send("Mum", "on my way"))
    result = run(comms.send("Mum", "on my way", confirm=True))
    assert result["ok"] is True
    assert phone_says_ok == [("sms.send", {"to": "Mum", "body": "on my way"})]
    # and it shows up in the thread as ours
    assert comms.recent("sms")[0]["from"] == "me"


def test_a_sent_message_cannot_be_sent_twice_on_one_confirmation(db, phone_says_ok):
    """The draft is consumed. Otherwise a repeated tool call - which is exactly
    what a retrying model does - sends the message again."""
    run(comms.send("Mum", "on my way"))
    run(comms.send("Mum", "on my way", confirm=True))
    again = run(comms.send("Mum", "on my way", confirm=True))
    assert again["ok"] is False and len(phone_says_ok) == 1


def test_repeating_a_rejected_confirmation_still_will_not_send(db, phone_says_ok):
    """The refusal used to STAGE the text it had just rejected, so the same
    confirmed call made twice - which is exactly what a retrying model does -
    sent a message no human had ever heard read back: refuse, re-arm, retry,
    send. A mismatch has to drop the draft, or the guarantee is only an
    inconvenience."""
    run(comms.send("Mum", "on my way"))
    run(comms.send("Mum", "wire me two thousand dollars", confirm=True))
    again = run(comms.send("Mum", "wire me two thousand dollars", confirm=True))
    assert again["ok"] is False and phone_says_ok == []
    assert "Nothing is composed" in again["error"]


def test_a_phone_failure_is_never_retried(db, monkeypatch):
    """A timeout can mean the message went out and only the confirmation was
    lost. Sending again to be sure is how one message becomes two."""
    calls = []

    async def fake_request(cmd, args=None, timeout=None):
        calls.append(cmd)
        return {"error": "the phone didn't answer"}

    monkeypatch.setattr(cmdchan, "request", fake_request)
    run(comms.send("Mum", "on my way"))
    result = run(comms.send("Mum", "on my way", confirm=True))
    assert result["ok"] is False and len(calls) == 1
    assert "may have gone out anyway" in result["error"]


def test_an_empty_recipient_or_body_is_refused(db, phone_says_ok):
    assert run(comms.send("", "hello"))["ok"] is False
    assert run(comms.send("Mum", "   "))["ok"] is False
    assert phone_says_ok == []


# -- speech_inbox.json -----------------------------------------------------
@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "INBOX_FILE", tmp_path / "speech_inbox.json")
    inbox._state.update(seq=None, items=[])
    return tmp_path


def test_queued_lines_come_back_once_and_only_once(db, box):
    """The drain is cortana's half of the bridge's desk leg. Replaying an item
    on every poll would have her repeat herself every two seconds."""
    inbox.put("desk", "the pasta is done", "urgent")
    assert [i["text"] for i in inbox.drain()] == ["the pasta is done"]
    assert inbox.drain() == []


def test_the_seq_keeps_climbing_across_a_bridge_restart(db, box):
    """Cortana filters on a mark she persisted. A counter that restarted at 1
    would make every queued line look already-spoken - and the symptom would
    only appear while she was down, which is the case this exists for."""
    inbox.put("desk", "one")
    first = inbox.drain()[0]["seq"]
    inbox._state.update(seq=None, items=[])      # the restart
    inbox.put("desk", "two")
    assert inbox.drain()[0]["seq"] > first


def test_items_older_than_the_window_are_skipped_not_spoken(db, box):
    """Someone away for a day does not want yesterday read back at them."""
    inbox.put("desk", "ancient")
    raw = json.loads(inbox.INBOX_FILE.read_text())
    raw["items"][0]["ts"] = time.time() - inbox.MAX_AGE - 60
    inbox.INBOX_FILE.write_text(json.dumps(raw))
    assert inbox.drain() == []
    # skipped once, and the mark moved past it so it is never reconsidered
    inbox.put("desk", "fresh")
    assert [i["text"] for i in inbox.drain()] == ["fresh"]


def test_the_file_is_bounded_while_nobody_drains(db, box):
    """cortana is DESIGNED to be absent (shut down exits 42), so 'nobody is
    draining this' is normal, not a fault."""
    for i in range(inbox.MAX_ITEMS + 40):
        inbox.put("desk", f"line {i}")
    raw = json.loads(inbox.INBOX_FILE.read_text())
    assert len(raw["items"]) == inbox.MAX_ITEMS
    assert raw["items"][-1]["text"] == f"line {inbox.MAX_ITEMS + 39}"


def test_an_empty_line_is_never_queued(db, box):
    assert inbox.put("desk", "   ") is None
    assert inbox.put("desk", None) is None


def test_the_surface_and_urgency_survive_the_round_trip(db, box):
    """cortana decides between speech.alert and speech.announce from these, and
    a board item must not be spoken at all."""
    inbox.put("board", "checking the calendar", "ambient")
    inbox.put("desk", "the oven is on fire", "critical")
    got = {i["surface"]: i["urgency"] for i in inbox.drain()}
    assert got == {"board": "ambient", "desk": "critical"}


def test_a_missing_file_drains_to_nothing(db, box):
    assert inbox.drain() == []
