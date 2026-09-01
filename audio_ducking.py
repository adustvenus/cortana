"""Audio ducking: lower whatever is making noise while Cortana is being talked
to or is talking, restore it exactly when both go quiet.

Targets sink-inputs directly via pactl (PulseAudio/PipeWire) rather than any
player's API - it reacts the instant listening/speaking starts, with no network
round-trip and no dependency on the Dashboard being open or a token being valid.
That is also why it works for a browser tab, which has no API to ask.

It ducks, it does not pause: the track keeps playing and keeps its position, it
just gets quiet. Pausing and resuming would lose your place in a video and
fight whatever you were doing.

engage(reason)/release(reason) are reference-counted by reason string, so
overlapping triggers (e.g. she's still speaking when PTT is pressed again)
only restore volume once every reason has cleared.
"""
import re
import subprocess
import threading

from config import (DUCK_ENABLED, DUCK_SINK_MATCH, DUCK_SINK_EXCLUDE,
                    DUCK_FACTOR)

_INCLUDE = [s.strip().lower() for s in DUCK_SINK_MATCH.split(",") if s.strip()]
_EXCLUDE = [s.strip().lower() for s in DUCK_SINK_EXCLUDE.split(",") if s.strip()]

_TIMEOUT = 2
_lock = threading.Lock()
_active = set()
_saved = {}   # sink-input id -> volume% captured when ducking engaged


def _matching_inputs():
    try:
        out = subprocess.run(["pactl", "list", "sink-inputs"],
                              capture_output=True, text=True, timeout=_TIMEOUT).stdout
    except Exception:
        return []
    found = []
    for block in out.split("Sink Input #")[1:]:
        sid = block.split("\n", 1)[0].strip()
        if not sid.isdigit():
            continue
        low = block.lower()
        # Empty include list = duck everything. The exclude list is applied
        # either way, so narrowing the include list can never accidentally
        # re-enable ducking Cortana's own voice.
        if _INCLUDE and not any(m in low for m in _INCLUDE):
            continue
        if any(x in low for x in _EXCLUDE):
            continue
        m = re.search(r"Volume:.*?(\d+)%", block)
        if m:
            found.append((sid, int(m.group(1))))
    return found


def _set_volume(sid, pct):
    try:
        subprocess.run(["pactl", "set-sink-input-volume", sid, f"{pct}%"],
                        capture_output=True, timeout=_TIMEOUT)
    except Exception as e:
        print("[ducking] pactl failed:", e)


def engage(reason):
    """Start ducking for `reason`. Volume only drops on the first active
    reason - a second overlapping reason is a no-op."""
    if not DUCK_ENABLED:
        return
    with _lock:
        first = not _active
        _active.add(reason)
        if not first:
            return
        for sid, pct in _matching_inputs():
            _saved[sid] = pct
            _set_volume(sid, max(1, round(pct * DUCK_FACTOR)))


def release(reason):
    """Clear `reason`. Volume is restored to exactly what it was only once
    every reason has cleared."""
    if not DUCK_ENABLED:
        return
    with _lock:
        _active.discard(reason)
        if _active:
            return
        for sid, pct in _saved.items():
            _set_volume(sid, pct)
        _saved.clear()
