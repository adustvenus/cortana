"""Sentinel tests.

Every test here is named after a failure that has actually happened on this
project, or one whose whole nature is that nobody notices it:

  * a background loop that spawned a systemctl process 24 times a minute
    forever on a laptop, and had to be walked back;
  * a Google refresh token silently dying every seven days, discovered days
    later by squinting at an agenda that looked wrong;
  * two copies of the same /proc/meminfo parse drifting apart, so the tile and
    the alarm disagree and neither can be trusted;
  * a health check that throws and takes the whole health report down with it.

None of those raise anywhere a human is looking, which is exactly why they get
tests instead of a careful read.
"""
import json
import time
from pathlib import Path

import pytest

import sentinel

ROOT = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    """Every cache in this module is process-global by design (one writer, one
    loop), so each test starts from a blank one or leaks into the next."""
    # alerts() persists its ledger through memory.meta_set, so without this the
    # suite writes `sentinel_alerted` into the REAL state.db - and on the Linux
    # box, where that table exists, a test run would then suppress the next
    # genuine alert for a fault the user was never told about.
    import memory
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "autouse.db")
    sentinel._cache.clear()
    sentinel._alerted.clear()
    sentinel._alerted_loaded["done"] = True   # tests opt in to the db explicitly
    sentinel._probe_ts.clear()
    sentinel._probe_out.clear()
    monkeypatch.setattr(sentinel, "_last_written", None)
    monkeypatch.setattr(sentinel, "_last_write_ts", 0.0)
    yield
    sentinel._cache.clear()
    sentinel._alerted.clear()
    sentinel._probe_ts.clear()
    sentinel._probe_out.clear()


def _fake_checks(monkeypatch, *rows):
    """rows: (key, label, every, fn). Keeps a test off the real machine - no
    sockets, no subprocesses, no /proc."""
    monkeypatch.setattr(sentinel, "_CHECKS", tuple(rows))


# ── failure isolation ──────────────────────────────────────────────────────
def test_a_raising_check_degrades_only_its_own_row(monkeypatch):
    """One broken probe must cost one row. Losing the whole sentinel to a
    renamed thermal zone would be worse than never having written it, because
    the user would go on trusting a tile that had stopped measuring."""
    def boom(now=None):
        raise RuntimeError("sensor went away")

    _fake_checks(monkeypatch,
                 ("mem", "Memory", 60, lambda now=None: ("ok", "plenty")),
                 ("temp", "Temperature", 60, boom),
                 ("disk", "Disk", 60, lambda now=None: ("warn", "tight")))

    snap = sentinel.poll(now=1000.0)
    by_key = {c["key"]: c for c in snap["checks"]}
    assert len(snap["checks"]) == 3, "a throwing probe removed other rows"
    assert by_key["temp"]["state"] == "bad"
    assert "sensor went away" in by_key["temp"]["detail"]
    assert by_key["mem"]["state"] == "ok" and by_key["disk"]["state"] == "warn"


def test_a_check_returning_nonsense_is_unknown_not_trusted(monkeypatch):
    """An unrecognised state must never be ranked as healthy by accident."""
    _fake_checks(monkeypatch,
                 ("mem", "Memory", 60, lambda now=None: ("fine, probably", "?")))
    snap = sentinel.poll(now=1.0)
    assert snap["checks"][0]["state"] == "unknown"


def test_memory_is_the_first_row():
    """Order is the file order and memory leads it. RAM exhaustion is the most
    likely way this box actually kills Cortana, and Restart=always turns that
    into a respawn loop that `systemctl start` reports as success."""
    assert sentinel._CHECKS[0][0] == "mem"
    keys = [c[0] for c in sentinel._CHECKS]
    assert keys[:1] == ["mem"]
    for expected in ("disk", "temp", "units", "net", "git", "spend", "google", "apk"):
        assert expected in keys


# ── the /proc/meminfo parse, which must match the dashboard's ──────────────
MEMINFO = """MemTotal:        5023456 kB
MemFree:          210044 kB
MemAvailable:    1998872 kB
Buffers:           50000 kB
Cached:           900000 kB
SwapTotal:       2097148 kB
SwapFree:        2097148 kB
HugePages_Total:       0
HugePages_Free:        0
Hugepagesize:       2048 kB
DirectMap4k:      123456 kB
"""


def test_meminfo_parse_matches_a_real_file():
    mi = sentinel.parse_meminfo(MEMINFO)
    assert mi["MemAvailable"] == 1998872 * 1024
    assert mi["MemTotal"] == 5023456 * 1024
    assert mi["SwapFree"] == 2097148 * 1024
    # Lines with no "kB" unit are not memory sizes and the dashboard drops them.
    # Keeping them would make HugePages_Total look like 0 bytes of something.
    assert "HugePages_Total" not in mi
    assert "Hugepagesize" in mi


def test_meminfo_regex_is_literally_the_dashboards_regex():
    """The two parses must not drift. If they do, the MEMORY tile and the
    sentinel can report different truths about the same instant and there is no
    way to tell which one is lying - so pin the source, not just the behaviour."""
    js = ROOT / "Dashboard" / "app" / "main.js"
    if not js.exists():
        pytest.skip("dashboard main.js not present in this checkout")
    src = js.read_text(encoding="utf-8", errors="replace")
    assert sentinel._MEMINFO_RE.pattern in src, (
        "sentinel's /proc/meminfo regex no longer appears in readMeminfo()")


def test_a_short_of_ram_box_reads_bad_not_warn(monkeypatch):
    monkeypatch.setattr(sentinel, "MEM_BAD", 300 * sentinel.MB)
    monkeypatch.setattr(sentinel, "MEM_WARN", 600 * sentinel.MB)
    state, detail = sentinel.classify_mem(200 * sentinel.MB, 5 * sentinel.GB)
    assert state == "bad"
    assert "restart loop" in detail, "the detail must name the failure shape"
    assert sentinel.classify_mem(500 * sentinel.MB, 5 * sentinel.GB)[0] == "warn"
    assert sentinel.classify_mem(2 * sentinel.GB, 5 * sentinel.GB)[0] == "ok"


def test_an_unreadable_meminfo_is_unknown_never_ok():
    """A guard that cannot measure must not report health. That is the
    install-spotifyd lesson: the write probe passed while curl could not write
    at all, because the probe ran in a different context to the failure."""
    assert sentinel.classify_mem(None, None)[0] == "unknown"
    assert sentinel.classify_mem(0, 0)[0] == "unknown"


# ── the throttle, i.e. the process-storm regression ───────────────────────
def test_unit_checks_are_throttled_across_rapid_ticks(monkeypatch):
    """A prior bug in this repo spawned a systemctl process 24 times a minute
    forever. Rapid ticks - a fast loop, a forced poll, a tool call - must not be
    able to turn that back on, so the ceiling is asserted, not assumed."""
    calls = []

    monkeypatch.setattr(sentinel, "_have", lambda b: True)
    monkeypatch.setattr(sentinel, "_run",
                        lambda args, timeout=5: (calls.append(args) or
                                                 "active\nactive\nactive\nactive\n"))
    _fake_checks(monkeypatch,
                 ("units", "Services", sentinel.UNITS_EVERY, sentinel._check_units))

    t = 1_000_000.0
    for i in range(240):                      # four minutes of one-second ticks
        sentinel.poll(now=t + i, force=True)  # force: even this may not unthrottle
    assert len(calls) == 1, f"{len(calls)} systemctl invocations in four minutes"

    sentinel.poll(now=t + sentinel.UNITS_EVERY + 1, force=True)
    assert len(calls) == 2, "the throttle never released"
    # and it really is one process for all four units, not one process each
    assert calls[0][:3] == ["systemctl", "--user", "is-active"]
    assert len(calls[0]) == 3 + len(sentinel.UNITS)


def test_throttle_serves_the_cached_answer_rather_than_no_answer(monkeypatch):
    """A throttled probe must still report its last reading. Reporting nothing
    would flip the row to unknown every tick and make the file churn."""
    monkeypatch.setattr(sentinel, "_have", lambda b: True)
    monkeypatch.setattr(sentinel, "_run",
                        lambda args, timeout=5: "active\nactive\nactive\nactive\n")
    first = sentinel._check_units(1.0)
    second = sentinel._check_units(2.0)
    assert first == second == ("ok", f"All {len(sentinel.UNITS)} services are up.")


def test_a_missing_systemctl_is_unknown_not_an_outage(monkeypatch):
    monkeypatch.setattr(sentinel, "_have", lambda b: False)
    assert sentinel._check_units(1.0)[0] == "unknown"


def test_a_failed_unit_outranks_a_stopped_one():
    st = dict(cortana="active", **{"cortana-dash": "inactive",
                                   "cortana-bridge": "failed",
                                   "cortana-spotifyd": "active"})
    state, detail = sentinel.classify_units(st)
    assert state == "bad" and "cortana-bridge" in detail
    st["cortana-bridge"] = "active"
    state, detail = sentinel.classify_units(st)
    assert state == "warn" and "cortana-dash" in detail


def test_unparsable_systemctl_output_does_not_invent_an_outage(monkeypatch):
    """Reading something we do not understand must not become 'your services
    are down' - that is a guess dressed up as a measurement."""
    monkeypatch.setattr(sentinel, "_have", lambda b: True)
    monkeypatch.setattr(sentinel, "_run", lambda args, timeout=5: "what\n")
    assert sentinel._check_units(1.0)[0] == "unknown"


# ── google: the recurring seven-day death ─────────────────────────────────
def test_google_expiry_is_detected_from_the_calendar_error():
    """The documented failure: the consent screen sits in Testing, Google kills
    the refresh token after seven days, the agenda silently stops updating, and
    the only symptom is that it 'looks wrong' days later."""
    cal = {"events": [], "day": "",
           "error": "Google access expired - run: python main.py --google-auth",
           "ts": time.time()}
    state, detail = sentinel.classify_google(cal, token_exists=True, token_age=60)
    assert state == "bad"
    assert "--google-auth" in detail, "the fix command must be in the alert itself"


def test_google_expiry_is_detected_from_the_library_wording_too():
    """google_auth raises AuthExpired with wording of its own; the detector must
    not be pinned to one exact sentence written elsewhere."""
    for msg in ("invalid_grant: Token has been expired or revoked.",
                "no usable token", "Reconnect with the auth flow"):
        cal = {"error": msg, "ts": time.time()}
        assert sentinel.classify_google(cal, True, 60)[0] == "bad", msg


def test_a_token_older_than_a_week_warns_before_it_dies(monkeypatch):
    """token.json is rewritten on every successful refresh, so its mtime is the
    last moment Google actually said yes. Six days is a day of warning."""
    monkeypatch.setattr(sentinel, "GOOGLE_TOKEN_AGE", 6 * 86400)
    cal = {"error": "", "ts": time.time()}
    state, detail = sentinel.classify_google(cal, True, 6.5 * 86400)
    assert state == "warn"
    assert "--google-auth" in detail
    assert sentinel.classify_google(cal, True, 3600)[0] == "ok"


def test_a_missing_token_is_bad_and_says_so():
    state, detail = sentinel.classify_google({"ts": time.time()}, False, None)
    assert state == "bad" and "--google-auth" in detail


def test_an_ordinary_calendar_error_is_not_reported_as_expiry():
    state, detail = sentinel.classify_google({"error": "HTTP 503 backend error",
                                              "ts": time.time()}, True, 60)
    assert state == "warn"
    assert "--google-auth" not in detail, "sent the user to re-auth for an outage"


# ── the rest of the rows ──────────────────────────────────────────────────
def test_disk_thresholds():
    assert sentinel.classify_disk(1 * sentinel.GB, 200 * sentinel.GB)[0] == "bad"
    assert sentinel.classify_disk(4 * sentinel.GB, 200 * sentinel.GB)[0] == "warn"
    assert sentinel.classify_disk(90 * sentinel.GB, 200 * sentinel.GB)[0] == "ok"


def test_a_machine_with_no_thermal_zone_is_unknown_not_cool():
    assert sentinel.classify_temp(None)[0] == "unknown"
    assert sentinel.classify_temp(45)[0] == "ok"
    assert sentinel.classify_temp(85)[0] == "warn"
    assert sentinel.classify_temp(95)[0] == "bad"


def test_no_route_and_broken_dns_are_told_apart():
    """Two questions, two answers - the same discipline as `curl -o file` versus
    `curl > file`. 'The internet is down' and 'DNS is down' need different fixes."""
    assert sentinel.classify_net(False, False, "")[0] == "bad"
    assert "DNS" in sentinel.classify_net(True, False, "")[1]
    assert sentinel.classify_net(True, True, "")[0] == "ok"


def test_tailscale_down_warns_because_the_phone_rides_on_it():
    state, detail = sentinel.classify_net(True, True, "Stopped")
    assert state == "warn" and "bridge" in detail
    assert sentinel.classify_net(True, True, "Running")[0] == "ok"


def test_a_dirty_repo_warns_that_a_self_update_will_conflict():
    state, detail = sentinel.classify_git(dirty=3, behind=0)
    assert state == "warn" and "conflict" in detail
    assert sentinel.classify_git(0, 2)[0] == "warn"
    assert sentinel.classify_git(0, 0) == ("ok", "Repo clean and up to date.")
    assert sentinel.classify_git(0, 0, detached=True)[0] == "warn"


def test_spend_crosses_to_bad_only_at_the_budget():
    assert sentinel.classify_spend(10.0, 50.0)[0] == "ok"
    assert sentinel.classify_spend(41.0, 50.0)[0] == "warn"
    assert sentinel.classify_spend(51.0, 50.0)[0] == "bad"
    assert sentinel.classify_spend(1.0, 0)[0] == "unknown"


def test_apk_freshness(monkeypatch):
    monkeypatch.setattr(sentinel, "APK_STALE", 30 * 86400)
    now = 1_800_000_000.0
    fresh = {"version": "2.4.0", "_built_ts": now - 86400}
    stale = {"version": "2.4.0", "_built_ts": now - 60 * 86400}
    assert sentinel.classify_apk(fresh, now)[0] == "ok"
    assert sentinel.classify_apk(stale, now)[0] == "warn"
    assert sentinel.classify_apk(None, now)[0] == "unknown"


def test_apk_check_reads_the_real_version_file():
    """Cheap end-to-end on the one check whose input lives in this repo."""
    state, detail = sentinel._check_apk(time.time())
    assert state in ("ok", "warn", "unknown")
    assert detail


# ── worst(), snapshot(), and the file ─────────────────────────────────────
def test_worst_is_the_worst_row(monkeypatch):
    _fake_checks(monkeypatch,
                 ("mem", "Memory", 60, lambda now=None: ("ok", "fine")),
                 ("disk", "Disk", 60, lambda now=None: ("warn", "tight")),
                 ("net", "Network", 60, lambda now=None: ("bad", "offline")))
    assert sentinel.poll(now=1.0)["worst"] == "bad"


def test_unknown_never_raises_the_worst_and_never_lowers_it(monkeypatch):
    """Not knowing is not an incident, and it is also not proof of health."""
    _fake_checks(monkeypatch,
                 ("temp", "Temperature", 60, lambda now=None: ("unknown", "no zone")))
    assert sentinel.poll(now=1.0)["worst"] == "ok"

    sentinel._cache.clear()
    _fake_checks(monkeypatch,
                 ("temp", "Temperature", 60, lambda now=None: ("unknown", "no zone")),
                 ("mem", "Memory", 60, lambda now=None: ("warn", "tight")))
    assert sentinel.poll(now=1.0)["worst"] == "warn"


def test_snapshot_matches_the_agreed_contract(monkeypatch):
    _fake_checks(monkeypatch,
                 ("mem", "Memory", 60, lambda now=None: ("ok", "fine")))
    snap = sentinel.poll(now=1.0)
    assert {"worst", "checks"} <= set(snap)
    assert snap["worst"] in ("ok", "warn", "bad")
    assert set(snap["checks"][0]) == {"key", "label", "state", "detail"}
    assert snap["metrics"] == {}, "a check with no numbers must not invent any"


def test_numbers_are_published_as_numbers_not_words(monkeypatch):
    """routines.py compares a health metric numerically (disk_pct >= 90). A
    string '92' would compare as text and quietly rank below '9'."""
    _fake_checks(monkeypatch,
                 ("disk", "Disk", 60,
                  lambda now=None: ("warn", "tight", {"disk_pct": 92})),
                 ("net", "Network", 60, lambda now=None: ("ok", "online")))
    snap = sentinel.poll(now=1.0)
    assert snap["metrics"] == {"disk_pct": 92}
    assert isinstance(snap["metrics"]["disk_pct"], (int, float))


def test_a_check_that_returns_a_junk_metric_is_ignored_not_published(monkeypatch):
    _fake_checks(monkeypatch,
                 ("disk", "Disk", 60, lambda now=None: ("ok", "fine", "92%")))
    assert sentinel.poll(now=1.0)["metrics"] == {}


def test_each_check_keeps_its_own_cadence(monkeypatch):
    """A 900-second git probe must not be re-run by a 60-second loop, or the
    cadences are decoration."""
    runs = {"fast": 0, "slow": 0}

    def fast(now=None):
        runs["fast"] += 1
        return "ok", "fast"

    def slow(now=None):
        runs["slow"] += 1
        return "ok", "slow"

    _fake_checks(monkeypatch, ("mem", "Memory", 60, fast), ("git", "Repo", 900, slow))
    for i in range(10):
        sentinel.poll(now=1000.0 + i * 60)
    assert runs["fast"] == 10
    assert runs["slow"] == 1


def test_an_unchanged_payload_is_not_rewritten(tmp_path, monkeypatch):
    """Same discipline as hud_state.py, and it matters more here: this runs on a
    slow loop forever, and a file that keeps changing mtime is a file every
    reader keeps re-parsing."""
    f = tmp_path / "sentinel_state.json"
    monkeypatch.setattr(sentinel, "STATE_FILE", f)
    _fake_checks(monkeypatch,
                 ("mem", "Memory", 1, lambda now=None: ("ok", "fine")))

    sentinel.publish(now=1000.0)
    first = f.stat().st_mtime_ns
    for i in range(5):
        sentinel.publish(now=1001.0 + i, force=True)
    assert f.stat().st_mtime_ns == first, "rewrote an identical payload"

    _fake_checks(monkeypatch,
                 ("mem", "Memory", 1, lambda now=None: ("bad", "out of RAM")))
    sentinel.publish(now=2000.0, force=True)
    written = json.loads(f.read_text())
    assert written["worst"] == "bad" and written["ts"] == 2000.0


def test_a_missing_state_file_reads_unknown_and_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(sentinel, "STATE_FILE", tmp_path / "nope.json")
    d = sentinel.read_state()
    assert d["worst"] == "unknown" and d["stale"] is True and d["checks"] == []


def test_a_stale_file_is_flagged_but_not_turned_into_an_outage(tmp_path, monkeypatch):
    """Cortana being shut down deliberately is not an incident. The reader greys
    the tile out; this module refuses to invent a failure from an absence."""
    f = tmp_path / "sentinel_state.json"
    f.write_text(json.dumps({"worst": "ok", "checks": [], "ts": time.time() - 99999}))
    monkeypatch.setattr(sentinel, "STATE_FILE", f)
    d = sentinel.read_state()
    assert d["stale"] is True and d["worst"] == "ok"


# ── alerting ──────────────────────────────────────────────────────────────
def test_only_degradations_speak(monkeypatch):
    """The point of the module: an alert habit, not a poll habit. But a row that
    is still bad an hour later is the same news, and saying it every minute is
    how a user learns to ignore the voice."""
    state = {"v": ("ok", "fine")}
    _fake_checks(monkeypatch, ("mem", "Memory", 1, lambda now=None: state["v"]))

    sentinel.poll(now=100.0, force=True)
    assert sentinel.alerts(now=100.0) == []

    state["v"] = ("bad", "out of RAM")
    sentinel.poll(now=200.0, force=True)
    fired = sentinel.alerts(now=200.0)
    assert [f[0] for f in fired] == ["mem"]
    assert fired[0][1] == "urgent"
    assert "out of RAM" in fired[0][2]

    sentinel.poll(now=260.0, force=True)
    assert sentinel.alerts(now=260.0) == [], "repeated the same bad news"


def test_a_bad_row_is_repeated_once_the_realert_window_passes(monkeypatch):
    monkeypatch.setattr(sentinel, "REALERT", 3600.0)
    _fake_checks(monkeypatch, ("disk", "Disk", 1, lambda now=None: ("bad", "full")))
    sentinel.poll(now=0.0, force=True)
    assert len(sentinel.alerts(now=0.0)) == 1
    assert sentinel.alerts(now=1800.0) == []
    assert len(sentinel.alerts(now=3700.0)) == 1


def test_recovery_is_ambient_so_it_never_speaks(monkeypatch):
    """Ambient routes to the board only - good news is not worth interrupting
    for, but it is worth seeing."""
    state = {"v": ("bad", "full")}
    _fake_checks(monkeypatch, ("disk", "Disk", 1, lambda now=None: state["v"]))
    sentinel.poll(now=0.0, force=True)
    sentinel.alerts(now=0.0)

    state["v"] = ("ok", "plenty")
    sentinel.poll(now=100.0, force=True)
    fired = sentinel.alerts(now=100.0)
    assert len(fired) == 1 and fired[0][1] == "ambient"


def test_an_unknown_row_never_raises_an_alarm(monkeypatch):
    """A probe that vanished (no thermal zone after a kernel update) must not
    wake anybody up."""
    _fake_checks(monkeypatch,
                 ("temp", "Temperature", 1, lambda now=None: ("unknown", "no zone")))
    sentinel.poll(now=0.0, force=True)
    assert sentinel.alerts(now=0.0) == []


# ── the spoken form ───────────────────────────────────────────────────────
def test_spoken_summary_is_prose(monkeypatch):
    """Anything read aloud is prose: no markdown, no bullets, no URLs."""
    _fake_checks(monkeypatch,
                 ("mem", "Memory", 60, lambda now=None: ("bad", "Only 0.2 gigabytes free.")),
                 ("disk", "Disk", 60, lambda now=None: ("ok", "90 gigabytes free.")))
    sentinel.poll(now=1.0)
    said = sentinel.speakable()
    assert "Only 0.2 gigabytes free." in said
    assert not any(ch in said for ch in "*#`\n")
    assert "http" not in said


def test_spoken_details_are_separated_into_sentences(monkeypatch):
    """Two details run together are one unparseable sentence out of the TTS."""
    _fake_checks(monkeypatch,
                 ("mem", "Memory", 60, lambda now=None: ("bad", "out of RAM")),
                 ("net", "Network", 60, lambda now=None: ("bad", "offline")))
    sentinel.poll(now=1.0)
    assert sentinel.speakable() == "out of RAM. offline."


def test_an_unreadable_usage_table_is_not_read_aloud_as_sqlite(monkeypatch):
    """The generic handler puts the exception text in a string that gets SPOKEN.
    'no such table: usage' is not a sentence anybody wants to hear, and a db
    that cannot be read is not evidence of overspending."""
    import memory
    monkeypatch.setattr(memory, "month_spend",
                        lambda: (_ for _ in ()).throw(RuntimeError("no such table: usage")))
    state, detail = sentinel._check_spend(1.0)
    assert state == "unknown"
    assert "sqlite" not in detail and "table" not in detail


def test_spoken_summary_when_everything_is_fine(monkeypatch):
    _fake_checks(monkeypatch, ("mem", "Memory", 60, lambda now=None: ("ok", "fine")))
    sentinel.poll(now=1.0)
    assert sentinel.speakable() == "Everything checks out."


def test_spoken_summary_before_the_first_poll():
    assert "not run" in sentinel.speakable()


# ── the loop must not reach the network or spawn anything by accident ─────
def test_polling_does_not_shell_out_when_no_binary_exists(monkeypatch):
    """On a box missing systemctl, git and tailscale - which is the Windows dev
    box, and could be a stripped runtime box - nothing may be spawned at all."""
    spawned = []
    monkeypatch.setattr(sentinel, "_have", lambda b: False)
    monkeypatch.setattr(sentinel, "_run",
                        lambda args, timeout=5: (spawned.append(args) or ""))
    monkeypatch.setattr(sentinel, "_tcp_ok", lambda h, p, timeout=3: True)
    monkeypatch.setattr(sentinel, "_dns_ok", lambda name="x": True)
    sentinel.poll(now=1.0, force=True)
    assert spawned == [], f"spawned {spawned} with no binaries present"


# ── the alert map must survive a restart ──────────────────────────────────
@pytest.fixture
def db(tmp_path, monkeypatch):
    import memory
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.init()
    return tmp_path


def test_a_restart_does_not_re_speak_what_was_already_said(db, monkeypatch):
    """cortana.service is Restart=always. An in-process alert map plus a crash
    loop is the same urgent line spoken every five seconds forever, which is a
    worse failure than the one being reported."""
    _fake_checks(monkeypatch, ("mem", "Memory", 1, lambda now=None: ("bad", "out of RAM")))
    sentinel._alerted_loaded["done"] = False
    sentinel.poll(now=100.0, force=True)
    assert len(sentinel.alerts(now=100.0)) == 1

    # the process dies and comes back: caches empty, sqlite intact
    sentinel._alerted.clear()
    sentinel._alerted_loaded["done"] = False
    sentinel.poll(now=140.0, force=True)
    assert sentinel.alerts(now=140.0) == [], "re-spoke the same alert after a restart"


def test_alerting_still_works_with_no_database(monkeypatch):
    """A missing or unreadable state.db must degrade the memory of what was
    said, never the ability to say it."""
    import memory
    monkeypatch.setattr(memory, "meta_get",
                        lambda k, default="": (_ for _ in ()).throw(RuntimeError("no db")))
    monkeypatch.setattr(memory, "meta_set",
                        lambda k, v: (_ for _ in ()).throw(RuntimeError("no db")))
    _fake_checks(monkeypatch, ("mem", "Memory", 1, lambda now=None: ("bad", "out of RAM")))
    sentinel._alerted_loaded["done"] = False
    sentinel.poll(now=1.0, force=True)
    assert len(sentinel.alerts(now=1.0)) == 1


def test_a_probe_that_stops_measuring_is_not_announced_as_a_recovery(monkeypatch):
    """`unknown` ranks with `ok` so that it can never raise an alarm - which
    also made it look, to the diff, like the fault had cleared. A bad row whose
    probe simply went away announced "Memory is back to normal." while the box
    was still dying. That is the install-spotifyd mistake with a voice attached:
    the probe changed context and the change of context got reported as a
    measurement."""
    v = {"s": ("bad", "Only 0.1 gigabytes free.")}
    _fake_checks(monkeypatch, ("mem", "Memory", 1, lambda now=None: v["s"]))

    sentinel.poll(now=0.0, force=True)
    assert len(sentinel.alerts(now=0.0)) == 1

    v["s"] = ("unknown", "No /proc/meminfo on this machine.")
    sentinel.poll(now=10.0, force=True)
    assert sentinel.alerts(now=10.0) == [], "claimed a recovery it never measured"

    # and because the ledger forgot the old state rather than banking a
    # recovery, the fault alerts again the moment it can be measured again
    v["s"] = ("bad", "Only 0.1 gigabytes free.")
    sentinel.poll(now=20.0, force=True)
    assert len(sentinel.alerts(now=20.0)) == 1, "went quiet about a live fault"


def test_a_real_recovery_still_speaks_ambiently(monkeypatch):
    """The guard above must not cost the genuine ok transition."""
    v = {"s": ("bad", "full")}
    _fake_checks(monkeypatch, ("disk", "Disk", 1, lambda now=None: v["s"]))
    sentinel.poll(now=0.0, force=True)
    sentinel.alerts(now=0.0)
    v["s"] = ("ok", "plenty")
    sentinel.poll(now=10.0, force=True)
    assert [f[1] for f in sentinel.alerts(now=10.0)] == ["ambient"]


def test_zero_available_memory_is_the_worst_reading_not_a_missing_one():
    """`if not avail` folded "MemAvailable: 0 kB" - the single most severe
    reading this row can take - into "no /proc/meminfo on this machine", which
    ranks with ok. The leading check said nothing at the one moment it mattered
    most."""
    mi = sentinel.parse_meminfo("MemTotal:        5023456 kB\n"
                                "MemAvailable:          0 kB\n")
    state, detail = sentinel.classify_mem(mi.get("MemAvailable"), mi.get("MemTotal"))
    assert state == "bad", "zero bytes free reported as unmeasurable"
    assert "restart loop" in detail
    # genuinely absent is still unknown, which is the other half of the rule
    assert sentinel.classify_mem(None, 5 * sentinel.GB)[0] == "unknown"


def test_a_failed_write_is_not_remembered_as_written(tmp_path, monkeypatch):
    """publish() banked the payload before os.replace ran, so ONE failed write
    froze sentinel_state.json forever: every later publish saw an unchanged
    payload and skipped. The write fails on a full disk - the exact condition
    this module exists to report - so the disk row silenced itself, and nothing
    anywhere said so."""
    f = tmp_path / "sentinel_state.json"
    monkeypatch.setattr(sentinel, "STATE_FILE", f)
    _fake_checks(monkeypatch, ("disk", "Disk", 1, lambda now=None: ("bad", "full")))

    def no_space(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sentinel.os, "replace", no_space)
    with pytest.raises(OSError):
        sentinel.publish(now=1000.0)

    monkeypatch.undo()                      # the disk gets some room back
    monkeypatch.setattr(sentinel, "STATE_FILE", f)
    _fake_checks(monkeypatch, ("disk", "Disk", 1, lambda now=None: ("bad", "full")))
    sentinel.publish(now=1001.0, force=True)
    assert f.exists(), "the identical payload was skipped as already written"
    assert json.loads(f.read_text())["worst"] == "bad"


def test_a_failed_write_does_not_leak_a_temp_file(tmp_path, monkeypatch):
    """The tmp file is created in the state file's own directory - the repo
    directory. Leaking one per attempt puts the retry loop to work filling the
    disk that could not be written to in the first place."""
    f = tmp_path / "sentinel_state.json"
    monkeypatch.setattr(sentinel, "STATE_FILE", f)
    _fake_checks(monkeypatch, ("disk", "Disk", 1,
                               lambda now=None: ("bad", "full", {"n": 1})))
    monkeypatch.setattr(sentinel.os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(OSError(28, "full")))
    for i in range(5):
        with pytest.raises(OSError):
            sentinel.publish(now=1000.0 + i, force=True)
    assert list(tmp_path.iterdir()) == [], f"leaked {list(tmp_path.iterdir())}"


def test_the_state_file_heartbeats_so_a_steady_box_is_not_read_as_dead(tmp_path,
                                                                       monkeypatch):
    """Skip-unchanged also freezes `ts`, and two readers treat `ts` as proof of
    life: routines.py drops EVERY health routine past 900s and bridge/watch.py
    greys the tile at 300s. A box whose payload simply held steady would have
    silently disabled its own health alerting."""
    f = tmp_path / "sentinel_state.json"
    monkeypatch.setattr(sentinel, "STATE_FILE", f)
    monkeypatch.setattr(sentinel, "HEARTBEAT", 120.0)
    _fake_checks(monkeypatch, ("mem", "Memory", 1, lambda now=None: ("ok", "fine")))

    sentinel.publish(now=1000.0)
    assert json.loads(f.read_text())["ts"] == 1000.0
    sentinel.publish(now=1060.0, force=True)
    assert json.loads(f.read_text())["ts"] == 1000.0, "rewrote inside the heartbeat"
    sentinel.publish(now=1200.0, force=True)
    assert json.loads(f.read_text())["ts"] == 1200.0, "let `ts` age past the beat"
    assert sentinel.HEARTBEAT < 300, "the beat must clear the bridge's stale window"


def test_a_raising_probe_still_burns_its_throttle_window():
    """The throttle asked "is there a cached output?", and a probe that raised
    never left one - so it re-ran on every tick. A probe that fails FAST is the
    process storm this function exists to prevent, and it only misbehaved while
    something was already wrong."""
    calls = {"n": 0}

    def raiser():
        calls["n"] += 1
        raise OSError("fork: resource temporarily unavailable")

    for i in range(60):
        assert sentinel._throttled("boom", 300.0, raiser, 1000.0 + i, default=None) is None
    assert calls["n"] == 1, f"{calls['n']} invocations in 60 ticks under a 300s throttle"


def test_a_forced_poll_cannot_bypass_the_git_or_network_probes(monkeypatch):
    """The module promises no caller - "a fast loop, a forced poll, a tool
    call" - can restart a process storm. That held for systemctl only: git shells
    out three times and the net row opens a socket and a resolver, and both sat
    on the _CHECKS cadence, which force=True skips by definition."""
    ran = {"git": 0, "tcp": 0, "dns": 0}
    monkeypatch.setattr(sentinel, "_have", lambda b: b == "git")
    monkeypatch.setattr(sentinel, "_run",
                        lambda args, timeout=5: (ran.__setitem__("git", ran["git"] + 1)
                                                 or "0\n"))
    monkeypatch.setattr(sentinel, "_tcp_ok",
                        lambda h, p, timeout=3: (ran.__setitem__("tcp", ran["tcp"] + 1)
                                                 or True))
    monkeypatch.setattr(sentinel, "_dns_ok",
                        lambda name="x": (ran.__setitem__("dns", ran["dns"] + 1) or True))
    _fake_checks(monkeypatch,
                 ("net", "Network", sentinel.NET_EVERY, sentinel._check_net),
                 ("git", "Repo", sentinel.GIT_EVERY, sentinel._check_git))

    for i in range(120):                       # two minutes of one-second ticks
        sentinel.poll(now=5000.0 + i, force=True)
    assert ran["git"] == 3, f"{ran['git']} git processes for one throttle window"
    assert ran["tcp"] == 1 and ran["dns"] == 1

    sentinel.poll(now=5000.0 + sentinel.GIT_EVERY + 1, force=True)
    assert ran["git"] == 6, "the git throttle never released"


def test_spoken_details_never_carry_a_newline_or_a_url(monkeypatch):
    """Two details are written elsewhere and arrive unclean: an exception
    message, and calendar_state's `error` straight from a Google API. Both
    routinely contain a hard newline and a console URL, and both are read aloud
    by speakable() and handed to notify.deliver by alerts()."""
    def erroring(now=None):
        raise RuntimeError("token store unreadable\nfix at https://console.example/x")

    _fake_checks(monkeypatch, ("google", "Google", 60, erroring))
    sentinel.poll(now=1.0)
    said = sentinel.speakable()
    assert "\n" not in said and "http" not in said, repr(said)
    assert "token store unreadable" in said, "sanitising ate the actual message"

    line = sentinel.alerts(now=1.0)[0][2]
    assert "\n" not in line and "http" not in line, repr(line)


def test_a_google_console_url_is_not_read_aloud(monkeypatch):
    """The real shape: a 403 from the Calendar API, which names a console URL
    and wraps. The URL is stripped BEFORE the 90-character slice, or the slice
    leaves half a URL that no strip can recognise."""
    err = ("HttpError 403: Calendar API has not been used in project 12345 "
           "before or it is disabled.\nEnable it by visiting "
           "https://console.developers.google.com/apis/api/calendar")
    state, detail = sentinel.classify_google({"error": err, "ts": 1.0}, True, 60, now=1.0)
    assert state == "warn"
    assert "http" not in detail and "\n" not in detail, repr(detail)
    assert "console.developers" not in detail
