"""Offline wake gate and address-log learning tests.

The feature ships DORMANT - no library, no model, nothing configured - so the
first block below is not a formality. It is the only proof that adding a wake
engine to the tree changed nothing about how Cortana listens today. Everything
else here pins a failure that would be silent on the runtime box: a gate that
fails closed and makes her deaf, a malformed chunk that kills the capture
loop, or a few-shot prompt that grows without bound on a table that only ever
gets longer.
"""
import time
from array import array

import pytest

import memory
import wakeword
from voice import stt, wake


@pytest.fixture(autouse=True)
def fresh():
    """Backend resolution is cached on purpose, so every test starts by
    forgetting it. Without this the first test to load a fake engine would
    hand it to all the others."""
    wakeword.reload()
    wake._cache.update(ts=0.0, text="")
    yield
    wakeword.reload()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Throwaway database. memory.DB_PATH is read inside _c(), so patching the
    module attribute is enough - same trick as test_schedule.py."""
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.init()
    return tmp_path


def _fake_backend(predict, frame=4, name="fake"):
    """Install an engine without any of the libraries being present. Tests
    reach into _st because the alternative is shipping a public injection
    hook that exists only for tests."""
    wakeword._st.update(resolved=True, backend=wakeword._Backend(name, frame, predict),
                        reason="fake engine")


# ── the shipped default: nothing installed, nothing changes ────────────────
def test_unavailable_with_nothing_configured():
    """THE ship-safe test. No engine named in config means dormant, and it
    must say so in a sentence rather than by raising."""
    assert wakeword.available() is False
    assert wakeword.engine() == ""
    assert wakeword.frame_samples() == 0
    r = wakeword.reason()
    assert r and r[0].isupper() and r.endswith(".")


def test_dormant_gate_never_swallows_audio(tmp_path):
    """FAILS OPEN. detect_wav on a dormant gate must say 'transcribe it' -
    a gate that returns False here silently makes Cortana deaf, which is far
    worse than the Whisper call it was meant to save."""
    assert wakeword.detect_wav(tmp_path / "does-not-exist.wav") is True


def test_dormant_detect_is_false_and_silent():
    assert wakeword.detect(b"\x00\x01" * 100) is False


def test_unknown_engine_name_stays_dormant(monkeypatch):
    monkeypatch.setattr(wakeword.config, "WAKEWORD_ENGINE", "porcuwhat", raising=False)
    wakeword.reload()
    assert wakeword.available() is False
    assert "porcuwhat" in wakeword.reason()


def test_missing_model_file_names_the_file(monkeypatch, tmp_path):
    """The expected state on a fresh clone: engine configured, model not
    committed yet. The reason has to name the file or the user cannot act."""
    monkeypatch.setattr(wakeword.config, "WAKEWORD_ENGINE", "openwakeword", raising=False)
    monkeypatch.setattr(wakeword.config, "WAKEWORD_MODEL",
                        str(tmp_path / "cortana.onnx"), raising=False)
    wakeword.reload()
    assert wakeword.available() is False
    assert "cortana.onnx" in wakeword.reason()


def test_porcupine_without_a_key_stays_dormant(monkeypatch):
    monkeypatch.setattr(wakeword.config, "WAKEWORD_ENGINE", "porcupine", raising=False)
    monkeypatch.setattr(wakeword.config, "PICOVOICE_ACCESS_KEY", "", raising=False)
    wakeword.reload()
    assert wakeword.available() is False
    assert "key" in wakeword.reason().lower()


def test_status_is_json_safe_when_dormant():
    s = wakeword.status()
    assert s["available"] is False and s["sample_rate"] == 16000
    assert set(s) == {"engine", "available", "reason", "frame", "sample_rate"}


# ── detect() must never raise, whatever it is handed ───────────────────────
ODD_INPUTS = [None, b"", b"\x00", bytearray(b"\x01\x02\x03"), memoryview(b"\x01\x02"),
              [], [1, 2, 3], [999999, -999999], ["nonsense", None, 3],
              [(5,), (6,)], "a string", 17, {"not": "audio"}, 3.5,
              array("h", [1, 2, 3, 4])]


@pytest.mark.parametrize("bad", ODD_INPUTS)
def test_detect_never_raises_when_dormant(bad):
    assert wakeword.detect(bad) is False


@pytest.mark.parametrize("bad", ODD_INPUTS)
def test_detect_never_raises_with_a_live_engine(bad):
    """The dormant path short-circuits before conversion, so the conversion
    itself is only actually exercised with a backend loaded."""
    _fake_backend(lambda f: False)
    assert wakeword.detect(bad) is False


def test_odd_byte_count_drops_the_half_sample():
    """A truncated stream read is a real event, not a hypothetical. Three
    bytes must become one sample, not an exception."""
    assert list(wakeword._to_samples(b"\x01\x00\x02")) == [1]


def test_int16_bytes_round_trip():
    assert list(wakeword._to_samples(array("h", [-2, 7, 300]).tobytes())) == [-2, 7, 300]


def test_an_int16_array_is_not_silently_dropped():
    """array('h') is the buffer type this module itself uses, and it has
    .tobytes but no .dtype - so it fell through every branch and returned
    empty, which reads as 'nobody said anything' rather than as an error."""
    assert list(wakeword._to_samples(array("h", [4, 5, 6]))) == [4, 5, 6]
    assert list(wakeword._to_samples(array("f", [1.0, 2.0]))) == [1, 2]


def test_a_live_gate_still_fails_open_when_soundfile_is_missing(tmp_path):
    """The engine loads but the audio cannot be read. Every one of those paths
    has to mean 'transcribe it anyway', never 'throw the utterance away'."""
    _fake_backend(lambda f: False)
    assert wakeword.detect_wav(tmp_path / "nope.wav") is True


def test_out_of_range_values_are_clamped_not_wrapped():
    """array('h').append raises OverflowError on 99999; wrapping it into a
    negative sample would be a plausible-looking lie fed to the model."""
    assert list(wakeword._to_samples([99999, -99999])) == [32767, -32768]


def test_a_backend_that_throws_is_taken_out_of_service():
    """One bad frame must not spam the journal and add latency to every chunk
    for the rest of the process's life."""
    def boom(frame):
        raise RuntimeError("onnx exploded")
    _fake_backend(boom)
    assert wakeword.detect([1, 2, 3, 4]) is False
    assert wakeword.available() is False
    assert "stopped working" in wakeword.reason()


# ── framing and cooldown ───────────────────────────────────────────────────
def test_frames_are_assembled_across_chunks():
    """The mic hands over 100ms blocks; the engine wants its own frame size.
    Detection must survive a wake word split across two reads."""
    seen = []
    _fake_backend(lambda f: seen.append(list(f)) or False, frame=4)
    assert wakeword.detect([1, 2, 3]) is False       # not a whole frame yet
    assert seen == []
    wakeword.detect([4, 5, 6, 7, 8])
    assert seen == [[1, 2, 3, 4], [5, 6, 7, 8]]


def test_reset_discards_a_partial_frame():
    """Otherwise the tail of one capture and the head of the next get glued
    into a frame that no one ever spoke."""
    seen = []
    _fake_backend(lambda f: seen.append(list(f)) or False, frame=4)
    wakeword.detect([1, 2, 3])
    wakeword.reset()
    wakeword.detect([4, 5, 6, 7])
    assert seen == [[4, 5, 6, 7]]


def test_cooldown_suppresses_the_rest_of_the_same_word(monkeypatch):
    """One spoken "Cortana" spans several frames and would otherwise fire on
    every one of them, queueing duplicate turns."""
    monkeypatch.setattr(wakeword.config, "WAKEWORD_COOLDOWN", 60.0, raising=False)
    _fake_backend(lambda f: True, frame=2)
    assert wakeword.detect([1, 2, 3, 4, 5, 6]) is True
    assert wakeword.detect([1, 2, 3, 4]) is False
    wakeword.reset()
    assert wakeword.detect([1, 2]) is True


def test_cooldown_expires():
    _fake_backend(lambda f: True, frame=2)
    assert wakeword.detect([1, 2]) is True
    wakeword._st["mute_until"] = time.time() - 1
    assert wakeword.detect([3, 4]) is True


def test_buffer_does_not_grow_while_muted():
    """Muted frames are consumed, not skipped. A capture loop running for an
    hour behind a cooldown must not accumulate an hour of PCM."""
    _fake_backend(lambda f: False, frame=2)
    wakeword._st["mute_until"] = time.time() + 60
    for _ in range(50):
        wakeword.detect([1, 2, 3, 4])
    assert len(wakeword._st["buf"]) < 4


# ── address_log as training data ───────────────────────────────────────────
def _log(rows):
    for text, decision in rows:
        memory.log_address_decision(text, decision)
        time.sleep(0.001)   # ts is the only ordering key and it is a float second


def test_examples_on_an_empty_table(db):
    """Open mode on a fresh install has no history at all. The builder must
    return nothing and the prompt must fall back to the bare instruction."""
    assert wake.address_examples() == []
    assert wake._examples_block() == ""


def test_examples_survive_a_missing_table(tmp_path, monkeypatch):
    """memory.init() has not run: reading training data must never be the
    thing that stops Cortana answering."""
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "never-created.db")
    assert wake.address_examples() == []


def test_examples_are_bounded_however_long_the_log_gets(db):
    """This text is billed on every open-mode utterance. Ten thousand rows and
    ten rows must produce the same prompt size."""
    _log([(f"utterance number {i} " + "x" * 400, "yes" if i % 2 else "no")
          for i in range(60)])
    ex = wake.address_examples()
    assert 0 < len(ex) <= 8
    assert all(len(t) <= wake._EX_CHARS for t, _ in ex)
    block = wake._examples_block()
    assert len(block) < 1200


def test_examples_are_balanced_after_an_evening_of_television(db):
    """A long run of NOs at the head of the log, handed over unbalanced,
    teaches the classifier to answer NO to everything - which is exactly how
    open mode stops responding at all."""
    _log([("cortana what is the time", "yes"), ("play something else", "yes")])
    _log([(f"tv chatter {i}", "no") for i in range(30)])
    ex = wake.address_examples(limit=4)
    labels = [label for _, label in ex]
    assert labels.count("YES") >= 1 and labels.count("NO") >= 1


def test_lopsided_log_still_fills_the_quota(db):
    """Backfill: with only NOs recorded, eight slots should still carry eight
    examples rather than the four the balance rule would allow."""
    _log([(f"only nos {i}", "no") for i in range(20)])
    assert len(wake.address_examples(limit=8)) == 8


def test_error_rows_are_never_taught(db):
    """A failed API call used to be written as a NO. Teaching that back would
    make every outage permanently bias the classifier."""
    _log([("did the network drop", "error")])
    assert wake.address_examples() == []


def test_a_correction_beats_the_verdict_it_corrects(db):
    """Newest-first plus dedupe-by-text is the whole precedence rule; this is
    the test that says so out loud."""
    _log([("turn the kitchen light off", "no")])
    msg = wake.correct_last(True)
    ex = dict((t, label) for t, label in wake.address_examples())
    assert ex["turn the kitchen light off"] == "YES"
    assert "should have" in msg


def test_correcting_with_no_history_says_so(db):
    assert "no recent decision" in wake.correct_last(True)


def test_correction_takes_effect_without_waiting_out_the_cache(db):
    """The cache exists for latency, but a user who just said "no, I was
    talking to you" must not be told to wait two minutes."""
    _log([("call him back", "yes")])
    assert "-> YES" in wake._examples_block()
    wake.correct_last(False)
    assert "-> NO" in wake._examples_block()


def test_correction_skips_error_rows(db):
    """Correcting an utterance the classifier never actually judged would
    invent training data out of an outage."""
    _log([("real utterance", "yes"), ("outage utterance", "error")])
    assert wake.last_decision()[0] == "real utterance"


def test_addressed_logs_an_error_not_a_no(db, monkeypatch):
    """The old code wrote a NO row when the API call failed. Harmless while
    nothing read the table; poison now that these rows are the examples."""
    monkeypatch.setattr(wake, "_get_client", lambda: (_ for _ in ()).throw(RuntimeError("no network")))
    assert wake.addressed("hello there") is False
    con = memory.connect()
    rows = con.execute("SELECT decision FROM address_log").fetchall()
    con.close()
    assert rows == [("error",)]
    assert wake.address_examples() == []


def test_wake_match_still_strips_the_prefix():
    """Regression guard: the offline gate is additive, it does not replace the
    prefix stripper that both wake and open mode still depend on."""
    assert wake.wake_match("ok cortana, what's the time") == "what's the time"
    assert wake.wake_match("cortana") == "Yes?"
    assert wake.wake_match("pass the salt") is None


# ── the local STT flag ─────────────────────────────────────────────────────
def test_local_stt_defaults_off():
    """faster-whisper would claim about half the free RAM on the runtime box
    to save an API call worth well under a cent a minute. Config-driven now,
    but the default must stay off."""
    assert stt.USE_LOCAL is False


def test_stt_imports_without_the_openai_sdk():
    """The client is built lazily; importing this module on a box with no
    openai package (CI, and this dev box) must not fail."""
    assert stt._client is None


# ── review additions: silent failures the first pass left in ───────────────
class _FakeSoundfile:
    """Stands in for the real soundfile so these tests run in CI, which has
    neither it nor numpy. detect_wav() imports it inside the function, so
    putting one in sys.modules is enough."""

    def __init__(self, audio, rate):
        self._audio, self._rate = audio, rate

    def read(self, path, dtype=None):
        return self._audio, self._rate


def _with_soundfile(monkeypatch, audio, rate):
    import sys
    monkeypatch.setitem(sys.modules, "soundfile", _FakeSoundfile(audio, rate))


def test_a_wrong_sample_rate_is_reported_not_only_tolerated(monkeypatch, tmp_path):
    """mic._resolve() really does fall back to a device's native rate when 16k
    is unsupported, and _save() writes the WAV at that rate. The gate then
    passes every utterance through and the whole cost saving disappears with
    nothing in the journal to say why. Failing open is right; failing open in
    silence is the bug."""
    _fake_backend(lambda f: True, frame=2)
    _with_soundfile(monkeypatch, [1, 2, 3, 4], 44100)
    assert wakeword.detect_wav(tmp_path / "capture.wav") is True
    assert "44100" in wakeword.reason() and "16000" in wakeword.reason()


def test_a_correct_sample_rate_clears_the_warning(monkeypatch, tmp_path):
    """Plugging in a mic that does support 16k must take the complaint back
    down, or the dashboard shows a stale grievance forever."""
    _fake_backend(lambda f: True, frame=2)
    _with_soundfile(monkeypatch, [1, 2, 3, 4], 44100)
    wakeword.detect_wav(tmp_path / "bad.wav")
    _with_soundfile(monkeypatch, [1, 2, 3, 4], 16000)
    assert wakeword.detect_wav(tmp_path / "good.wav") is True
    assert "44100" not in wakeword.reason()


def test_a_zero_length_frame_cannot_hang_the_capture_thread():
    """detect()'s drain loop is `while remaining >= frame`. A frame of zero
    spins forever inside the thread that records audio - the one failure mode
    here that is worse than the gate being deaf."""
    assert wakeword._Backend("x", 0, lambda f: False).frame == 1
    assert wakeword._Backend("x", None, lambda f: False).frame == 1


def test_the_picovoice_key_is_never_quoted_back(monkeypatch):
    """Porcupine puts the AccessKey in its own error text, and reason() is both
    spoken aloud and published by status() to the dashboard. A secret that only
    escapes on the error path has still escaped."""
    monkeypatch.setattr(wakeword.config, "PICOVOICE_ACCESS_KEY",
                        "sekret-key-abc123", raising=False)
    assert "sekret" not in wakeword._redact("bad key: sekret-key-abc123")
    _fake_backend(lambda f: (_ for _ in ()).throw(RuntimeError("bad key sekret-key-abc123")))
    wakeword.detect([1, 2, 3, 4])
    assert "sekret" not in wakeword.reason()


def test_the_buffer_is_emptied_once_a_chunk_is_fully_framed():
    """The drain walks an offset and splices once at the end; an off-by-one
    there would either re-feed the model the same frame forever or leak a
    capture's worth of PCM per utterance."""
    _fake_backend(lambda f: False, frame=4)
    wakeword.detect(list(range(1, 17)))
    assert len(wakeword._st["buf"]) == 0
    wakeword.detect(list(range(1, 7)))
    assert len(wakeword._st["buf"]) == 2


def test_a_dead_backend_does_not_keep_a_capture_in_memory():
    """Once the engine is out of service nothing will ever drain the buffer,
    so whatever was in flight would sit resident for the life of the process."""
    _fake_backend(lambda f: (_ for _ in ()).throw(RuntimeError("onnx exploded")), frame=2)
    wakeword.detect(list(range(1000)))
    assert len(wakeword._st["buf"]) == 0


def test_a_float_audio_block_is_not_silently_heard_as_silence():
    """A float32 block is -1.0..1.0 and int() truncates every sample to zero.
    The model never fires and never complains - it just looks like nobody has
    said the wake word since the day the dtype changed."""
    class _Floats(list):
        dtype = "float32"

        def tobytes(self):
            raise AssertionError("float bytes are not int16 bytes")

    out = list(wakeword._to_samples(_Floats([0.5, -0.5, 0.0])))
    assert out == [16383, -16383, 0]


# ── review additions: voice/wake.py and voice/stt.py ───────────────────────
def test_an_empty_log_is_cached_like_any_other_result(db, monkeypatch):
    """Keying the cache on the TEXT meant the empty result was never cached, so
    a fresh install - every install, on day one - opened state.db and sorted
    the log on every single open-mode utterance, on the latency path the cache
    was added to protect."""
    calls = []
    real = wake._read
    monkeypatch.setattr(wake, "_read", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert wake._examples_block() == ""
    assert wake._examples_block() == ""
    assert len(calls) == 1


def test_a_failed_address_log_write_does_not_lose_the_turn(db, monkeypatch):
    """address_log is training data, not the answer. A locked database must not
    turn 'the classifier said yes' into an exception climbing out of
    addressed() and past the whole utterance."""
    monkeypatch.setattr(memory, "log_address_decision",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("database is locked")))

    class _R:
        content = [type("T", (), {"text": "YES"})()]

    monkeypatch.setattr(wake, "_get_client",
                        lambda: type("C", (), {"messages": type("M", (), {
                            "create": staticmethod(lambda **kw: _R())})()})())
    assert wake.addressed("cortana are you there") is True


def test_a_string_flag_of_zero_leaves_local_stt_off():
    """Every switch in config.py is built from os.getenv, so what arrives here
    is far more likely to be the string "0" than the boolean False - and
    bool("0") is True. That would claim about half the free RAM on the runtime
    box on the strength of a setting written to turn the feature OFF."""
    assert stt._flag("STT_USE_LOCAL") is False
    for off in ("0", "false", "False", "no", "off", "", "   "):
        stt.config.STT_USE_LOCAL = off
        assert stt._flag("STT_USE_LOCAL") is False, off
    for on in ("1", "true", "yes"):
        stt.config.STT_USE_LOCAL = on
        assert stt._flag("STT_USE_LOCAL") is True, on
    del stt.config.STT_USE_LOCAL


def test_local_stt_failure_is_a_quiet_skip_not_a_traceback(monkeypatch, tmp_path):
    """faster-whisper is a manual, opt-in install, so 'flag on, library absent'
    is the likely first run. An ImportError raised from here goes up into the
    voice loop as a traceback per utterance instead of behaving like the API
    path, which already returns an empty transcript."""
    import sys

    class _Vec(list):
        def astype(self, _):
            return _Vec(self)

        def __pow__(self, n):
            return _Vec(v ** n for v in self)

    fake_np = type("np", (), {"float32": "f4",
                              "mean": staticmethod(lambda x: sum(x) / len(x)),
                              "sqrt": staticmethod(lambda x: x ** 0.5)})
    monkeypatch.setitem(sys.modules, "numpy", fake_np)
    _with_soundfile(monkeypatch, _Vec([5000.0] * 16), 16000)
    monkeypatch.setattr(stt, "USE_LOCAL", True)
    monkeypatch.setattr(stt, "_transcribe_local",
                        lambda p: (_ for _ in ()).throw(ImportError("No module named 'faster_whisper'")))
    assert stt.transcribe(str(tmp_path / "x.wav")) == ""
