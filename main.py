"""Cortana entrypoint.

Voice mode (default):
  F9 (hold)  = push-to-talk
  F10        = cycle mode: ptt -> wake ("ok cortana ...") -> open (just talk)
Text mode (debug / hotkey-less fallback):
  python main.py --text
"""
import argparse
import queue
import re
import sys
import threading
import time
from pathlib import Path

import config
import memory
import orchestrator
import tasks
import hud_state
from voice import mic, stt, wake, speech

state = {"mode": config.MODE, "ptt": False,
         "busy": False,        # a request is being processed (LLM working)
         "capturing": False,   # the user is talking right now (VAD-started)
         "exit": None}         # exit code set by _do_system; loops exit on it

# Cancel token for whichever turn the processor thread is currently working
# on. Cleared right before each turn starts; set by a NEWER utterance arriving
# while one is still in flight, so the stale turn bails instead of running to
# completion (or its full step limit) while the user is talking about
# something else. Single processor thread => single slot is always correct.
current_cancel = threading.Event()

# Flag file written before a restart exit; checked on the next startup
RESTART_FLAG = config.ROOT / ".restarting"

# Spoken restart/shutdown with tasks in flight requires saying it twice (90s).
_pending_sys = {"kind": None, "ts": 0.0}

# --- Spoken system commands, matched before the LLM for reliability. ---
_RESTART_RE = re.compile(
    r"^\s*(ok(ay)?[\s,]+)?(cortana[\s,]+)?(please\s+)?"
    r"(time\s+to\s+restart|restart(\s+yourself)?|reboot(\s+yourself)?)"
    r"\s*[.!]?\s*$", re.I)
_SHUTDOWN_RE = re.compile(
    r"^\s*(ok(ay)?[\s,]+)?(cortana[\s,]+)?(please\s+)?"
    r"(time\s+to\s+shut\s*down|shut\s*down|shutdown|power\s*down|go\s+offline)"
    r"\s*[.!]?\s*$", re.I)


def _system_command(text):
    """Return 'restart' | 'shutdown' | None for a spoken system command."""
    if _RESTART_RE.search(text):
        return "restart"
    if _SHUTDOWN_RE.search(text):
        return "shutdown"
    return None


def _confirm_or_warn(kind):
    """Restart/shutdown guard: with background tasks running, require the
    command twice within 90s so in-flight work isn't killed by accident."""
    running = tasks.active_summary()
    if not running:
        return True
    if _pending_sys["kind"] == kind and time.time() - _pending_sys["ts"] < 90:
        return True
    _pending_sys.update(kind=kind, ts=time.time())
    speech.say(f"Still working on {running}. That work dies on {kind} - "
               f"say it again to {kind} anyway.")
    return False


def _do_system(kind):
    """Speak, park the HUD, and flag the exit code (loops exit on it).
    Runs on the processor thread, so it must NOT sys.exit directly - that
    would only kill the worker thread, not the process."""
    if kind == "restart":
        speech.say_wait("Restarting.")
        RESTART_FLAG.touch()          # new instance detects this and says "Back up"
        hud_state.set_state("offline")
        print("[main] clean exit for restart")
        state["exit"] = 0
        return
    speech.say_wait("Shutting down. Goodbye.")
    hud_state.set_state("offline")
    print("[main] clean exit for shutdown")
    state["exit"] = config.SHUTDOWN_CODE


def _startup_announce():
    """If coming back from a restart, announce it. Silent on first boot."""
    if RESTART_FLAG.exists():
        RESTART_FLAG.unlink()
        speech.say("Back up.")


def process(wav_path, cancel=None):
    """Runs on the processor thread. busy/hud-idle lifecycle is owned by the
    caller (processor()) so there's no window where a just-dequeued turn is
    running but state['busy'] hasn't flipped yet."""
    hud_state.set_state("thinking")
    text = stt.transcribe(wav_path)
    if not text:
        hud_state.set_state("idle")
        return
    mode = state["mode"]
    if mode == "wake":
        stripped = wake.wake_match(text)
        if stripped is None:
            hud_state.set_state("idle")
            return
        text = stripped
    elif mode == "open":
        stripped = wake.wake_match(text)
        if stripped is not None:
            text = stripped
        elif not wake.addressed(text, memory.recent_text(6)):
            print(f"(ignored, not addressed to me: {text[:60]})")
            hud_state.set_state("idle")
            return
    print("YOU:", text)
    cmd = _system_command(text)
    if cmd:
        if _confirm_or_warn(cmd):
            _do_system(cmd)
        hud_state.set_state("idle")
        return
    # Any ordinary utterance disarms a pending restart/shutdown confirm, so a
    # later 'restart' re-runs the warn-once flow instead of firing silently
    # (e.g. 'restart' -> warned -> 'never mind' -> 'restart' should warn again).
    _pending_sys["kind"] = None
    reply = orchestrator.handle(text, cancel=cancel)
    if reply is None:
        # Preempted by a newer utterance mid-turn - stay silent, the newer
        # request is next in the queue and will speak its own reply.
        return
    print("CORTANA:", reply)
    # If the agent itself asked to restart/shutdown, let _do_system say the
    # one action line instead of speaking the reply too (avoids double TTS).
    pending = orchestrator.shutdown_requested() or orchestrator.restart_requested()
    if not pending:
        speech.say(reply)
    if orchestrator.shutdown_requested():
        _do_system("shutdown")
    if orchestrator.restart_requested():
        _do_system("restart")


def voice_loop():
    """Full-duplex-feel loop: the MIC keeps listening while requests process
    (utterances queue up and are handled in order). Listening only pauses
    while Cortana is audibly speaking - one mic, one speaker, no echo
    cancellation, so that half-duplex gate is what stops her hearing herself."""
    from pynput import keyboard

    def on_press(key):
        if key == keyboard.Key.f9:
            state["ptt"] = True

    def on_release(key):
        if key == keyboard.Key.f9:
            state["ptt"] = False
        if key == keyboard.Key.f10:
            order = ["ptt", "wake", "open"]
            state["mode"] = order[(order.index(state["mode"]) + 1) % 3]
            print("MODE:", state["mode"])
            hud_state.set_mode(state["mode"])   # reflect in the dashboard AI module
            speech.say(f"{state['mode']} mode")

    keyboard.Listener(on_press=on_press, on_release=on_release).start()

    utterances = queue.Queue()

    def _enqueue(p):
        """Queue an utterance. If the processor is mid-turn, signal that turn
        to bail ASAP - a fresh command always takes priority over finishing
        whatever she was already doing (see current_cancel)."""
        if state["busy"]:
            current_cancel.set()
        utterances.put(p)

    def processor():
        while state["exit"] is None:
            try:
                p = utterances.get(timeout=0.5)
            except queue.Empty:
                continue
            state["busy"] = True          # set BEFORE any work, closes the
            current_cancel.clear()        # race where a new utterance arrives
            try:                          # between dequeue and 'busy' flipping
                process(p, cancel=current_cancel)
            except Exception as e:
                print("[main] process error:", e)
            finally:
                state["busy"] = False
                if not speech.speaking():
                    hud_state.set_state("idle")

    threading.Thread(target=processor, daemon=True, name="processor").start()

    hud_state.set_state("idle")
    hud_state.set_mode(state["mode"])   # publish initial talking mode to the dashboard
    _startup_announce()
    print(f"Cortana up. Mode: {state['mode']}. F9 hold = talk. F10 = cycle mode. Ctrl+C = quit.")
    # Abort a capture the instant Cortana starts speaking (half-duplex: she must
    # not record her own TTS - which can begin mid-capture now that processing is
    # async) or when an exit is pending (so restart/shutdown isn't stuck waiting
    # up to 30s for a segment to finish).
    abort = lambda: speech.speaking() or state["exit"] is not None
    while state["exit"] is None:
        if speech.speaking():          # don't even start listening while speaking
            time.sleep(0.05)
            continue
        try:
            if state["mode"] == "ptt":
                if state["ptt"]:
                    p = mic.record_while(lambda: state["ptt"], should_abort=abort)
                    if p:
                        _enqueue(p)
                else:
                    time.sleep(0.05)
            else:
                p = mic.listen_segment(
                    on_speech_start=lambda: state.__setitem__("capturing", True),
                    should_abort=abort)
                state["capturing"] = False
                if p:
                    _enqueue(p)
        except Exception as e:
            # A transient audio-device error (USB hiccup, PipeWire suspend, unplug)
            # must not kill the main thread - that would take the whole process and
            # every in-flight background task with it. Log, settle, retry.
            print("[main] mic error, recovering:", e)
            state["capturing"] = False
            time.sleep(0.5)
    speech.flush(timeout=15)
    sys.exit(state["exit"])


def text_loop():
    print("Text mode. 'quit' to exit.")
    _startup_announce()
    while state["exit"] is None:
        try:
            t = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if t.lower() in ("quit", "exit"):
            break
        if not t:
            continue
        cmd = _system_command(t)
        if cmd:
            if _confirm_or_warn(cmd):
                _do_system(cmd)
            continue
        print("CORTANA:", orchestrator.handle(t))
        if orchestrator.shutdown_requested():
            _do_system("shutdown")
        elif orchestrator.restart_requested():
            _do_system("restart")
    if state["exit"] is not None:
        speech.flush(timeout=15)
        sys.exit(state["exit"])


def _calendar_loop():
    """Refresh today's calendar events for the dashboard Agenda every 10 min.
    Never triggers the interactive Google consent from here (that would pop a
    browser unexpectedly) - if not connected yet, writes a helpful error and
    keeps trying. Run `python main.py --google-auth` once to connect."""
    import calendar_state
    while state["exit"] is None:
        try:
            if not config.GMAIL_TOKEN.exists():
                calendar_state.write_error("Google not connected - run: python main.py --google-auth")
            else:
                from tools import calendar_tool
                calendar_state.write(calendar_tool.today_events())
        except Exception as e:
            calendar_state.write_error(e)
        for _ in range(600):
            if state["exit"] is not None:
                return
            time.sleep(1)


def _google_auth():
    """Force a fresh Google consent covering all scopes (Gmail + Calendar)."""
    from tools import google_auth
    config.GMAIL_TOKEN.unlink(missing_ok=True)
    google_auth.creds()
    print("Google connected (Gmail + Calendar). Token saved.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", action="store_true", help="text mode (no mic/hotkeys)")
    ap.add_argument("--google-auth", action="store_true",
                    help="re-authorize Google (Gmail + Calendar) in a browser, then exit")
    args = ap.parse_args()
    if args.google_auth:
        _google_auth()
        sys.exit(0)
    memory.init()
    speech.init(voice=not args.text,
                quiet_gate=lambda: not state["busy"] and not state["capturing"]
                                   and not state["ptt"])
    threading.Thread(target=_calendar_loop, daemon=True, name="calendar").start()
    if args.text:
        text_loop()
    else:
        voice_loop()
