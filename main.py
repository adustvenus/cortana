"""Cortana entrypoint.

Voice mode (default):
  F9 (hold)  = push-to-talk
  F10        = cycle mode: ptt -> wake ("ok cortana ...") -> open (just talk)
Text mode (debug / hotkey-less fallback):
  python main.py --text

System keywords (spoken, matched before LLM):
  "okay cortana, time for a restart"  -> restarts the cortana systemd service
  "okay cortana, time to shut down"   -> stops the cortana systemd service
"""
import argparse
import os
import re
import time

import config
import memory
import orchestrator
from voice import mic, stt, tts, wake

state = {"mode": config.MODE, "ptt": False, "busy": False}

# --- System command patterns ---
_RESTART_PATTERN = re.compile(
    r"(ok(ay)?[\s,]+cortana[\s,]+)?time\s+for\s+a\s+restart", re.I
)
_SHUTDOWN_PATTERN = re.compile(
    r"(ok(ay)?[\s,]+cortana[\s,]+)?time\s+to\s+shut\s+down", re.I
)


def _handle_system_commands(text):
    """Check for restart/shutdown keywords. Returns True if a system command was matched."""
    if _RESTART_PATTERN.search(text):
        tts.speak("Restarting now. Back in a moment.")
        time.sleep(1.5)
        os.system("systemctl --user restart cortana")
        return True
    if _SHUTDOWN_PATTERN.search(text):
        tts.speak("Shutting down. Goodbye.")
        time.sleep(1.5)
        os.system("systemctl --user stop cortana")
        return True
    return False


def process(wav_path):
    text = stt.transcribe(wav_path)
    if not text:
        return
    mode = state["mode"]
    if mode == "wake":
        stripped = wake.wake_match(text)
        if stripped is None:
            return
        text = stripped
    elif mode == "open":
        stripped = wake.wake_match(text)
        if stripped is not None:
            text = stripped
        elif not wake.addressed(text, memory.recent_text(6)):
            print(f"(ignored, not addressed to me: {text[:60]})")
            return
    print("YOU:", text)

    # Check for system-level commands before hitting the LLM
    if _handle_system_commands(text):
        return

    state["busy"] = True
    try:
        reply = orchestrator.handle(text)
        print("CORTANA:", reply)
        tts.speak(reply)
    finally:
        state["busy"] = False


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
            # Also handle system commands in text mode
            if not _handle_system_commands(t):
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
