"""Workstation-control tests.

The failures this module can have are all of the "it was fine on the dev box"
kind. Four of them are worth a test each:

  * On the runtime box wmctrl, xdotool and xclip are NOT installed. Every one
    of those actions has to come back as a sentence a person can act on. A
    FileNotFoundError here is read out loud in her voice.
  * type_text puts keystrokes into whatever has focus. If the gate ever fails
    open - or fails in the wrong ORDER, so that installing xdotool silently
    switches it on - the first symptom is text typed into a sudo prompt.
  * audio_ducking snapshots the spotifyd sink-input volume and restores it from
    that snapshot. A volume action that wrote to sink-inputs would either be
    reverted on release or become the value the duck restores TO. Nothing about
    that is visible until Spotify stays quiet after she stops talking.
  * Two of the binaries here outlive the call - xclip forks to serve the
    selection, sleep-screen.sh polls until the panel wakes. Waiting on either
    hangs the voice loop, and it hangs it on SUCCESS, which is why review does
    not catch it.
"""
import subprocess

import pytest

import audio_ducking
import config
from tools import desktop


# Every action, with the minimum arguments that get it past its own argument
# check and down to the part that needs a binary.
ALL_ACTIONS = [
    ("list_windows", {}),
    ("focus_window", {"target": "Firefox"}),
    ("close_window", {"target": "Firefox"}),
    ("launch", {"target": "firefox"}),
    ("open", {"target": "https://example.com"}),
    ("clipboard_get", {}),
    ("clipboard_set", {"text": "hello"}),
    ("volume", {}),
    ("notify", {"text": "hi"}),
    ("lock_screen", {}),
    ("type_text", {"text": "hello"}),
]


@pytest.fixture
def display(monkeypatch):
    """An X session, so the DISPLAY guard is not what these tests are measuring."""
    monkeypatch.setenv("DISPLAY", ":0")


@pytest.fixture
def bare(monkeypatch, display):
    """No binaries at all - the runtime box's actual state for most of these."""
    monkeypatch.setattr(desktop, "_which", lambda name: None)


class Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


# ── degradation ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("action,args", ALL_ACTIONS)
def test_every_action_degrades_to_a_sentence_with_no_binaries(action, args, bare, monkeypatch):
    """The box she runs on is missing wmctrl, xdotool and xclip right now.

    Each of those must come back as prose, not an exception and not an empty
    string - the result of this call goes into a spoken turn verbatim.
    """
    # If anything reached a real binary this would be the crash, not the answer.
    def explode(*a, **kw):
        raise AssertionError("shelled out with no binary present: " + repr(a))
    monkeypatch.setattr(desktop.subprocess, "run", explode)
    monkeypatch.setattr(desktop.subprocess, "Popen", explode)

    out = desktop.desktop(dict(args, action=action))
    assert isinstance(out, str) and out.strip(), action
    assert "Traceback" not in out
    assert out.rstrip().endswith((".", "?")), out       # a sentence, not a code


@pytest.mark.parametrize("action,args,package", [
    ("list_windows", {}, "wmctrl"),
    ("focus_window", {"target": "x"}, "wmctrl"),
    ("close_window", {"target": "x"}, "wmctrl"),
    ("open", {"target": "/tmp/x"}, "xdg-utils"),
    ("clipboard_get", {}, "xclip"),
    ("clipboard_set", {"text": "x"}, "xclip"),
    ("volume", {}, "pulseaudio-utils"),
    ("notify", {"text": "x"}, "libnotify-bin"),
])
def test_missing_binary_names_the_apt_package(action, args, package, bare):
    """"I can't do that" is useless on its own. The sentence has to name the
    package, because half of these binaries do not match their package name
    (notify-send is libnotify-bin, xdg-open is xdg-utils, pactl is
    pulseaudio-utils) and the user cannot guess them."""
    out = desktop.desktop(dict(args, action=action))
    assert "apt install" in out, out
    assert package in out, out


def test_unknown_action_lists_the_real_ones(bare):
    """One tool with eleven actions means the model will invent a twelfth. It
    should get the menu back, not a shrug."""
    out = desktop.desktop({"action": "play_music"})
    assert "play_music" in out
    assert "clipboard_get" in out and "lock_screen" in out


# ── DISPLAY ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("action", ["list_windows", "clipboard_get", "focus_window"])
def test_x11_actions_say_so_instead_of_hanging_without_a_display(action, monkeypatch):
    """cortana-bridge.service sets no DISPLAY. An X11 client with no display
    blocks on connect, and a blocked voice turn looks identical to a crash."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda *a, **kw: pytest.fail("connected to X with no DISPLAY"))
    out = desktop.desktop({"action": action, "text": "x", "target": "x"})
    assert "DISPLAY" in out


def test_type_text_checks_the_display_too_but_only_after_the_gate(monkeypatch):
    """type_text needs X like the rest, but it must not be in _NEEDS_X11.

    The dispatcher's DISPLAY check runs before the action does, so listing
    type_text there hoists it above the policy gate: on the bridge, which sets
    no DISPLAY, a switched-OFF feature answered "there's no X display" and sent
    the user to fix an X session that would still have refused to type. Both
    sentences have to be reachable, in this order.
    """
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda *a, **kw: pytest.fail("connected to X with no DISPLAY"))

    monkeypatch.delattr(config, "DESKTOP_TYPE_ENABLED", raising=False)
    off = desktop.desktop({"action": "type_text", "text": "x"})
    assert "switched off" in off, "the DISPLAY check masked the real reason"

    monkeypatch.setattr(config, "DESKTOP_TYPE_ENABLED", True, raising=False)
    on = desktop.desktop({"action": "type_text", "text": "x"})
    assert "DISPLAY" in on, "typed, or tried to, with no display to type into"


def test_volume_and_notify_still_work_without_a_display(monkeypatch):
    """pactl has its own socket and notify-send goes over D-Bus. Gating them on
    DISPLAY would make her deaf and mute on a headless session for no reason."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    calls = []

    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl(calls))
    desktop.desktop({"action": "notify", "text": "hi"})
    assert calls and calls[0][0] == "notify-send"
    # and the half this test is named after, which it used not to exercise
    calls.clear()
    assert "45 percent" in desktop.desktop({"action": "volume"})


# ── type_text, the dangerous one ───────────────────────────────────────────
def test_type_text_refuses_while_the_flag_is_off_even_with_xdotool_present(monkeypatch, display):
    """The gate is policy, not tooling. Installing xdotool must not turn typing
    on as a side effect - so the flag is checked FIRST and nothing is spawned."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.delattr(config, "DESKTOP_TYPE_ENABLED", raising=False)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda *a, **kw: pytest.fail("typed while the flag was off"))
    out = desktop.desktop({"action": "type_text", "text": "rm -rf /"})
    assert "DESKTOP_TYPE_ENABLED" in out, out
    assert "xdotool" not in out, "a missing-binary excuse would hide the real reason"


def test_type_text_defaults_to_off_when_config_has_no_such_setting(monkeypatch, display):
    """This module ships before config.py grows the constant. getattr's default
    decides what happens on that box, and it must be OFF."""
    monkeypatch.delattr(config, "DESKTOP_TYPE_ENABLED", raising=False)
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    assert not hasattr(config, "DESKTOP_TYPE_ENABLED")
    assert "switched off" in desktop.desktop({"action": "type_text", "text": "x"})


def test_type_text_types_once_enabled(monkeypatch, display):
    """The gate has to be openable, or the test above is proving nothing."""
    monkeypatch.setattr(config, "DESKTOP_TYPE_ENABLED", True, raising=False)
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    seen = []

    def fake(argv, **kw):
        seen.append(argv)
        return Proc("")
    monkeypatch.setattr(desktop.subprocess, "run", fake)
    out = desktop.desktop({"action": "type_text", "text": "hello"})
    assert seen[0][:2] == ["xdotool", "type"]
    assert seen[0][-2:] == ["--", "hello"]      # -- or a leading dash becomes a flag
    assert "5 characters" in out


def test_type_text_enabled_but_xdotool_missing_is_still_a_sentence(monkeypatch, display):
    monkeypatch.setattr(config, "DESKTOP_TYPE_ENABLED", True, raising=False)
    monkeypatch.setattr(desktop, "_which", lambda name: None)
    out = desktop.desktop({"action": "type_text", "text": "hello"})
    assert "xdotool" in out and "apt install" in out


# ── volume vs. audio ducking ───────────────────────────────────────────────
SINKS = """Sink #0
	Name: alsa_output.pci-0000_04_00.6.HiFi__hw_acp__sink
	Mute: no
	Volume: front-left: 29491 /  45% / -18.06 dB,   front-right: 29491 /  45%
Sink #1
	Name: alsa_output.usb-Some_Headset
	Mute: yes
	Volume: front-left: 65536 / 100% / 0.00 dB
"""

SPOTIFYD_INPUT = """Sink Input #42
    Sink: 0
    Volume: front-left: 45875 /  70% / -9.35 dB
    Properties:
        application.name = "spotifyd"
"""


def _desktop_pactl(recorder, muted=False):
    """A pactl stand-in for tools.desktop, recording every argv it is given.

    `muted` flips the DEFAULT sink (#0) to Mute: yes - the second sink is
    already muted, so a parser that reads the wrong block cannot pass both this
    and test_volume_reads_the_default_sink_not_whichever_comes_first.
    """
    sinks = SINKS.replace("Mute: no", "Mute: yes", 1) if muted else SINKS

    def fake(argv, **kw):
        recorder.append(argv)
        if argv[:3] == ["pactl", "list", "sinks"]:
            return Proc(sinks)
        if argv[:2] == ["pactl", "get-default-sink"]:
            return Proc("alsa_output.pci-0000_04_00.6.HiFi__hw_acp__sink\n")
        return Proc("")
    return fake


def _ducking_pactl(argv, **kw):
    if argv[:3] == ["pactl", "list", "sink-inputs"]:
        return Proc(SPOTIFYD_INPUT)
    return Proc("")


@pytest.fixture
def clean_duck():
    audio_ducking._active.clear()
    audio_ducking._saved.clear()
    yield
    audio_ducking._active.clear()
    audio_ducking._saved.clear()


def test_setting_the_volume_does_not_strand_an_active_duck(monkeypatch, clean_duck, display):
    """She is speaking (so spotifyd is ducked to 18%) and is told to turn the
    volume down. If that write landed on the sink-input, release() would put
    Spotify back to whatever this action left behind - or capture it as the new
    "original" - and the duck would never unwind correctly.

    The invariant: nothing in tools.desktop ever writes a sink-INPUT volume.
    """
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(audio_ducking, "DUCK_ENABLED", True)
    monkeypatch.setattr(audio_ducking, "DUCK_FACTOR", 0.25)
    monkeypatch.setattr(audio_ducking.subprocess, "run", _ducking_pactl)

    audio_ducking.engage("speaking")
    assert audio_ducking._saved == {"42": 70}, "fixture broken: nothing was ducked"

    seen = []
    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl(seen))
    out = desktop.desktop({"action": "volume", "level": 30})

    assert "30 percent" in out
    assert not any("set-sink-input-volume" in a for argv in seen for a in argv), seen
    assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "30%"] in seen
    assert audio_ducking._saved == {"42": 70}, "the duck's snapshot was disturbed"

    restores = []
    monkeypatch.setattr(audio_ducking.subprocess, "run",
                        lambda argv, **kw: (restores.append(argv), Proc(""))[1])
    audio_ducking.release("speaking")
    assert restores[-1] == ["pactl", "set-sink-input-volume", "42", "70%"]


def test_volume_reads_the_default_sink_not_whichever_comes_first(monkeypatch, display):
    """Two sinks at 45% and 100%, and the default is the quiet one. Reporting
    the first block parsed would tell the user 100 while the room is at 45."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl([]))
    assert "45 percent" in desktop.desktop({"action": "volume"})


def test_relative_volume_steps_from_the_measured_level(monkeypatch, display):
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    seen = []
    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl(seen))
    desktop.desktop({"action": "volume", "level": "+10"})
    assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "55%"] in seen


@pytest.mark.parametrize("spelling", [False, "false", "off", "no", "unmute"])
def test_unmute_survives_every_spelling_including_a_json_false(spelling, monkeypatch, display):
    """"Unmute" reaches this as mute=false at least as often as mute="off",
    and a JSON false is falsy. Truthiness swallowed it into the read-the-level
    branch, so the answer was "volume is at 45 percent, and muted" and nothing
    changed - a report where an action was asked for."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    seen = []
    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl(seen))
    desktop.desktop({"action": "volume", "mute": spelling})
    assert ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"] in seen, seen


def test_setting_a_level_on_a_muted_sink_also_unmutes_it(monkeypatch, display):
    """The default sink in SINKS is muted. "Turn it up to 30" that leaves the
    mute in place is silence plus a confident sentence, and the user asked to
    HEAR something. tools/media.py's volume verb already unmutes on an explicit
    level; two tools disagreeing about that is worse than either rule."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    seen = []
    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl(seen, muted=True))
    out = desktop.desktop({"action": "volume", "level": 30})
    assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "30%"] in seen
    assert ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"] in seen, seen
    assert "unmuted" in out, out
    # and it still never touches a sink-input, muted or not
    assert not any("set-sink-input-volume" in a for argv in seen for a in argv)


def test_an_unmuted_sink_is_not_pointlessly_unmuted(monkeypatch, display):
    """The mirror of the test above: the extra pactl call is for the muted
    case only, so a plain volume set stays one shell-out on the write side."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    seen = []
    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl(seen))
    out = desktop.desktop({"action": "volume", "level": 30})
    assert not any("set-sink-mute" in a for argv in seen for a in argv), seen
    assert "unmuted" not in out


def test_volume_is_clamped_to_a_sane_range(monkeypatch, display):
    """A model that hears "way up" will happily send 400, and PulseAudio will
    happily accept it and blow the speakers."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    seen = []
    monkeypatch.setattr(desktop.subprocess, "run", _desktop_pactl(seen))
    desktop.desktop({"action": "volume", "level": 400})
    assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"] in seen


# ── the two calls that must not be waited on ───────────────────────────────
def test_clipboard_set_never_waits_on_the_forked_selection_owner(monkeypatch, display):
    """xclip forks and stays resident to serve the selection. Give that child
    the write end of a captured stdout pipe and the parent blocks on an EOF
    that never arrives - hanging the voice loop for the full timeout on every
    SUCCESSFUL copy."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda *a, **kw: pytest.fail("used run(); it will block on the fork"))
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"], captured["kw"] = argv, kw

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            return "", ""
    monkeypatch.setattr(desktop.subprocess, "Popen", FakePopen)

    out = desktop.desktop({"action": "clipboard_set", "text": "some text"})
    assert captured["kw"]["stdout"] is subprocess.DEVNULL
    assert captured["kw"]["stderr"] is subprocess.DEVNULL
    assert captured["kw"]["stdin"] is subprocess.PIPE
    assert captured["input"] == "some text"
    assert "9 characters" in out


def test_lock_screen_reuses_sleep_screen_and_detaches_from_it(monkeypatch, display):
    """sleep-screen.sh polls until someone wakes the panel, so running it in the
    foreground would block until the user comes back to the desk. It also owns
    the pointer-disable dance that stops a nudged mouse relighting the screen,
    which is exactly why lock_screen must call it instead of shelling xset."""
    assert desktop.SLEEP_SCREEN.exists(), \
        "fixture broken: Dashboard/app/sleep-screen.sh moved, so this proves nothing"
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    ran, spawned = [], []
    monkeypatch.setattr(desktop, "_run", lambda argv, **kw: (ran.append(argv), (True, ""))[1])
    monkeypatch.setattr(desktop, "_spawn", lambda argv: (spawned.append(argv), (True, ""))[1])

    out = desktop.desktop({"action": "lock_screen"})
    assert ["loginctl", "lock-session"] in ran
    assert any("sleep-screen.sh" in a for argv in spawned for a in argv), spawned
    assert not any("sleep-screen.sh" in a for argv in ran for a in argv), \
        "waited on the blanking script; the turn would never end"
    assert "Locked the session" in out


def test_lock_screen_does_not_claim_a_lock_it_could_not_take(monkeypatch, display):
    """A dark screen is not a locked machine. Saying "locked" when only the
    blanking worked is the one lie here with a security consequence."""
    monkeypatch.setattr(desktop, "_which",
                        lambda name: "/usr/bin/" + name if name in ("bash", "xset") else None)
    monkeypatch.setattr(desktop, "_spawn", lambda argv: (True, ""))
    out = desktop.desktop({"action": "lock_screen"})
    assert "still unlocked" in out


@pytest.mark.parametrize("absent", ["DISPLAY", "xset"])
def test_lock_screen_will_not_start_a_blanking_loop_that_cannot_end(absent, monkeypatch):
    """sleep-screen.sh ends with `while sleep 1; do xset q | grep -q "Monitor
    is On" && break; done`, and forces DISPLAY=:0 for itself.

    With no X server, or no xset, that query can never succeed - so the loop
    never breaks. It is spawned detached and in a new session, so it survives
    every restart of ours: two processes a second, forever, on a laptop
    battery. cortana-bridge.service sets no DISPLAY, and lock_screen is
    deliberately not gated on DISPLAY (loginctl works without one), so this was
    exactly one tool call away.
    """
    monkeypatch.setenv("DISPLAY", ":0")
    if absent == "DISPLAY":
        monkeypatch.delenv("DISPLAY")
        have = {"bash", "xset", "loginctl"}
    else:
        have = {"bash", "loginctl"}
    monkeypatch.setattr(desktop, "_which",
                        lambda name: "/usr/bin/" + name if name in have else None)
    monkeypatch.setattr(desktop, "_run", lambda argv, **kw: (True, ""))
    spawned = []
    monkeypatch.setattr(desktop, "_spawn", lambda argv: (spawned.append(argv), (True, ""))[1])

    out = desktop.desktop({"action": "lock_screen"})
    assert not spawned, "spawned an immortal poll loop: " + repr(spawned)
    assert "Locked the session." == out, out       # and does not claim a dark screen


# ── window matching ────────────────────────────────────────────────────────
WMCTRL_LIST = """0x03000007  0 box Firefox - Cortana build
0x04a00003  0 box Terminal
0x04a00009  0 box Firefox - news
"""


@pytest.fixture
def wmctrl(monkeypatch, display):
    monkeypatch.setattr(desktop, "_which",
                        lambda name: "/usr/bin/wmctrl" if name == "wmctrl" else None)
    seen = []

    def fake(argv, **kw):
        seen.append(argv)
        return Proc(WMCTRL_LIST if argv[:2] == ["wmctrl", "-l"] else "")
    monkeypatch.setattr(desktop.subprocess, "run", fake)
    return seen


def test_an_ambiguous_close_target_is_refused_not_guessed(wmctrl):
    """Two Firefox windows. Picking one and closing it loses whatever was in
    the other - there is no undo for a closed window, so a question is cheaper
    than a coin flip."""
    out = desktop.desktop({"action": "close_window", "target": "Firefox"})
    assert "2 windows" in out
    assert not any(a == "-c" for argv in wmctrl for a in argv), "closed one anyway"


def test_a_unique_title_substring_closes_the_right_window(wmctrl):
    out = desktop.desktop({"action": "close_window", "target": "terminal"})
    assert ["wmctrl", "-i", "-c", "0x04a00003"] in wmctrl
    assert "Terminal" in out


def test_a_window_id_beats_a_title_match(wmctrl):
    """list_windows hands the model ids precisely so it can be unambiguous.
    Falling through to a substring search on an id would defeat that."""
    desktop.desktop({"action": "focus_window", "target": "0x04a00009"})
    assert ["wmctrl", "-i", "-a", "0x04a00009"] in wmctrl


def test_no_matching_window_says_so(wmctrl):
    assert "No open window" in desktop.desktop({"action": "focus_window", "target": "Slack"})


def test_the_success_sentence_is_a_word_she_can_say(wmctrl):
    """Building the past tense as verb + "d" makes "Focusd Terminal." Every
    one of these strings is read aloud, and a mispronounced verb is the kind of
    thing that gets heard once and remembered as her sounding broken."""
    out = desktop.desktop({"action": "focus_window", "target": "Terminal"})
    assert out == "Focused Terminal."


# ── launching ──────────────────────────────────────────────────────────────
@pytest.fixture
def spawn_only(monkeypatch, display):
    """Popen recorded, run() fatal: a launched GUI app does not exit, so
    anything that WAITS on one holds the voice turn for the app's lifetime."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda *a, **kw: pytest.fail("waited on a launched app"))
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))
    monkeypatch.setattr(desktop.subprocess, "Popen", FakePopen)
    return calls


def test_launch_detaches_so_the_app_outlives_the_turn(spawn_only):
    """start_new_session, or the app dies with the next cortana restart - and
    every stream to DEVNULL, or a chatty app fills a pipe nobody drains and
    blocks on its own stdout."""
    desktop.desktop({"action": "launch", "target": "firefox"})
    argv, kw = spawn_only[0]
    assert argv == ["firefox"]
    assert kw["start_new_session"] is True
    assert kw["stdout"] is subprocess.DEVNULL and kw["stdin"] is subprocess.DEVNULL


def test_launch_keeps_a_quoted_path_with_a_space_in_one_piece(spawn_only):
    """Nothing here goes through a shell, so a plain str.split() makes
    "/home/x/My Project" into two arguments and the app opens the wrong thing
    or nothing. Quotes are the only way to express it."""
    desktop.desktop({"action": "launch",
                     "target": 'code --new-window "/home/x/My Project"'})
    assert spawn_only[0][0] == ["code", "--new-window", "/home/x/My Project"]


def test_an_unclosed_quote_is_a_sentence_not_an_internal_error(spawn_only):
    """shlex raises on a dangling quote. Left to the catch-all in desktop() it
    reads as "the launch action failed", which sounds like a bug in her rather
    than a fixable typo in the request."""
    out = desktop.desktop({"action": "launch", "target": 'code "/home/x/My Project'})
    assert not spawn_only, "launched something despite not knowing where the args ended"
    assert "quote" in out and out.rstrip().endswith(".")


def test_notify_with_nothing_to_say_asks_instead_of_posting_a_blank_toast(monkeypatch, display):
    """title defaulted to "Cortana" before the emptiness check, so the check
    could never fire: `notify` with no arguments put an empty toast on screen
    and reported success."""
    monkeypatch.setattr(desktop, "_which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda *a, **kw: pytest.fail("posted an empty toast"))
    assert "?" in desktop.desktop({"action": "notify"})
