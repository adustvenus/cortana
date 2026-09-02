"""Desk presence: is the user actually at this machine?

Written ONLY by the cortana process and read by everyone else. That is not a
style choice - cortana.service sets DISPLAY and XAUTHORITY, cortana-bridge.service
sets neither, so an X11 idle probe from the bridge returns nothing and would
read as "away". One writer per state file is the same invariant hud_state.json
and calendar_state.json already depend on.

Two safety properties the rest of this file exists to guarantee:

  * A stale file reads as `unknown`, never as its last value. Serving a
    six-hour-old "present" would aim an alarm at a speaker nobody is near.
  * The degraded path (no idle probe available) can never report `asleep`.
    Guessing "asleep" and silencing an alarm is the worst error here, so it is
    made structurally unreachable rather than merely unlikely.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from config import AWAY_IDLE, PRESENCE_STALE, PRESENT_IDLE

STATE_FILE = Path(__file__).resolve().parent / "presence_desk.json"

_last_voice = {"ts": 0.0}
_last_written = None
_last_write_ts = 0.0
# Which idle probe won last time. The fallback chain is only walked once; after
# that we go straight to the tool that worked, so a 30s loop is not shelling
# out to three missing binaries forever.
_idle_probe = {"cmd": None, "resolved": False}


def note_voice():
    """Stamp real user activity. Called from main.py whenever an utterance is
    accepted or push-to-talk is pressed - this is the ONLY presence signal that
    survives when no X11 idle probe is installed."""
    _last_voice["ts"] = time.time()


def _run(args, timeout=3):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _idle_seconds():
    """Seconds since the last input event, or None if nothing can tell us.

    None is a real answer, distinct from 0. It means "unmeasured", and the
    caller must not turn it into "asleep".
    """
    probes = (
        ("xprintidle", lambda: _run(["xprintidle"]), 1000.0),
        ("xssstate", lambda: _run(["xssstate", "-i"]), 1000.0),
    )
    if _idle_probe["resolved"] and _idle_probe["cmd"] is None:
        return _loginctl_idle()

    for name, call, divisor in probes:
        if _idle_probe["resolved"] and _idle_probe["cmd"] != name:
            continue
        if not shutil.which(name):
            continue
        raw = call()
        if raw.isdigit():
            _idle_probe.update(cmd=name, resolved=True)
            return int(raw) / divisor
    _idle_probe.update(cmd=None, resolved=True)
    return _loginctl_idle()


def _loginctl_idle():
    sid = os.getenv("XDG_SESSION_ID", "")
    if not sid or not shutil.which("loginctl"):
        return None
    raw = _run(["loginctl", "show-session", sid, "-p", "IdleSinceHint"])
    _, _, val = raw.partition("=")
    if val.strip().isdigit() and int(val) > 0:
        return max(0.0, time.time() - int(val) / 1_000_000.0)
    return None


def _screen_on():
    """True/False, or None if DPMS can't be read. The dashboard's own auto-sleep
    already drives DPMS off, so this reads a signal the system is generating
    rather than inventing a new one."""
    if not shutil.which("xset"):
        return None
    out = _run(["xset", "-q"])
    if "Monitor is Off" in out:
        return False
    if "Monitor is On" in out:
        return True
    return None


def classify(idle_sec, screen, last_voice, now=None):
    """The whole desk policy, as a pure function so the tests can pin it."""
    now = now or time.time()
    if screen is False:
        return "asleep"
    if idle_sec is None:
        # Degraded: no idle probe installed. Recent speech is the only evidence
        # we have, and its absence is NOT evidence of sleep - so this branch
        # deliberately cannot return "asleep".
        if last_voice and (now - last_voice) < 900:
            return "present"
        return "away"
    if idle_sec < PRESENT_IDLE:
        return "present"
    if idle_sec >= AWAY_IDLE:
        return "asleep"
    return "away"


def sample_desk():
    idle = _idle_seconds()
    screen = _screen_on()
    return {"idle_sec": idle, "screen": screen,
            "last_voice": _last_voice["ts"],
            "probe": _idle_probe["cmd"] or "voice-only",
            "state": classify(idle, screen, _last_voice["ts"])}


def publish():
    """Write presence_desk.json, skipping the disk write when nothing changed -
    same discipline as hud_state.py, and it matters more here because this runs
    on a 30s loop forever."""
    global _last_written, _last_write_ts
    payload = sample_desk()
    compare = dict(payload)
    compare.pop("last_voice", None)      # moves constantly; not worth a write
    # Skip the write only while the file is still comfortably fresh. The guard
    # compares the PAYLOAD, but read_desk() trusts the file by its TIMESTAMP -
    # so a genuinely steady desk state stopped being written, aged past
    # PRESENCE_STALE and read back as "unknown" while the user sat right there.
    # That is this repo's signature bug: a guard measuring one thing while the
    # consumer checks another.
    if compare == _last_written and (time.time() - _last_write_ts) < PRESENCE_STALE / 2:
        return payload
    _last_written, _last_write_ts = compare, time.time()
    tmp = tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent,
                                      delete=False, suffix=".tmp")
    json.dump(dict(payload, ts=time.time()), tmp)
    tmp.close()
    os.replace(tmp.name, STATE_FILE)
    return payload


def read_desk(now=None):
    """Merged desk view for whoever is routing a delivery.

    Cortana being down IS information - she was told to shut down, which usually
    means the user left - but it is not information about which room they are
    in, so it degrades to `unknown` rather than to a guess.
    """
    now = now or time.time()
    try:
        d = json.loads(STATE_FILE.read_text())
    except Exception:
        return "unknown"
    if now - float(d.get("ts") or 0) > PRESENCE_STALE:
        return "unknown"
    state = d.get("state")
    return state if state in ("present", "away", "asleep") else "unknown"
