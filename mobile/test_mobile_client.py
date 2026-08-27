"""Structural checks on the Android client, because CI is the only compiler.

Kotlin cannot be built on the Windows dev box - no gradle, no Android SDK - so
the compiler's first sight of this code is the `mobile-apk` workflow, several
minutes and one push away. Every failure caught here is a round trip saved.

These are deliberately NOT unit tests of behaviour: nothing here runs a line of
Kotlin. They pin the four things that broke silently in the past and cannot be
seen by reading a diff:

  * a splice that unbalanced a file's braces,
  * a call to a helper that was renamed or never written,
  * a manifest component or drawable whose class/file does not exist,
  * a runtime permission the code checks and the manifest never declared -
    which fails at RUNTIME on the phone, with a SecurityException, and only for
    the person holding it.

Stdlib only, so it runs in CI's pytest + python-dotenv + python-dateutil box.
"""
import pathlib
import re
import xml.etree.ElementTree as ET

import pytest

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "app" / "src" / "main"
KOTLIN = SRC / "java" / "com" / "cortana" / "mobile"
MANIFEST = SRC / "AndroidManifest.xml"
GRADLE = ROOT / "app" / "build.gradle"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

# Singletons whose members are referenced by name across files. A typo in any
# of these is a compile error CI would find; finding it here is free.
OBJECTS = ("Prefs", "Presence", "Comms", "Theme", "Ui", "Help", "LinkClient",
           "LinkService", "UpdateManager", "SphereWidget", "WavRecorder")


def kotlin_files():
    return sorted(KOTLIN.glob("*.kt"))


# ── a Kotlin-shaped lexer ───────────────────────────────────────────────────
# Naive comment/string stripping is not good enough here. Kotlin string
# templates nest arbitrarily - "${a.ifEmpty { "-" }}" contains a quote INSIDE a
# quoted string - so a scanner that ends the string at the second quote both
# mis-strips the code and mis-counts the braces it was supposed to be checking.
# This tracks the template nesting explicitly.
def strip(src):
    """Return (code_only_text, balances) with strings and comments removed.

    balances is a dict of the closing depth of (), [] and {}; all three must be
    zero in a file that compiles.
    """
    out = []
    # Stack of frames. A "code" frame remembers the brace depth it began at, so
    # the } that closes a ${...} template can be told from an ordinary one.
    stack = [{"kind": "code", "brace_at": 0}]
    depth = {"(": 0, "[": 0, "{": 0}
    i, n = 0, len(src)
    while i < n:
        top = stack[-1]
        c = src[i]
        if top["kind"] == "code":
            if c == "/" and i + 1 < n and src[i + 1] == "/":
                i = src.find("\n", i)
                if i < 0:
                    break
                continue
            if c == "/" and i + 1 < n and src[i + 1] == "*":
                nest, i = 1, i + 2
                while i < n and nest:
                    if src.startswith("/*", i):
                        nest, i = nest + 1, i + 2
                    elif src.startswith("*/", i):
                        nest, i = nest - 1, i + 2
                    else:
                        i += 1
                continue
            if src.startswith('"""', i):
                stack.append({"kind": "raw"})
                out.append(" ")
                i += 3
                continue
            if c == '"':
                stack.append({"kind": "str"})
                out.append(" ")
                i += 1
                continue
            if c == "'":
                i += 1
                while i < n and src[i] != "'":
                    i += 2 if src[i] == "\\" else 1
                i += 1
                out.append(" ")
                continue
            if c in "([":
                depth[c] += 1
            elif c == ")":
                depth["("] -= 1
            elif c == "]":
                depth["["] -= 1
            elif c == "{":
                depth["{"] += 1
            elif c == "}":
                # The } that closes a ${ ... } is the one at the exact depth the
                # template opened at; anything deeper is an ordinary lambda
                # brace inside it. Both cases decrement - the ${ counted as an
                # open brace when it was pushed.
                if len(stack) > 1 and depth["{"] == top["brace_at"]:
                    depth["{"] -= 1
                    stack.pop()
                    i += 1
                    continue
                depth["{"] -= 1
            out.append(c)
            i += 1
            continue

        # inside a string literal
        if top["kind"] == "str":
            if c == "\\":
                i += 2
                continue
            if c == '"':
                stack.pop()
                i += 1
                continue
        else:                              # raw """ string
            if src.startswith('"""', i):
                stack.pop()
                i += 3
                continue
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            depth["{"] += 1
            stack.append({"kind": "code", "brace_at": depth["{"]})
            out.append(" ")
            i += 2
            continue
        i += 1
    return "".join(out), depth


def declared_members(path):
    """Every name a sibling file could legally reach through Object.name."""
    code, _ = strip(path.read_text(encoding="utf-8"))
    names = set()
    names |= set(re.findall(r"\bfun\s+(?:<[^>]*>\s*)?(\w+)\s*\(", code))
    names |= set(re.findall(r"\b(?:va[lr])\s+(\w+)", code))
    names |= set(re.findall(r"\b(?:class|interface|object|enum class)\s+(\w+)", code))
    return names


@pytest.fixture(scope="module")
def sources():
    return {p.name: strip(p.read_text(encoding="utf-8")) for p in kotlin_files()}


# ── the four failures worth catching before a push ──────────────────────────
def test_every_kotlin_file_is_bracket_balanced(sources):
    """A bad splice is the single most common way this tree breaks.

    It is invisible in review - the diff looks right and the damage is a brace
    two hundred lines below the change - and it costs a full CI round trip to
    discover. Every file must close every (, [ and { it opens.
    """
    for name, (_code, depth) in sources.items():
        assert depth == {"(": 0, "[": 0, "{": 0}, f"{name} is unbalanced: {depth}"


def test_no_cross_file_call_names_a_member_that_does_not_exist(sources):
    """`Prefs.setPresenceOnn(...)` compiles nowhere and is invisible here.

    Every Object.member reference is checked against the members that object's
    own file actually declares. This is a lower bound on correctness - it says
    nothing about types or visibility - but the class of typo it catches is
    exactly the one that survives a careful read.
    """
    members = {}
    for p in kotlin_files():
        stem = p.stem
        if stem in OBJECTS:
            members[stem] = declared_members(p)
    missing = []
    pattern = re.compile(r"\b(" + "|".join(OBJECTS) + r")\.(\w+)")
    for name, (code, _depth) in sources.items():
        for obj, member in pattern.findall(code):
            if obj not in members:
                continue
            if member[0].isupper() and member.isupper():
                continue                      # SCREAMING_CASE constants
            if member not in members[obj]:
                missing.append(f"{name}: {obj}.{member}")
    assert not missing, "undefined members: " + ", ".join(sorted(set(missing)))


def test_manifest_is_well_formed_and_every_component_class_exists():
    """A component named in the manifest with no class behind it installs fine.

    It then fails the first time Android tries to instantiate it - at boot, or
    when a notification is tapped - which is the worst possible moment and the
    hardest to attribute.
    """
    root = ET.parse(MANIFEST).getroot()      # raises on malformed XML
    app = root.find("application")
    assert app is not None
    named = []
    for tag in ("activity", "service", "receiver", "provider"):
        for el in app.findall(tag):
            named.append(el.get(ANDROID_NS + "name"))
    local = [n for n in named if n and n.startswith(".")]
    assert local, "no local components found - the parse went wrong"
    for n in local:
        cls = n[1:]
        path = KOTLIN / (cls + ".kt")
        assert path.exists(), f"{n} has no {cls}.kt"
        assert re.search(r"\bclass\s+" + cls + r"\b",
                         path.read_text(encoding="utf-8")), f"{cls}.kt declares no class {cls}"


def test_every_runtime_permission_the_code_checks_is_declared(sources):
    """An undeclared permission cannot be granted, only refused - on the phone.

    checkSelfPermission returns DENIED forever, requestPermissions returns
    without showing a dialog, and the feature is simply dead with no error
    anywhere. Nothing about that is visible from this machine, so it is pinned
    here instead.
    """
    declared = {el.get(ANDROID_NS + "name")
                for el in ET.parse(MANIFEST).getroot().findall("uses-permission")}
    used = set()
    for _name, (code, _depth) in sources.items():
        used |= {"android.permission." + m
                 for m in re.findall(r"\bManifest\.permission\.(\w+)", code)}
    missing = used - declared
    assert not missing, "used but not declared: " + ", ".join(sorted(missing))


# ── smaller pins, each one an actual past or plausible break ────────────────
def test_referenced_drawables_exist(sources):
    """R.drawable.x that does not exist is a compile error CI would catch, but
    it is also the cheapest possible thing to check, and the notification icon
    is a new file that nothing else references."""
    have = {p.stem for p in (SRC / "res").rglob("*.xml")}
    have |= {p.stem for p in (SRC / "res").rglob("*.png")}
    for name, (code, _depth) in sources.items():
        for d in re.findall(r"\bR\.drawable\.(\w+)", code):
            assert d in have, f"{name} references missing drawable {d}"


def test_every_help_topic_used_has_an_entry():
    """A `?` with no entry behind it does nothing at all when tapped.

    Help.show() returns silently on an unknown topic - the right behaviour, and
    the reason a typo here is completely invisible until someone taps it.
    """
    help_src = (KOTLIN / "Help.kt").read_text(encoding="utf-8")
    topics = set(re.findall(r'^\s*"([\w.-]+)" to \(', help_src, re.M))
    assert topics, "no topics parsed out of Help.kt"
    used = set()
    for p in kotlin_files():
        raw = p.read_text(encoding="utf-8")
        used |= set(re.findall(r'helpIcon\(\s*\w+\s*,\s*"([\w.-]+)"', raw))
        used |= set(re.findall(r'cardHeader\([^,]+,\s*"[^"]*",\s*"([\w.-]+)"', raw))
    assert used, "no help topics referenced - the scrape went wrong"
    assert used <= topics, "no Help entry for: " + ", ".join(sorted(used - topics))


def test_every_card_type_has_a_signature_a_builder_and_a_help_entry():
    """The card recipe has three halves and forgetting one fails quietly.

    No builder branch: the card silently never appears. No signature branch:
    signatureFor returns "" for every push, so the card is built once and then
    NEVER repaints - it shows the first snapshot forever, which reads as a
    frozen link rather than a missing branch.
    """
    src = (KOTLIN / "MainActivity.kt").read_text(encoding="utf-8")
    supported = re.search(r"private val supported = listOf\((.*?)\)", src, re.S)
    assert supported, "could not find the supported list"
    types = re.findall(r'"(\w+)"', supported.group(1))
    assert "upcoming" in types and "sentinel" in types and "presence" in types

    sig_body = re.search(r"private fun signatureFor\(.*?\n    \}", src, re.S)
    build_body = re.search(r"private fun buildCard\(.*?\n\n", src, re.S)
    assert sig_body and build_body
    help_topics = set(re.findall(r'^\s*"([\w.-]+)" to \(',
                                 (KOTLIN / "Help.kt").read_text(encoding="utf-8"), re.M))
    for t in types:
        assert f'"{t}" ->' in sig_body.group(0), f"{t} has no signatureFor branch"
        assert f'"{t}" ->' in build_body.group(0), f"{t} has no buildCard branch"
        assert t in help_topics, f"{t} has no Help entry"


def test_version_code_and_name_moved_together():
    """CI's release step is a no-op unless BOTH move.

    versionName tags the release and feeds the in-app update prompt; versionCode
    is what Android's installer compares. Bumping one and not the other ships a
    build nobody can install, and says nothing about it.
    """
    g = GRADLE.read_text(encoding="utf-8")
    code = int(re.search(r"versionCode\s+(\d+)", g).group(1))
    name = re.search(r'versionName\s+"([^"]+)"', g).group(1)
    assert code > 20, "versionCode was not bumped past the 2.4.0 release"
    assert name != "2.4.0", "versionName was not bumped past 2.4.0"
    # A name of "2.5.0" with a code of 20 is the exact shipwreck above.
    major_minor = tuple(int(x) for x in name.split(".")[:2])
    assert major_minor >= (2, 5)


def test_link_client_is_no_longer_stopped_by_argumentless_call():
    """The service is a holder of the same socket as the screens.

    stop() with no argument dropped whichever holder was counted last, so
    MainActivity going to the background could tear the socket down under the
    service that was meant to be keeping it alive. Every call site must name
    the holder that is letting go.
    """
    for p in kotlin_files():
        code, _ = strip(p.read_text(encoding="utf-8"))
        assert "LinkClient.stop()" not in code, f"{p.name} still calls stop() with no holder"


def test_capability_switches_all_default_to_off():
    """Anything that reads location, SMS or other apps' notifications must be
    off on a fresh install. An app that starts doing that unasked is
    indistinguishable from spyware, whoever wrote it."""
    prefs = (KOTLIN / "Prefs.kt").read_text(encoding="utf-8")
    for key in ("bgLink", "presenceOn", "commsNotif", "smsRead", "smsSend"):
        m = re.search(r'getBoolean\("' + key + r'",\s*(\w+)\)', prefs)
        assert m, f"no accessor found for {key}"
        assert m.group(1) == "false", f"{key} does not default to off"


# ── permissions a call needs but never names ────────────────────────────────
# The check above only sees permissions the code writes out as
# Manifest.permission.X. The expensive ones are the permissions an API method
# REQUIRES without any mention of them anywhere in the source: the call throws
# SecurityException, the catch around it turns that into silence, and the
# feature is dead on the phone with nothing in any log. ACCESS_NETWORK_STATE
# for registerDefaultNetworkCallback shipped exactly that way once.
IMPLIED_PERMISSIONS = {
    "registerDefaultNetworkCallback": "android.permission.ACCESS_NETWORK_STATE",
    "registerNetworkCallback": "android.permission.ACCESS_NETWORK_STATE",
    "getNetworkCapabilities": "android.permission.ACCESS_NETWORK_STATE",
    "getActiveNetwork": "android.permission.ACCESS_NETWORK_STATE",
    "requestLocationUpdates": "android.permission.ACCESS_COARSE_LOCATION",
    "sendMultipartTextMessage": "android.permission.SEND_SMS",
    "setExactAndAllowWhileIdle": "android.permission.SCHEDULE_EXACT_ALARM",
}


def test_permissions_implied_by_an_api_call_are_declared(sources):
    """A @RequiresPermission the source never spells out is invisible twice.

    Nothing in the code says "ACCESS_NETWORK_STATE", so the check on
    Manifest.permission.X references cannot see it; the SecurityException lands
    in the catch that was written for "no connectivity service"; and the only
    symptom is that switching from Wi-Fi to LTE waits out a thirty-second
    backoff instead of reconnecting at once. Nobody would ever attribute that.
    """
    declared = {el.get(ANDROID_NS + "name")
                for el in ET.parse(MANIFEST).getroot().findall("uses-permission")}
    missing = []
    for name, (code, _depth) in sources.items():
        for call, perm in IMPLIED_PERMISSIONS.items():
            if re.search(r"\b" + call + r"\s*\(", code) and perm not in declared:
                missing.append(f"{name} calls {call}() but {perm} is not declared")
    assert not missing, "; ".join(sorted(set(missing)))


DANGEROUS = {
    "android.permission.READ_SMS", "android.permission.SEND_SMS",
    "android.permission.RECEIVE_SMS", "android.permission.READ_CONTACTS",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_BACKGROUND_LOCATION",
    "android.permission.RECORD_AUDIO", "android.permission.CAMERA",
    "android.permission.READ_CALL_LOG", "android.permission.READ_PHONE_STATE",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.BLUETOOTH_CONNECT",
}


def test_no_dangerous_permission_is_declared_that_the_code_never_uses(sources):
    """The reverse direction, and it is not cosmetic.

    RECEIVE_SMS was declared and requested with a dialog while nothing in the
    app ever received an SMS. Every unused dangerous permission buys a scarier
    install screen, a Play Protect warning and a grant the user is asked to
    make for no function - and, if it is ever granted, a capability this app
    holds and no line of code accounts for.
    """
    declared = {el.get(ANDROID_NS + "name")
                for el in ET.parse(MANIFEST).getroot().findall("uses-permission")}
    used = set()
    for _name, (code, _depth) in sources.items():
        used |= {"android.permission." + m
                 for m in re.findall(r"\bManifest\.permission\.(\w+)", code)}
    unused = (declared & DANGEROUS) - used
    assert not unused, "declared but never used: " + ", ".join(sorted(unused))


def test_location_and_bluetooth_are_requested_in_one_call():
    """Activity.requestPermissions refuses a second request while one is open.

    It logs "Can request only one set of permissions at a time" and hands back
    an empty result, so back-to-back request(LOCATION); request(BLUETOOTH_CONNECT)
    dropped the second one every single time. From API 31 that grant is what
    lets the phone RECEIVE the car's ACL_CONNECTED broadcast, so driving
    detection was dead and the only symptom was a presence card that never said
    "driving".
    """
    src = (KOTLIN / "SettingsActivity.kt").read_text(encoding="utf-8")
    toggle = re.search(r"Prefs\.setPresenceOn\(this, on\)(.*?)\n        \}\)", src, re.S)
    assert toggle, "could not find the presence toggle body"
    body = toggle.group(1)
    assert "BLUETOOTH_CONNECT" in body and "ACCESS_COARSE_LOCATION" in body
    # Exactly one requestPermissions round trip out of this handler.
    assert len(re.findall(r"\brequest\(", body)) == 1, \
        "the presence toggle issues more than one permission request"


def test_a_dead_socket_callback_cannot_flip_the_link_state():
    """stop() then start() leaves the OLD socket's callback still in flight.

    close(1000) does not complete until the peer answers, so backgrounding and
    reopening the app lands onClosed for socket 1 after socket 2 is already up.
    Without an identity guard that callback ran setLink(false) over a working
    link, and nothing sets linkUp back to true without a fresh onOpen - so the
    MOBILE LINK card and the service's permanent row read DISCONNECTED while
    state frames kept arriving.
    """
    code, _ = strip((KOTLIN / "LinkClient.kt").read_text(encoding="utf-8"))
    for cb in ("onFailure", "onClosed"):
        body = re.search(r"override fun " + cb + r"\(.*?\n            \}", code, re.S)
        assert body, f"{cb} not found"
        guard = body.group(0).index("ws !== webSocket")
        assert guard < body.group(0).index("setLink(false)"), \
            f"{cb} touches the link state before checking the socket is current"


def test_a_cmd_frame_is_never_dropped_just_because_no_hook_is_installed():
    """One slot, three components, and a window where all of them let go.

    LinkService sets LinkClient.onCmd on attach and clears it on destroy;
    MainActivity fills it in only when it finds it null. Stopping the service
    from its own notification while the board was open therefore left a live
    socket, capability switches reading ON, and every inbound command silently
    discarded until the activity happened to restart.
    """
    # Raw source, not strip()ped: the branch is keyed on a string literal and
    # strip() is exactly what removes those.
    raw = (KOTLIN / "LinkClient.kt").read_text(encoding="utf-8")
    frame = re.search(r'"cmd" ->(.*?)\n                \}', raw, re.S)
    assert frame, "no cmd branch in the socket reader"
    assert "Comms.handleCmd" in frame.group(1), \
        "a cmd frame with no hook installed goes nowhere"
