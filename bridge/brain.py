"""Cortana's reasoning, run inside the bridge process for phone-initiated turns.

The phone gets the real pipeline - the same Whisper STT and orchestrator the
desk uses - so a turn from the sofa is indistinguishable from one at the
keyboard, and it keeps working while cortana.service is stopped.

Loading is lazy: a missing API key or broken dependency must degrade to
"talking is unavailable", never stop the bridge from serving state and pairing.
"""
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


def _apply_service_request(orchestrator):
    """A phone-requested restart/shutdown sets flags in THIS process, so act on
    the real cortana.service ourselves, then clear them for the next turn."""
    if orchestrator.restart_requested():
        orchestrator._restart_flag["do"] = False
        subprocess.Popen(["systemctl", "--user", "restart", "cortana"])
    elif orchestrator.shutdown_requested():
        orchestrator._shutdown_flag["do"] = False
        subprocess.Popen(["systemctl", "--user", "stop", "cortana"])
