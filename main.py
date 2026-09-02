"""Cortana entrypoint.

Voice mode (default):
  F9 (hold)  = push-to-talk
  F10        = cycle mode: ptt -> wake ("ok cortana ...") -> open (just talk)
Text mode (debug / hotkey-less fallback):
  python main.py --text
"""
import argparse
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import config
import audio_ducking
import memory
import notify
import orchestrator
import presence
import routines
import schedule
import sentinel
import tasks
import wakeword
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

# orchestrator keeps its per-turn state in module globals (_turn, _stalled,
# _restart_flag). One processor thread made that safe; a scheduler that can also
# start a turn does not, so every entry into orchestrator.handle() takes this.
# Contention is normally zero - it exists so a fired routine QUEUES behind a
# voice turn instead of corrupting its tool_calls count and critique gate.
_turn_lock = threading.Lock()

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
    # The offline gate goes in front of the PAID transcription - that is the
    # entire point of it, since a room full of unrelated speech otherwise buys a
    # Whisper call per utterance before it can be rejected. Dormant by default
    # and fails OPEN, so with no model installed this line is a no-op. Only wake
    # mode: open mode means "just talk", with no wake word to find.
    if state["mode"] == "wake" and not wakeword.detect_wav(wav_path):
        hud_state.set_state("idle")
        return
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
    with _turn_lock:
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
            presence.note_voice()   # the only presence signal that survives
            audio_ducking.engage("ptt")   # with no X11 idle probe installed

    def on_release(key):
        if key == keyboard.Key.f9:
            state["ptt"] = False
            audio_ducking.release("ptt")
        if key == keyboard.Key.f10:
            # No mic -> wake/open would just error-spin. Refuse the swap, stay
            # in PTT, and say why instead of silently doing nothing.
            if not mic.available():
                state["mode"] = "ptt"
                hud_state.set_mode("ptt")
                speech.say("I can't swap modes right now - I have no input "
                           "device. Check the microphone.")
                return
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
        presence.note_voice()
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
                # mic._save() writes with delete=False, and nothing on this path
                # removed it - one WAV per utterance accumulated in the temp dir
                # for the life of the box. The phone path already cleans up
                # after itself (bridge/api_phone.py). Done here rather than in
                # process(), which has several early returns, so an unreadable
                # capture or a wake-word miss frees its file too.
                try:
                    os.unlink(p)
                except OSError:
                    pass

    threading.Thread(target=processor, daemon=True, name="processor").start()

    hud_state.set_state("idle")
    hud_state.set_mode(state["mode"])   # publish initial talking mode to the dashboard
    _startup_announce()
    if not mic.available():
        # Booting without a mic is fine now (it used to crash on import) -
        # announce it once and stay up; F9 presses repeat the warning.
        state["mode"] = "ptt"
        hud_state.set_mode("ptt")
        print("[main] no input device - staying up in PTT, waiting for a mic")
        speech.say("Heads up - I have no input device. Plug in or pick a "
                   "microphone on the dashboard.")
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
                    try:
                        p = mic.record_while(lambda: state["ptt"], should_abort=abort)
                    except mic.MicUnavailable:
                        speech.say("I have no input device at this time - "
                                   "please check the microphone.")
                        # Wait out the rest of this F9 hold so the warning
                        # speaks once per press, not once per loop tick.
                        while state["ptt"] and state["exit"] is None:
                            time.sleep(0.1)
                        continue
                    if p:
                        _enqueue(p)
                else:
                    time.sleep(0.05)
            else:
                def _on_speech_start():
                    state["capturing"] = True
                    audio_ducking.engage("listening")
                try:
                    p = mic.listen_segment(on_speech_start=_on_speech_start,
                                           should_abort=abort)
                except mic.MicUnavailable:
                    # Mic vanished while in wake/open: drop to PTT (the only
                    # mode that doesn't need a always-on mic) and say so once.
                    # Release too: speech may have started before it vanished,
                    # and this path skips the release below via `continue`.
                    state["capturing"] = False
                    audio_ducking.release("listening")
                    state["mode"] = "ptt"
                    hud_state.set_mode("ptt")
                    speech.say("I lost the input device - dropping to push to "
                               "talk. Check the microphone.")
                    continue
                state["capturing"] = False
                audio_ducking.release("listening")
                if p:
                    _enqueue(p)
        except Exception as e:
            # A transient audio-device error (USB hiccup, PipeWire suspend, unplug)
            # must not kill the main thread - that would take the whole process and
            # every in-flight background task with it. Log, settle, retry.
            print("[main] mic error, recovering:", e)
            state["capturing"] = False
            audio_ducking.release("listening")
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


def _mic_state_loop():
    """Publish the input-device inventory (mic_state.json) every 10s so the
    dashboard's AI-module MIC dropdown stays current across plug/unplug."""
    while state["exit"] is None:
        mic.publish_state()
        for _ in range(10):
            if state["exit"] is not None:
                return
            time.sleep(1)


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
            from tools import google_auth
            if isinstance(e, google_auth.AuthExpired):
                # Expired/revoked token: drop the stale events outright. Showing
                # a week-old agenda as if it were today's is worse than showing
                # nothing - it is what made this bug so hard to spot.
                calendar_state.write_error_clearing(
                    "Google access expired - run: python main.py --google-auth")
            else:
                calendar_state.write_error(e)
        for _ in range(600):
            if state["exit"] is not None:
                return
            time.sleep(1)


def _presence_loop():
    """Publish presence_desk.json every 30s.

    Only this process can do it: cortana.service sets DISPLAY and XAUTHORITY,
    cortana-bridge.service sets neither, so an X11 idle probe from the bridge
    returns nothing and would read as 'away'.
    """
    was = None
    while state["exit"] is None:
        try:
            now_state = presence.publish()["state"]
            # Coming back to the desk is what releases anything that was held
            # while the screen was asleep - "you got three things while you were
            # out" falls out of the transition, with no special-casing.
            if was in ("away", "asleep") and now_state == "present":
                notify.release_held()
            was = now_state
        except Exception as e:
            print("[presence] sample failed:", e)
        for _ in range(30):
            if state["exit"] is not None:
                return
            time.sleep(1)


def _routine_turn(prompt):
    """Run a full orchestrator turn on behalf of a routine.

    Injected into routines.py rather than imported by it, so that module stays
    testable on a box with no audio and no API key - and so the lock lives in
    the one place that knows about the voice loop.
    """
    with _turn_lock:
        reply = orchestrator.handle(prompt)
    # Nobody asked for a reboot at 7am, and the lead has both tools.
    if orchestrator.restart_requested() or orchestrator.shutdown_requested():
        orchestrator._restart_flag["do"] = False
        orchestrator._shutdown_flag["do"] = False
        print("[routines] refused a restart/shutdown asked for by a routine turn")
    return reply


def _phone_leg(text, urgency):
    """Hand one line to the bridge to put on the phone.

    Raising here is correct rather than swallowing: notify records a failed leg
    as "phone:failed" in `deliveries`, and a silent success would make an
    unreachable bridge indistinguishable from a delivered notification.
    """
    from bridge import client
    _, err = client.call("POST", "/local/notify",
                         {"text": text, "urgency": urgency})
    if err:
        raise RuntimeError(err)


def _sentinel_loop():
    """Publish sentinel_state.json and speak anything that just got WORSE.

    The speaking half can only happen here: notify.deliver needs presence, and
    presence is only knowable in this process. 60s rather than the scheduler's
    5s because every check throttles itself further underneath.
    """
    while state["exit"] is None:
        try:
            sentinel.publish()
            # Edge-triggered: alerts() returns only transitions, so a disk that
            # is still full says nothing on the next 1,440 ticks.
            for _key, urgency, line in sentinel.alerts():
                notify.deliver(line, urgency, src="sentinel")
        except Exception as e:
            print("[sentinel] poll failed:", e)
        for _ in range(max(5, int(sentinel.INTERVAL))):
            if state["exit"] is not None:
                return
            time.sleep(1)


def _notes_loop():
    """Keep the knowledge index current: conversation turns, then one bounded
    slice of the document walk.

    Slow on purpose and bounded on purpose. Documents do not change fast enough
    to justify a tighter loop, and each pass resumes where the last one stopped
    rather than re-walking ~/Downloads - the idle burn this box has already been
    bitten by once.
    """
    from tools import notes            # lazy, same reason as the tool imports
    while state["exit"] is None:
        try:
            notes.tick()
        except Exception as e:
            print("[notes] tick failed:", e)
        for _ in range(max(30, int(config.NOTES_TICK))):
            if state["exit"] is not None:
                return
            time.sleep(1)


def _inbox_loop():
    """Speak what the BRIDGE routed to the desk while it owned the tick.

    The bridge has no speaker, so over there notify's desk and board legs write
    speech_inbox.json instead. This is the other half. It only ever READS - the
    bridge is the single writer - and the high-water mark lives in memory.meta,
    so nothing is spoken twice and nothing is replayed after a restart.

    The mtime check is what makes polling affordable: nothing is queued on the
    overwhelming majority of ticks, and a JSON parse every two seconds forever
    on a laptop is exactly the idle burn that had to be walked back once.
    """
    from bridge import inbox           # lazy: pulls config, nothing heavier
    seen = -1.0
    while state["exit"] is None:
        try:
            stamp = inbox.mtime()
            if stamp != seen:
                seen = stamp
                for item in inbox.drain():
                    text = item.get("text", "")
                    if item.get("surface") == "board":
                        hud_state.think(text[:120])
                    elif item.get("urgency") in ("urgent", "critical"):
                        speech.alert(text)
                    else:
                        speech.announce(text)
        except Exception as e:
            print("[inbox] drain failed:", e)
        for _ in range(2):
            if state["exit"] is not None:
                return
            time.sleep(1)


def _fire(row, fire_ts):
    """Run one due occurrence. Runs on the scheduler thread."""
    action, payload = row["action"], row["payload"]
    late = time.time() - fire_ts

    # A schedules row whose payload names a routine IS that routine's clock
    # trigger - routines.py deliberately owns no recurrence of its own, so there
    # is ONE place where "7am daily" is expanded, not two.
    if payload.get("routine"):
        routines.run(payload["routine"], turn=_routine_turn,
                     run_agent=orchestrator.run_agent)
        return

    if action == "delegate":
        msg = tasks.start(payload["agent"], payload["task"],
                          runner=lambda a, t, c: orchestrator.run_agent(a, t, cancel=c))
        # tasks.start returns a line meant for the lead to relay to the user.
        # With no user in the loop it has nowhere to go, so a routine firing
        # into a full slot table would vanish silently. Surface refusals.
        if msg and msg.lstrip().lower().startswith(("all ", "can't", "cannot")):
            notify.deliver(msg, "normal", src=f"schedule:{row['id']}", ref=row["id"])
        return

    if action == "turn":
        with _turn_lock:
            reply = orchestrator.handle(payload.get("prompt") or row["title"])
        # A scheduled turn must never be able to take the box down: the lead has
        # restart/shutdown tools and no user is present to have asked for it.
        if orchestrator.restart_requested() or orchestrator.shutdown_requested():
            orchestrator._restart_flag["do"] = False
            orchestrator._shutdown_flag["do"] = False
            print("[sched] refused a restart/shutdown asked for by a scheduled turn")
        if reply:
            notify.deliver(reply, row["urgency"], src=f"schedule:{row['id']}",
                           ref=row["id"])
        return

    text = payload.get("text") or row["title"]
    if late > 120:
        text = f"This was due at {schedule.to_local(fire_ts):%H:%M}. {text}"
    notify.deliver(text, row["urgency"], src=f"schedule:{row['id']}", ref=row["id"])


def _scheduler_loop():
    """Phase 1: the tick lives here, in the cortana process, so the whole engine
    ships without touching a single self-edit-protected file.

    Phase 2 moves it into the bridge, which is Restart=always and therefore
    survives a deliberate `shut down` (exit 42) - a 7am alarm should outlive
    "stop listening to me". Running both during that cutover is safe: claim()
    is a conditional UPDATE and sqlite serialises writers, so exactly one
    ticker can ever win an occurrence.
    """
    try:
        recovered = schedule.recover()
        if recovered:
            print(f"[sched] recovered {recovered} item(s) stranded mid-fire")
    except Exception as e:
        print("[sched] recover failed:", e)
    while state["exit"] is None:
        try:
            schedule.tick(_fire, owner="cortana")
        except Exception as e:
            print("[sched] tick error:", e)
        try:
            # Same tick, no second daemon. routines.tick rate-limits itself to
            # config.ROUTINE_TICK internally, so calling it every 5s is cheap.
            routines.tick(turn=_routine_turn, run_agent=orchestrator.run_agent)
        except Exception as e:
            print("[routines] tick error:", e)
        for _ in range(max(1, int(config.SCHED_TICK))):
            if state["exit"] is not None:
                return
            time.sleep(1)


def _google_auth():
    """Force a fresh Google consent covering all scopes (Gmail + Calendar)."""
    from tools import google_auth
    config.GMAIL_TOKEN.unlink(missing_ok=True)
    google_auth.creds(interactive=True)
    print("Google connected (Gmail + Calendar). Token saved.")
    print("\nIMPORTANT: if this Cloud project's OAuth consent screen is still in")
    print("'Testing', Google will expire this token again in 7 DAYS. Publish it:")
    print("  console.cloud.google.com -> APIs & Services -> OAuth consent screen")
    print("  -> PUBLISH APP   (one 'unverified app' warning, then it stops expiring)")


def _calendar_once():
    """Run one calendar refresh, print the result, and write the state file the
    dashboard reads. Diagnostic: `python main.py --calendar-once`."""
    import calendar_state
    print("token.json present:", config.GMAIL_TOKEN.exists())
    try:
        from tools import calendar_tool
        evs = calendar_tool.today_events()
        calendar_state.write(evs)
        print(f"Wrote {calendar_state.STATE_FILE}")
        print(f"{len(evs)} event(s) today:")
        for e in evs:
            print(f"  {e['time']}  {e['title']}" + ("  (past)" if e.get("past") else ""))
    except Exception as e:
        calendar_state.write_error(e)
        print("CALENDAR ERROR:", repr(e))
        print("Wrote error to", calendar_state.STATE_FILE)


def _calendar_debug():
    """Show exactly what Google returns, per calendar, unfiltered. Use this when
    the Agenda shows an event you don't have, or misses one you do:
        ./venv/bin/python main.py --calendar-debug
    It prints which Google ACCOUNT the token belongs to, every calendar on it,
    and today's raw events per calendar with the fields our filter looks at."""
    import json as _json
    print("token.json present:", config.GMAIL_TOKEN.exists())
    if not config.GMAIL_TOKEN.exists():
        print("Not connected. Run: python main.py --google-auth")
        return
    from tools import calendar_tool, google_auth
    try:
        from googleapiclient.discovery import build
        me = build("oauth2", "v2",
                   credentials=google_auth.creds()).userinfo().get().execute()
        print("Google account:", me.get("email", "(unknown)"))
    except google_auth.AuthExpired as e:
        # The single most common cause of a wrong/frozen agenda: nothing can be
        # fetched, so whatever was last written keeps being displayed.
        print("\n*** GOOGLE ACCESS EXPIRED - this is why the agenda is wrong ***")
        print(e)
        print("\nUntil this is fixed, the Agenda can only show whatever was last")
        print("fetched, which is why it can list events you deleted and miss ones")
        print("you added.")
        return
    except Exception as e:
        print("Could not read the account email:", e)

    try:
        cals = calendar_tool.calendars()
    except google_auth.AuthExpired as e:
        print("\n*** GOOGLE ACCESS EXPIRED ***\n" + str(e))
        return
    except Exception as e:
        print("CALENDAR LIST ERROR:", e)
        return
    print(f"\n{len(cals)} calendar(s) on this account:")
    for c in cals:
        flags = []
        if c["primary"]:
            flags.append("PRIMARY")
        flags.append("selected" if c["selected"] else "NOT selected")
        print(f"  - {c['summary']!r}  [{', '.join(flags)}]  id={c['id']}")

    lo, hi, _tz = calendar_tool._local_day_bounds()
    print(f"\nWindow queried (local time): {lo}  ->  {hi}")
    for c in cals:
        try:
            items = calendar_tool.raw_today(c["id"])
        except Exception as e:
            print(f"\n{c['summary']!r}: ERROR {e}")
            continue
        print(f"\n{c['summary']!r}: {len(items)} raw event(s) today")
        for e in items:
            start = e.get("start", {})
            when = start.get("dateTime") or start.get("date") or "?"
            kept = calendar_tool._is_real_event(e)
            why = ""
            if not kept:
                if e.get("eventType") in calendar_tool._SKIP_TYPES:
                    why = f"eventType={e.get('eventType')}"
                elif e.get("status") == "cancelled":
                    why = "status=cancelled"
                else:
                    why = "you declined it"
            mine = [a for a in (e.get("attendees") or []) if a.get("self")]
            print(f"   {'KEPT  ' if kept else 'FILTERED'} {when}  {e.get('summary','(no title)')!r}"
                  f"  eventType={e.get('eventType','default')} status={e.get('status','')}"
                  + (f" self={mine[0].get('responseStatus')}" if mine else "")
                  + (f"   <- dropped because {why}" if why else ""))

    print("\nWhat the Agenda will show (merged + filtered):")
    try:
        for e in calendar_tool.today_events():
            print(f"   {e['time']}  {e['title']}" + ("  (past)" if e.get("past") else ""))
    except Exception as e:
        print("   ERROR:", e)
    print("\nIf an event you expect is missing above, check whether its calendar "
          "is listed and 'selected'; if the account email isn't the one you add "
          "events in, re-run: python main.py --google-auth")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", action="store_true", help="text mode (no mic/hotkeys)")
    ap.add_argument("--calendar-debug", action="store_true",
                    help="diagnose the Agenda: list calendars + today's raw events, then exit")
    ap.add_argument("--google-auth", action="store_true",
                    help="re-authorize Google (Gmail + Calendar) in a browser, then exit")
    ap.add_argument("--calendar-once", action="store_true",
                    help="fetch today's calendar once, print it, write the state file, then exit")
    args = ap.parse_args()
    if args.google_auth:
        _google_auth()
        sys.exit(0)
    if args.calendar_once:
        _calendar_once()
        sys.exit(0)
    if args.calendar_debug:
        _calendar_debug()
        sys.exit(0)
    memory.init()
    speech.init(voice=not args.text,
                quiet_gate=lambda: not state["busy"] and not state["capturing"]
                                   and not state["ptt"])
    # Delivery legs this process can actually serve. The phone leg belongs to
    # the bridge (it owns the WebSocket), so it is deliberately NOT registered
    # here - notify records the miss in `deliveries` rather than pretending.
    notify.register(
        desk=lambda text, urgency: (speech.alert(text)
                                    if urgency in ("urgent", "critical")
                                    else speech.announce(text)),
        board=lambda text, urgency: hud_state.think(text[:120]),
        # The phone leg this process never had. The WebSocket lives in the
        # bridge and nowhere else, so everything routed to the phone from HERE
        # recorded "phone:unavailable" while a phone sat connected - which is
        # most of why push notifications looked dead. bridge.client is
        # stdlib-only, so this never drags aiohttp into the voice process.
        phone=_phone_leg)
    threading.Thread(target=_calendar_loop, daemon=True, name="calendar").start()
    threading.Thread(target=_mic_state_loop, daemon=True, name="mic-state").start()
    threading.Thread(target=_presence_loop, daemon=True, name="presence").start()
    threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler").start()
    threading.Thread(target=_sentinel_loop, daemon=True, name="sentinel").start()
    threading.Thread(target=_notes_loop, daemon=True, name="notes").start()
    # Drains what the bridge routed here while IT owned the scheduler tick.
    threading.Thread(target=_inbox_loop, daemon=True, name="inbox").start()
    if args.text:
        text_loop()
    else:
        voice_loop()
