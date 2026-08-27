"""Cortana's reasoning, run inside the bridge process for phone-initiated turns.

The phone gets the real pipeline - the same Whisper STT and orchestrator the
desk uses - so a turn from the sofa is indistinguishable from one at the
keyboard, and it keeps working while cortana.service is stopped.

Loading is lazy: a missing API key or broken dependency must degrade to
"talking is unavailable", never stop the bridge from serving state and pairing.
"""
import shutil
import subprocess
import threading

import hud_state
from bridge import hub
from bridge.settings import log

# {"ready": bool, "error": str, "mods": (memory, orchestrator, stt)}
_brain = {"ready": False, "error": "", "mods": None}

_turn_lock = threading.Lock()            # one phone turn at a time
_turn_cancel = {"ev": None}              # cancel token for the in-flight turn
_hud_owned = {"on": False}               # did WE set the current orb state?


def load():
    """Import Cortana's brain once. Returns the _brain dict either way."""
    if _brain["ready"] or _brain["error"]:
        return _brain
    try:
        import memory
        import orchestrator
        from voice import stt, speech
        memory.init()
        # No speaker on this process: route every line Cortana would have
        # spoken (lead preambles, background-task completions) to the phones.
        speech.say = hub.announce
        speech.announce = hub.announce
        speech.say_wait = lambda text, timeout=60: hub.announce(text)
        _brain.update(ready=True, mods=(memory, orchestrator, stt))
    except Exception as e:
        _brain["error"] = f"brain unavailable: {e}"
        log("brain failed to load", e)
    return _brain


def ready():
    return load()["ready"]


def error():
    return _brain["error"]


def _set_hud(state):
    """Reflect phone turns on the dashboard orb without fighting the desk
    process: if Cortana is already mid-turn and it wasn't us who set that,
    leave the state file alone (it is last-writer-wins)."""
    current = hud_state.read_state().get("state", "idle")
    if state == "idle":
        if _hud_owned["on"]:
            _hud_owned["on"] = False
            hud_state.set_state("idle")
        return
    if current in ("thinking", "working", "speaking") and not _hud_owned["on"]:
        return
    _hud_owned["on"] = True
    hud_state.set_state(state, detail="phone")


def run_turn(wav_path, text):
    """Blocking: STT (when audio was sent) plus one full orchestrator turn.

    A newer phone turn cancels the one in flight, mirroring how a new utterance
    preempts the desk loop in main.py. Returns a dict shaped for the phone:
    {transcript, reply} | {error} | {canceled}.
    """
    state = load()
    if not state["ready"]:
        return {"error": state["error"]}
    _memory, orchestrator, stt = state["mods"]

    previous = _turn_cancel["ev"]
    if previous is not None:
        previous.set()
    cancel = threading.Event()
    _turn_cancel["ev"] = cancel

    with _turn_lock:
        if cancel.is_set():
            return {"canceled": True}
        transcript = text
        if wav_path:
            _set_hud("thinking")
            transcript = stt.transcribe(wav_path)
            if not transcript:
                _set_hud("idle")
                return {"transcript": "", "reply": "",
                        "error": "didn't catch that - too quiet or empty"}
        _set_hud("thinking")
        try:
            reply = orchestrator.handle(transcript, cancel=cancel)
        finally:
            _set_hud("idle")
        if reply is None:
            return {"transcript": transcript, "canceled": True}
        _apply_service_request(orchestrator)
        return {"transcript": transcript, "reply": reply}


# A scheduled turn waits this long for a phone turn to finish before giving up.
# Not forever: the lock is also what a phone turn takes, and a wedged
# orchestrator must not turn one stuck alarm into a permanently silent bridge.
PROMPT_LOCK_WAIT = 180


def run_prompt(text, source="scheduler", wait=PROMPT_LOCK_WAIT):
    """Blocking: one orchestrator turn with NO user in the loop.

    Takes the same _turn_lock a phone turn takes, so a 7am briefing and someone
    talking from the sofa cannot run through the orchestrator at the same time.

    The restart/shutdown flags are cleared afterwards, always. The lead has both
    tools and nobody asked for either - a scheduled turn that decides to reboot
    the box would take the dashboard, the bridge and the alarm clock down at 7am
    with nobody in the room. main.py refuses the same thing for the same reason;
    this is the bridge's half of that guarantee.
    """
    state = load()
    if not state["ready"]:
        log(f"{source}: {state['error']}")
        return ""
    _memory, orchestrator, _stt = state["mods"]
    if not _turn_lock.acquire(timeout=wait):
        log(f"{source}: gave up waiting for the turn lock after {wait}s")
        # A sentence, not silence: the caller delivers whatever comes back, and
        # "" would advance the schedule row to delivered with nothing said.
        return "A scheduled item could not run - Cortana was busy with another turn."
    try:
        return orchestrator.handle(text) or ""
    except Exception as e:
        log(f"{source} turn failed", e)
        return ""
    finally:
        # release() LAST, but never behind an unguarded call. _clear_service_
        # request reaches into orchestrator's private flag dicts; if that ever
        # raises, the finally: aborted before the release and _turn_lock was
        # held for the life of the process - every later phone turn and every
        # scheduled turn then blocked for PROMPT_LOCK_WAIT and gave up, with
        # the original traceback long gone from the journal.
        try:
            _clear_service_request(orchestrator, source)
        except Exception as e:
            log("could not clear the service-request flags", e)
        _turn_lock.release()


def _clear_service_request(orchestrator, source):
    """Disarm a restart/shutdown asked for with no user present."""
    if orchestrator.restart_requested() or orchestrator.shutdown_requested():
        orchestrator._restart_flag["do"] = False
        orchestrator._shutdown_flag["do"] = False
        log(f"refused a restart/shutdown asked for by {source}")


def _apply_service_request(orchestrator):
    """A phone-requested restart/shutdown sets flags in THIS process, so act on
    the real cortana.service ourselves, then clear them for the next turn.

    Guarded end to end. This runs AFTER the turn has produced its reply, so an
    unhandled FileNotFoundError here - no systemctl, or a dev box that is not
    Linux at all - threw away a perfectly good answer and handed the phone a
    503 instead. The flags are cleared before the spawn either way, so a failed
    restart cannot re-arm itself on the next turn.
    """
    if orchestrator.restart_requested():
        orchestrator._restart_flag["do"] = False
        argv = ["systemctl", "--user", "restart", "cortana"]
    elif orchestrator.shutdown_requested():
        orchestrator._shutdown_flag["do"] = False
        argv = ["systemctl", "--user", "stop", "cortana"]
    else:
        return
    if not shutil.which("systemctl"):
        log("asked to restart cortana, but systemctl is not on PATH here")
        return
    try:
        subprocess.Popen(argv)
    except Exception as e:
        log("could not reach systemctl", e)
