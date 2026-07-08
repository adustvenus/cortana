"""Cortana entrypoint.

Voice mode (default):
  F9 (hold)  = push-to-talk
  F10        = cycle mode: ptt -> wake ("ok cortana ...") -> open (just talk)
Text mode (debug / hotkey-less fallback):
  python main.py --text
"""
import argparse
import re
import sys
import time

import config
import memory
import orchestrator
import hud_state
from voice import mic, stt, tts, wake

state = {"mode": config.MODE, "ptt": False, "busy": False}

# --- Spoken system commands, matched before the LLM for reliability. ---
# Anchored to the whole utterance so incidental mentions ("how do I restart my
# router") don't trigger them. An optional "cortana" / "ok cortana" prefix is
# allowed. These run even if the model would never think to call the tool.
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


def _do_system(kind):
    """Speak, park the HUD, and exit with the code the launcher expects."""
    if kind == "restart":
        tts.speak("Restarting now. Back in a moment.")
        hud_state.set_state("offline")
        print("[main] clean exit for restart")
        sys.exit(0)
    tts.speak("Shutting down. Goodbye.")
    hud_state.set_state("offline")
    print("[main] clean exit for shutdown")
    sys.exit(config.SHUTDOWN_CODE)


def process(wav_path):
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
    # Spoken system commands take priority over the LLM.
    cmd = _system_command(text)
    if cmd:
        _do_system(cmd)
    state["busy"] = True
    try:
        reply = orchestrator.handle(text)
        print("CORTANA:", reply)
        # If the agent itself asked to restart/shutdown, let _do_system say the
        # one action line instead of speaking the reply too (avoids double TTS).
        pending = orchestrator.shutdown_requested() or orchestrator.restart_requested()
        if not pending:
            hud_state.set_state("speaking")
            tts.speak(reply)
    finally:
        state["busy"] = False
        hud_state.set_state("idle")
    # Tool-initiated restart/shutdown (e.g. the agent calls the tool itself).
    if orchestrator.shutdown_requested():
        _do_system("shutdown")
    if orchestrator.restart_requested():
        _do_system("restart")


def voice_loop():
    from pynput import keyboard  # imported here: fails cleanly on headless/Wayland

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
            tts.speak(f"{state['mode']} mode")

    keyboard.Listener(on_press=on_press, on_release=on_release).start()
    hud_state.set_state("idle")
    print(f"Cortana up. Mode: {state['mode']}. F9 hold = talk. F10 = cycle mode. Ctrl+C = quit.")
    while True:
        if state["busy"]:
            time.sleep(0.1)
            continue
        if state["mode"] == "ptt":
            if state["ptt"]:
                p = mic.record_while(lambda: state["ptt"])
                if p:
                    process(p)
            else:
                time.sleep(0.05)
        else:  # wake / open: continuous listen
            p = mic.listen_segment()
            if p and not state["busy"]:
                process(p)


def text_loop():
    print("Text mode. 'quit' to exit.")
    while True:
        try:
            t = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if t.lower() in ("quit", "exit"):
            break
        if t:
            cmd = _system_command(t)
            if cmd:
                _do_system(cmd)
            print("CORTANA:", orchestrator.handle(t))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", action="store_true", help="text mode (no mic/hotkeys)")
    args = ap.parse_args()
    memory.init()
    if args.text:
        text_loop()
    else:
        voice_loop()
