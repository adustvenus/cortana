"""Speech output queue. One worker thread owns the speaker, so replies and
background-task announcements never talk over each other.

- say(text): enqueue a reply; returns immediately.
- announce(text): like say, but polite - waits for a quiet moment (user not
  talking, no reply being computed) before speaking. Max hold 60s.
- say_wait(text): enqueue and block until spoken (system exit lines).
- speaking(): True while audio is actually playing - the mic loop gates on
  this so Cortana never transcribes her own voice (half-duplex).
- init(voice, quiet_gate): voice=False prints instead of TTS (text mode).
"""
import queue
import threading
import time

import hud_state

_q = queue.Queue()
_speaking = threading.Event()
_voice = True
_quiet_gate = lambda: True
_started = False
_lock = threading.Lock()

ANNOUNCE_MAX_HOLD = 60.0   # never sit on a completed task longer than this


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
        _q.put((text, False, None))


def announce(text):
    text = (text or "").strip()
    if text:
        _q.put((text, True, None))


def say_wait(text, timeout=60):
    text = (text or "").strip()
    if not text:
        return
    done = threading.Event()
    _q.put((text, False, done))
    done.wait(timeout)


def flush(timeout=30):
    """Best-effort wait until everything queued has been spoken."""
    deadline = time.time() + timeout
    while time.time() < deadline and (not _q.empty() or _speaking.is_set()):
        time.sleep(0.1)


def _speak(text):
    _speaking.set()
    hud_state.set_state("speaking")
    try:
        if _voice:
            from voice import tts
            tts.speak(text)
        else:
            print("CORTANA:", text)
    finally:
        _speaking.clear()
        if _q.empty():
            hud_state.set_state("idle")


def _worker():
    while True:
        text, polite, done = _q.get()
        try:
            if polite:
                deadline = time.time() + ANNOUNCE_MAX_HOLD
                while time.time() < deadline and not _quiet_gate():
                    time.sleep(0.25)
            _speak(text)
        except Exception as e:
            print("speech error:", e)
        finally:
            if done:
                done.set()
