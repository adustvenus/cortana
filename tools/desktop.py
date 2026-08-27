"""Workstation control: one coarse tool for the desktop she lives on but
cannot operate.

ONE tool with an `action`, not eleven tools. The lead is a chief of staff with
a hard step limit and a cached tool prefix; eleven near-identical schemas would
crowd out the routing decisions that actually matter, and every one of them
would sit in the cached prefix being re-billed forever.

Everything here shells out to an X11 or PulseAudio binary, and on the runtime
box most of those binaries are NOT INSTALLED (wmctrl, xdotool, xclip and
playerctl were all absent as of 2026-08-26). So the contract of this module is
that a missing binary is a normal outcome, not an error: every action returns
one plain sentence naming the apt package. A traceback here would be read
aloud, which is the worst possible way to learn that xdotool is missing.

Two boundaries this module deliberately does not cross:

  * `volume` touches the MASTER SINK only, never sink-inputs. audio_ducking.py
    owns per-application volume for the spotifyd sink-input: it snapshots the
    level when the first duck engages and writes that snapshot back when the
    last reason clears. An absolute set-sink-input-volume from here would
    either be overwritten on release or, worse, be captured AS the snapshot and
    become the level the duck "restores" to. Different PulseAudio objects, on
    purpose - turning the room volume down while she is talking must not
    strand a duck.
  * `notify` is the raw desktop toast, for when the user asked for a toast.
    Proactive lines she decided to say herself go through notify.deliver(),
    which knows whether anyone is at the desk to see it.
"""
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import config

# Dashboard/app/sleep-screen.sh already solves DPMS-off plus the pointer-wake
# problem, and it takes a flock so a second call is a no-op. Reimplementing
# `xset dpms force off` here would relight the panel on a nudged mouse.
SLEEP_SCREEN = Path(__file__).resolve().parent.parent / "Dashboard" / "app" / "sleep-screen.sh"

# binary -> apt package. The sentence a user gets when something is missing is
# only useful if it names the thing they have to install, and half of these do
# not match their package name.
_APT = {
    "wmctrl": "wmctrl",
    "xdotool": "xdotool",
    "xclip": "xclip",
    "xsel": "xsel",
    "xdg-open": "xdg-utils",
    "xdg-screensaver": "xdg-utils",
    "gtk-launch": "libgtk-3-bin",
    "notify-send": "libnotify-bin",
    "pactl": "pulseaudio-utils",
    "loginctl": "systemd",
}

# Actions that talk to the X server. volume and notify are deliberately not in
# here: pactl reaches PulseAudio over its own socket and notify-send goes via
# D-Bus, and both work from a session with no DISPLAY at all.
#
# type_text is not here EITHER, and that is load-bearing rather than an
# oversight: it checks DISPLAY itself, but only after its policy gate. Listing
# it here would hoist the DISPLAY check above the gate, so a switched-off
# feature on the bridge (which sets no DISPLAY) would answer "there's no X
# display" - sending the user off to fix an X session that would still refuse
# to type. The true reason has to win.
_NEEDS_X11 = {"list_windows", "focus_window", "close_window", "launch",
              "open", "clipboard_get", "clipboard_set"}

_CLIP_SEL = ["-selection", "clipboard"]


def _cfg(name, default):
    """Read config lazily and tolerate the constant not being there yet.

    getattr rather than `from config import ...` because the safe value of
    DESKTOP_TYPE_ENABLED is False, and an ImportError on a box whose config.py
    predates this module would take out the whole tool instead of just the one
    dangerous action. Reading at call time also means a test can monkeypatch
    config and be believed.
    """
    return getattr(config, name, default)


def _timeout():
    # Everything in here is interactive-latency work. Anything still running
    # after a few seconds has hung on an X server that is not answering, and
    # the voice loop is blocked behind it the whole time.
    return float(_cfg("DESKTOP_TIMEOUT", 5.0))


def _run(argv, timeout=None, input_text=None):
    """(ok, text). Never raises - every caller turns failure into a sentence."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           input=input_text, timeout=timeout or _timeout())
    except subprocess.TimeoutExpired:
        return False, argv[0] + " stopped responding."
    except FileNotFoundError:
        return False, argv[0] + " is not installed."
    except Exception as e:
        return False, "{} failed: {}".format(argv[0], e)
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return False, (p.stderr or out).strip() or "{} exited {}.".format(argv[0], p.returncode)
    return True, out


def _spawn(argv):
    """Fire and forget, fully detached. (ok, text).

    Used for anything that outlives the turn - a launched app, or
    sleep-screen.sh, which polls until someone wakes the panel. subprocess.run
    would either kill it on timeout or hold the voice loop until the user comes
    back to the desk.
    """
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True, ""
    except FileNotFoundError:
        return False, argv[0] + " is not installed."
    except Exception as e:
        return False, "{} failed: {}".format(argv[0], e)


def _which(name):
    # Indirected so a test can blank the entire toolchain with one patch.
    return shutil.which(name)


def _missing(*names):
    """None if any of `names` is installed, else the sentence to return."""
    if any(_which(n) for n in names):
        return None
    pkgs = " ".join(sorted({_APT.get(n, n) for n in names}))
    if len(names) == 1:
        return ("I can't do that - " + names[0] + " isn't installed on this "
                "machine. Install it with sudo apt install " + pkgs + ".")
    return ("I can't do that - none of " + ", ".join(names) + " are installed. "
            "Install one with sudo apt install " + pkgs + ".")


def _no_display():
    """The sentence to return when there is no X display, else None.

    cortana.service sets DISPLAY and XAUTHORITY; cortana-bridge.service sets
    neither. Saying so is far better than letting an X11 tool hang on connect.
    """
    if os.environ.get("DISPLAY"):
        return None
    return ("There's no X display attached to me right now - DISPLAY is unset, "
            "so I can't reach the desktop at all.")


# ── windows ────────────────────────────────────────────────────────────────
def _windows():
    """[(id, title)] for visible windows, or a sentence when nothing can list."""
    gone = _missing("wmctrl", "xdotool")
    if gone:
        return gone
    if _which("wmctrl"):
        ok, out = _run(["wmctrl", "-l"])
        if not ok:
            return "wmctrl couldn't list the windows: " + out
        found = []
        for line in out.splitlines():
            parts = line.split(None, 3)            # id, desktop, host, title
            if len(parts) == 4:
                found.append((parts[0], parts[3].strip()))
        return found
    ok, out = _run(["xdotool", "search", "--onlyvisible", "--name", "."])
    if not ok:
        return "xdotool couldn't list the windows: " + out
    found = []
    # One shell-out per window on this path, so it is capped. A desktop with
    # more than forty visible windows does not need an exhaustive answer.
    for wid in out.split()[:40]:
        got, title = _run(["xdotool", "getwindowname", wid])
        if got and title:
            found.append((wid, title.strip()))
    return found


def _match(windows, needle):
    """The one window `needle` names, or a sentence saying why not.

    An exact window id wins; otherwise a case-insensitive substring of the
    title. An ambiguous match is refused rather than guessed, because closing
    the wrong window is not something an apology fixes.
    """
    needle = (needle or "").strip()
    if not needle:
        return "Which window? Give me part of its title, or an id from list_windows."
    for wid, title in windows:
        if wid == needle:
            return (wid, title)
    hits = [w for w in windows if needle.lower() in w[1].lower()]
    if not hits:
        return "No open window has " + needle + " in its title."
    if len(hits) > 1:
        names = ", ".join(t for _, t in hits[:5])
        return "That matches {} windows: {}. Be more specific.".format(len(hits), names)
    return hits[0]


def _list_windows(args):
    got = _windows()
    if isinstance(got, str):
        return got
    if not got:
        return "Nothing is open, or nothing that manages windows is running."
    # Capped for the same reason the xdotool path is: this string is pasted
    # into the turn's context. The cap is on the ANSWER, not on _windows(),
    # so a window past the fortieth is still findable by name.
    shown = got[:40]
    text = "\n".join(wid + "  " + title for wid, title in shown)
    if len(got) > len(shown):
        text += "\n... and {} more.".format(len(got) - len(shown))
    return text


def _window_action(args, wmctrl_flag, xdotool_cmd, verb, done):
    """`verb` for the failure sentence, `done` for the success one.

    Two words rather than one because she reads these out: "focus" + "d" is
    "Focusd", which is the kind of thing that gets noticed once and then
    remembered as the assistant sounding broken.
    """
    got = _windows()
    if isinstance(got, str):
        return got
    hit = _match(got, args.get("target") or args.get("window") or "")
    if isinstance(hit, str):
        return hit
    wid, title = hit
    if _which("wmctrl"):
        ok, out = _run(["wmctrl", "-i", wmctrl_flag, wid])
    else:
        ok, out = _run(["xdotool", xdotool_cmd, wid])
    if not ok:
        return "I couldn't {} {}: {}".format(verb, title, out)
    return "{} {}.".format(done, title)


# ── launching ──────────────────────────────────────────────────────────────
def _launch(args):
    app = (args.get("target") or args.get("app") or "").strip()
    if not app:
        return "Which application? Give me a command name or a .desktop id."
    # shlex, not str.split: nothing here goes through a shell, so a path with a
    # space is only expressible if quotes survive the split. Unbalanced quotes
    # are answered here rather than left to the catch-all in desktop(), which
    # would report them as an internal failure of the launch action.
    try:
        argv = shlex.split(app)
    except ValueError:
        return "That has an unclosed quote in it, so I can't tell where the arguments end."
    if not argv:
        return "Which application? Give me a command name or a .desktop id."
    binary = argv[0]
    # A .desktop entry carries the app's own Exec line, environment and
    # StartupWMClass; gtk-launch honours all of it, where guessing at the
    # binary name from the app's display name does not.
    if app.endswith(".desktop") or not _which(binary):
        if _which("gtk-launch"):
            ok, err = _spawn(["gtk-launch", app[:-8] if app.endswith(".desktop") else app])
            if not ok:
                return "I couldn't launch " + app + ": " + err
            return ("I asked the desktop to start " + app + ". If nothing "
                    "appears, there's no desktop entry by that name.")
        if not _which(binary):
            return ("There's no " + binary + " on this machine, and gtk-launch "
                    "isn't installed either, so I can't look up desktop entries. "
                    "Install it with sudo apt install libgtk-3-bin.")
    ok, err = _spawn(argv)
    if not ok:
        return "I couldn't launch " + app + ": " + err
    return "Started " + app + "."


def _open(args):
    target = (args.get("target") or args.get("path") or "").strip()
    if not target:
        return "Open what? Give me a file path or a URL."
    gone = _missing("xdg-open")
    if gone:
        return gone
    # Detached rather than run(): some handlers keep xdg-open alive for the
    # whole lifetime of the app they started, and run() would kill both of them
    # when the timeout expired.
    ok, err = _spawn(["xdg-open", target])
    if not ok:
        return "I couldn't open " + target + ": " + err
    return ("I handed " + target + " to the desktop's default handler. If "
            "nothing opens, nothing is registered for that type.")


# ── clipboard ──────────────────────────────────────────────────────────────
def _clip_tool():
    if _which("xclip"):
        return "xclip"
    if _which("xsel"):
        return "xsel"
    return None


def _clipboard_get(args):
    gone = _missing("xclip", "xsel")
    if gone:
        return gone
    argv = (["xclip"] + _CLIP_SEL + ["-o"] if _clip_tool() == "xclip"
            else ["xsel", "-b", "-o"])
    ok, out = _run(argv)
    if not ok:
        # xclip exits non-zero when the selection is simply empty or holds a
        # non-text target. That is an answer, not a fault.
        return "The clipboard is empty, or holds something that isn't text."
    if not out:
        return "The clipboard is empty."
    cap = int(_cfg("DESKTOP_CLIP_MAX", 4000))
    if len(out) > cap:
        return out[:cap] + "\n... (truncated, {} characters total)".format(len(out))
    return out


def _clipboard_set(args):
    text = args.get("text")
    if text is None:
        return "What should I put on the clipboard? Pass it as text."
    gone = _missing("xclip", "xsel")
    if gone:
        return gone
    tool = _clip_tool()
    argv = (["xclip"] + _CLIP_SEL + ["-i"] if tool == "xclip"
            else ["xsel", "-b", "-i"])
    # Both tools FORK and stay resident to serve the selection to whoever pastes
    # next. subprocess.run would hand that surviving child the write ends of the
    # stdout/stderr pipes and then block waiting for an EOF that never comes -
    # i.e. hang for the whole timeout on every SUCCESSFUL copy. Only stdin gets
    # a pipe here, and the rest goes to /dev/null.
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             text=True, start_new_session=True)
        p.communicate(input=str(text), timeout=_timeout())
    except subprocess.TimeoutExpired:
        return tool + " stopped responding, so the clipboard may not have changed."
    except Exception as e:
        return "I couldn't set the clipboard: {}".format(e)
    return "Copied {} characters to the clipboard.".format(len(str(text)))


# ── volume ─────────────────────────────────────────────────────────────────
def _sink_state():
    """(percent, muted) for the default sink, or (None, None).

    Parsed out of `pactl list sinks` rather than `pactl get-sink-volume`,
    because the get- subcommands are newer than the pactl shipped on some of
    these boxes and one parse answers both volume and mute in a single
    shell-out instead of two.
    """
    ok, out = _run(["pactl", "list", "sinks"])
    if not ok:
        return None, None
    got, default = _run(["pactl", "get-default-sink"])
    default = default.strip() if got else ""
    for block in out.split("Sink #")[1:]:
        name = re.search(r"^\s*Name:\s*(\S+)", block, re.M)
        if default and name and name.group(1) != default:
            continue
        vol = re.search(r"Volume:.*?(\d+)%", block)
        mute = re.search(r"Mute:\s*(yes|no)", block)
        return (int(vol.group(1)) if vol else None,
                (mute.group(1) == "yes") if mute else None)
    return None, None


_MUTE_WORDS = {"on": "1", "yes": "1", "true": "1", "mute": "1",
               "off": "0", "no": "0", "false": "0", "unmute": "0",
               "toggle": "toggle"}


def _volume(args):
    gone = _missing("pactl")
    if gone:
        return gone
    level = args.get("level")
    if level is None:
        level = args.get("value")
    mute = args.get("mute")

    # `mute is not None`, NOT `if mute:` - a JSON false is the single most
    # likely way a model spells "unmute", and truthiness silently swallowed it
    # into the read-the-volume branch: "unmute the speakers" answered "volume
    # is at 45 percent, and muted" and changed nothing. str(False) is "false",
    # which _MUTE_WORDS already knows.
    if mute is not None and str(mute).strip():
        want = _MUTE_WORDS.get(str(mute).strip().lower())
        if want is None:
            return "For mute I take on, off, or toggle."
        ok, out = _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", want])
        if not ok:
            return "pactl wouldn't change the mute: " + out
        _, muted = _sink_state()
        if muted is None:
            return "Done."
        return "Sound is muted." if muted else "Sound is on."

    if level is None:
        pct, muted = _sink_state()
        if pct is None:
            return "I couldn't read the volume - pactl didn't report a default sink."
        return "Volume is at {} percent{}".format(pct, ", and muted." if muted else ".")

    text = str(level).strip()
    relative = text[:1] in ("+", "-")
    try:
        num = int(float(text))
    except ValueError:
        return "Give me a number from 0 to 100, or a relative step like +10."
    current, muted = _sink_state()
    if relative:
        if current is None:
            return "I couldn't read the current volume, so I won't guess at a step."
        num = current + num
    num = max(0, min(100, num))
    # @DEFAULT_SINK@, never a sink-input: see the module docstring. audio_ducking
    # owns sink-inputs and restores them from a snapshot it took itself.
    ok, out = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "{}%".format(num)])
    if not ok:
        return "pactl wouldn't set the volume: " + out
    # Setting a level on a MUTED sink is silence with a confident sentence
    # attached, and the user asked to HEAR something. tools/media.py's volume
    # verb already unmutes on an explicit level; matching it keeps the two
    # tools from disagreeing about what "turn it up" means. Unknown mute state
    # (muted is None) unmutes too - the failure mode of a needless unmute is
    # nothing, the failure mode of skipping it is a silent room.
    if muted is not False:
        cleared = _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])[0]
        if muted and cleared:
            return "Volume set to {} percent, and unmuted.".format(num)
    return "Volume set to {} percent.".format(num)


# ── notify / lock / type ───────────────────────────────────────────────────
def _notify(args):
    body = str(args.get("text") or args.get("body") or "").strip()
    # Defaulted AFTER the emptiness check, not before: with the default applied
    # first the check could never fire, so `notify` with no arguments posted a
    # blank toast titled "Cortana" and reported success.
    title = str(args.get("title") or "").strip()
    if not body and not title:
        return "What should the notification say?"
    title = title or "Cortana"
    gone = _missing("notify-send")
    if gone:
        return gone
    urgency = str(args.get("urgency") or "normal").lower()
    if urgency not in ("low", "normal", "critical"):
        urgency = "normal"
    ok, out = _run(["notify-send", "-a", "Cortana", "-u", urgency, title, body])
    if not ok:
        return "notify-send failed: " + out
    return "Put it on screen."


def _lock_screen(args):
    """Lock the session, then blank the panel with the dashboard's own script.

    Locking and blanking are separate things and both are wanted: loginctl
    lock-session tells the session manager to demand a password, while
    sleep-screen.sh turns the display off without letting a nudged mouse
    relight it. Reporting them separately matters - "the screen went dark" is
    not the same promise as "the machine is locked".
    """
    locked = False
    if _which("loginctl"):
        locked = _run(["loginctl", "lock-session"])[0]
    if not locked and _which("xdg-screensaver"):
        locked = _run(["xdg-screensaver", "lock"])[0]

    blanked = False
    # The blanking half IS gated on DISPLAY and on xset, even though the
    # locking half above deliberately is not. sleep-screen.sh ends with
    # `while sleep 1; do xset q | grep -q "Monitor is On" && break; done`, and
    # it forces DISPLAY=:0 for itself - so with no X server, or no xset, that
    # query can never succeed and the loop never terminates. Spawned detached
    # from us, it would then outlive our restarts as an immortal two-processes-
    # a-second wakeup on a laptop battery. cortana-bridge.service sets no
    # DISPLAY, so this was one lock_screen call away on a live surface.
    if not _no_display() and _which("xset") and SLEEP_SCREEN.exists() and _which("bash"):
        # It polls until someone wakes the panel, so it MUST be detached; it
        # also takes a flock, so a second call while already dark is a harmless
        # no-op. Invoked through bash rather than executed directly, because the
        # execute bit is one careless checkout away from being gone.
        blanked = _spawn(["bash", str(SLEEP_SCREEN)])[0]

    if locked:
        return "Locked the session" + (" and turned the screen off." if blanked else ".")
    if blanked:
        return ("I turned the screen off, but I couldn't lock the session - "
                "neither loginctl nor xdg-screensaver would take it. The machine "
                "is still unlocked.")
    return ("I can't lock this session - loginctl and xdg-screensaver both "
            "refused, and I couldn't blank the screen either.")


def _type_text(args):
    """Synthesise keystrokes into whatever window has focus.

    The only primitive in this module that can destroy something. Keystrokes go
    wherever focus happens to be - a terminal, a chat box, a sudo prompt - with
    no undo and no confirmation step, so it is off unless someone deliberately
    turned it on. The policy check comes BEFORE the binary check on purpose:
    "that's switched off" is the true answer and must not be masked by
    "xdotool isn't installed", which would read as though installing xdotool
    were all it took.
    """
    if not _cfg("DESKTOP_TYPE_ENABLED", False):
        return ("Typing into the focused window is switched off. It sends "
                "keystrokes wherever focus happens to be, including a terminal "
                "or a password prompt, so it stays off until you set "
                "DESKTOP_TYPE_ENABLED=1 in .env.local and restart me.")
    text = args.get("text")
    if not text:
        return "Type what?"
    text = str(text)
    cap = int(_cfg("DESKTOP_TYPE_MAX", 2000))
    if len(text) > cap:
        return "That's {} characters. I won't type more than {} blind.".format(len(text), cap)
    blocked = _no_display()
    if blocked:
        return blocked
    gone = _missing("xdotool")
    if gone:
        return gone
    # --clearmodifiers so a still-held modifier from the last shortcut doesn't
    # turn the whole string into shortcuts; -- so a leading dash is text, not a
    # flag. The timeout scales with length because --delay 12 is per keystroke.
    # ... and the ceiling is capped: at the cap of 2000 characters the length
    # term alone allows 102 seconds, and this runs in the turn's thread, so a
    # wedged X server would hold the voice loop for the better part of two
    # minutes. 12ms a keystroke means 2000 characters really take ~24s.
    ok, out = _run(["xdotool", "type", "--clearmodifiers", "--delay", "12", "--", text],
                   timeout=min(45.0, max(_timeout(), len(text) * 0.05 + 2)))
    if not ok:
        return "xdotool couldn't type that: " + out
    return "Typed {} characters into the focused window.".format(len(text))


_ACTIONS = {
    "list_windows": _list_windows,
    "focus_window": lambda a: _window_action(a, "-a", "windowactivate", "focus", "Focused"),
    "close_window": lambda a: _window_action(a, "-c", "windowclose", "close", "Closed"),
    "launch": _launch,
    "open": _open,
    "clipboard_get": _clipboard_get,
    "clipboard_set": _clipboard_set,
    "volume": _volume,
    "notify": _notify,
    "lock_screen": _lock_screen,
    "type_text": _type_text,
}


def desktop(args):
    """The whole tool. Always returns a string; never raises."""
    args = args or {}
    action = str(args.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if fn is None:
        return ("I don't have a desktop action called " + (action or "(nothing)")
                + ". I can do: " + ", ".join(_ACTIONS) + ".")
    if action in _NEEDS_X11:
        blocked = _no_display()
        if blocked:
            return blocked
    try:
        return fn(args)
    except Exception as e:
        # A traceback out of here gets read aloud. One sentence instead - but
        # printed as well as spoken, the way notify.py logs a failed leg. A
        # sentence she said once and nobody wrote down is not a bug report.
        print("[desktop] {} failed:".format(action), repr(e))
        return "The {} action failed: {}".format(action, e)
