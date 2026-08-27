"""Speech output queue. One worker thread owns the speaker, so replies and
background-task announcements never talk over each other.

- say(text): enqueue a reply; returns immediately.
- announce(text): like say, but polite - waits for a quiet moment (user not
  talking, no reply being computed) before speaking. Max hold 60s.
- alert(text): reminders and alarms. Jumps the queue and waits only ~3s for a
  gap. Still goes through the one worker, so it is NEXT, never CONCURRENT.
- say_wait(text): enqueue and block until spoken (system exit lines).
- speaking(): True while audio is actually playing - the mic loop gates on
  this so Cortana never transcribes her own voice (half-duplex).
- init(voice, quiet_gate): voice=False prints instead of TTS (text mode).
"""
import queue
import threading
import time

import audio_ducking
import hud_state

_q = queue.Queue()
# Reminders and alarms. A separate queue rather than a priority flag because a
# fired alarm must not sit behind a backlog of task completions - by the time
# they drained, the thing it was warning about has happened.
_urgent = queue.Queue()
_speaking = threading.Event()
_voice = True
_quiet_gate = lambda: True
_started = False
_lock = threading.Lock()

ANNOUNCE_MAX_HOLD = 60.0   # never sit on a completed task longer than this
# A timer cannot wait a minute for a gap in the conversation. Three seconds
# covers almost every real sentence boundary; past that it speaks anyway and
# the mic loop's existing abort truncates the capture, so the user re-asks. An
# alarm that never fires is a worse failure than a sentence that gets cut.
ALERT_MAX_HOLD = 3.0


def init(voice=True, quiet_gate=None):
    global _voice, _quiet_gate, _started
    _voice = voice
    if quiet_gate is not None:
        _quiet_gate = quiet_gate
    with _lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True, name="speech").start()
            _started = True


def speaking():
    return _speaking.is_set()


def say(text):
    text = (text or "").strip()
    if text:
        _q.put((text, 0.0, None))


def announce(text, max_hold=ANNOUNCE_MAX_HOLD):
    text = (text or "").strip()
    if text:
        _q.put((text, max_hold, None))


def alert(text, max_hold=ALERT_MAX_HOLD, **_kwargs):
    """Reminder/alarm lane: skips whatever is queued, holds only briefly.

    Accepts and ignores extra keyword arguments so the bridge can swap in
    hub.announce as a drop-in replacement, exactly as it already does for
    say/announce.
    """
    text = (text or "").strip()
    if text:
        _urgent.put((text, max_hold, None))


def say_wait(text, timeout=60):
    text = (text or "").strip()
    if not text:
        return
    done = threading.Event()
    _q.put((text, 0.0, done))
    done.wait(timeout)


def flush(timeout=30):
    """Best-effort wait until everything queued has been spoken."""
    deadline = time.time() + timeout
    while time.time() < deadline and (not _q.empty() or not _urgent.empty()
                                      or _speaking.is_set()):
        time.sleep(0.1)


def _speak(text):
    _speaking.set()
    hud_state.set_state("speaking")
    audio_ducking.engage("speaking")
    try:
        if _voice:
            from voice import tts
            tts.speak(text)
        else:
            print("CORTANA:", text)
    finally:
        audio_ducking.release("speaking")
        _speaking.clear()
        if _q.empty() and _urgent.empty():
            hud_state.set_state("idle")


def _worker():
    while True:
        # Urgent first, always. Polling both rather than blocking on one is what
        # lets an alarm overtake a queue of task completions; the 0.2s timeout
        # on the ordinary queue is what keeps that poll cheap.
        try:
            item = _urgent.get_nowait()
        except queue.Empty:
            try:
                item = _q.get(timeout=0.2)
            except queue.Empty:
                continue
        text, max_hold, done = item
        try:
            if max_hold > 0:
                deadline = time.time() + max_hold
                while time.time() < deadline and not _quiet_gate():
                    time.sleep(0.25)
            _speak(text)
        except Exception as e:
            print("speech error:", e)
        finally:
            if done:
                done.set()
