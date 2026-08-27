"""System, network and account sentinel: turn poll-a-tile into alert-me.

Everything this module watches fails SILENTLY today. The agenda goes stale for
a week before anyone notices it is wrong; the box runs out of RAM and systemd
quietly respawns whatever died; spend crosses the budget between two glances at
the dashboard. None of it raises anywhere a human is looking.

Three rules hold the design together:

  * One broken probe degrades ONE row. A check that throws is reported as a bad
    row carrying its own exception text, and every other check still reports. A
    sentinel that goes dark because a thermal zone was renamed is worse than no
    sentinel at all.
  * A probe that cannot measure reports `unknown`, never `ok`. install-spotifyd
    is the cautionary tale in this repo: it probed a directory the *shell*
    could write and called that evidence about a confined curl. Unmeasured is
    not the same as healthy.
  * Every subprocess is throttled by its OWN clock, not by the caller's tick. A
    prior bug here spawned a systemctl process 24 times a minute forever, on a
    laptop, and had to be walked back. No caller - a fast loop, a forced poll,
    a tool call - can turn that back on.

Cortana is the only writer of sentinel_state.json, the same one-writer
invariant hud_state.json and presence_desk.json already depend on.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import config

STATE_FILE = Path(__file__).resolve().parent / "sentinel_state.json"


def _cfg(name, default):
    """Read a tunable from config.py, falling back to a local default.

    getattr rather than `from config import ...` on purpose: sentinel must
    import cleanly on a box whose config.py predates it. A hard import would
    turn "the sentinel is newer than the config" into a crash of the entire
    cortana process at startup, which is precisely the silent-death failure
    class this module exists to catch.
    """
    return getattr(config, name, default)


MB = 1024 * 1024
GB = 1024 * MB

# Absolute headroom, not a percentage: what kills a process is an allocation
# failing, and that happens at an absolute number of free bytes no matter how
# big the machine is.
MEM_WARN = float(_cfg("SENTINEL_MEM_WARN_MB", 600)) * MB
MEM_BAD = float(_cfg("SENTINEL_MEM_BAD_MB", 300)) * MB
DISK_WARN = float(_cfg("SENTINEL_DISK_WARN_GB", 5)) * GB
DISK_BAD = float(_cfg("SENTINEL_DISK_BAD_GB", 1.5)) * GB
TEMP_WARN = float(_cfg("SENTINEL_TEMP_WARN_C", 82))
TEMP_BAD = float(_cfg("SENTINEL_TEMP_BAD_C", 92))
SPEND_WARN = float(_cfg("SENTINEL_SPEND_WARN", 0.8))
# Google kills refresh tokens after SEVEN days while the consent screen sits in
# "Testing". Warn at six, so there is a day left to act in.
GOOGLE_TOKEN_AGE = float(_cfg("SENTINEL_GOOGLE_TOKEN_DAYS", 6)) * 86400
APK_STALE = float(_cfg("SENTINEL_APK_STALE_DAYS", 30)) * 86400
UNITS = tuple(_cfg("SENTINEL_UNITS",
                   ("cortana", "cortana-dash", "cortana-bridge", "cortana-spotifyd")))
UNITS_EVERY = float(_cfg("SENTINEL_UNITS_EVERY", 300))
TAILSCALE_EVERY = float(_cfg("SENTINEL_TAILSCALE_EVERY", 600))
GIT_EVERY = float(_cfg("SENTINEL_GIT_EVERY", 900))
NET_EVERY = float(_cfg("SENTINEL_NET_EVERY", 120))
# A numeric address, and a knob: a network that blackholes 1.1.1.1 would
# otherwise leave this row permanently and unfixably bad.
NET_HOST = str(_cfg("SENTINEL_NET_HOST", "1.1.1.1"))
NET_PORT = int(_cfg("SENTINEL_NET_PORT", 443))
INTERVAL = float(_cfg("SENTINEL_INTERVAL", 60))
# How long a bad row stays quiet before it is worth saying again. Nagging every
# minute about a disk that is still full is how a user learns to ignore a voice.
REALERT = float(_cfg("SENTINEL_REALERT", 3 * 3600))
# Rewrite the state file at least this often even when nothing changed, so its
# `ts` stays a liveness signal - see publish(). The upper clamp is not a
# preference: bridge/watch.py calls this file stale at 300 seconds, so a
# heartbeat derived only from INTERVAL would grey the tile out on any box that
# configured a slower loop, and it would do it silently.
HEARTBEAT = min(240.0, max(120.0, INTERVAL * 2))

# `unknown` ranks with `ok` deliberately: an unmeasurable probe is not an
# incident. It must never be the reason worst() says the system is fine, and it
# must never raise an alarm on its own.
_RANK = {"ok": 0, "unknown": 0, "warn": 1, "bad": 2}

_cache = {}            # key -> {"state", "detail", "ts", "metric"}
_alerted = {}          # key -> {"state", "ts"} - what the user was last told
_alerted_loaded = {"done": False}
# meta, NOT kv: recall_all() dumps every kv row into the system prompt verbatim
# on every single turn, and this is an internal cursor, not a fact about the user.
_ALERTED_KEY = "sentinel_alerted"
_last_written = None   # last payload flushed to disk (minus its timestamp)
_last_write_ts = 0.0   # when that flush actually landed - see publish()
_probe_ts = {}         # per-subprocess throttle clocks, independent of _cache
_probe_out = {}


def _have(binary):
    """Every external binary may be absent - xdotool, wmctrl, xclip, playerctl
    and xprintidle already are on the runtime box. It is a named function so
    tests can pretend a binary exists without patching shutil globally."""
    return bool(shutil.which(binary))


def _run(args, timeout=5):
    """stdout of a command, or empty. Deliberately returns stdout even on a
    non-zero exit: `systemctl is-active` exits 3 when a unit is down, which is
    exactly the case being asked about."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return out.stdout or ""
    except Exception:
        return ""


def _throttled(key, every, fn, now, default=""):
    """Run fn() at most once per `every` seconds, forever, no matter who asks.

    The throttle clock lives here rather than in the check cadence because the
    two are not the same thing: a check may be forced (a tool call, a restart,
    a test) while the process it shells out to must still be rate-limited.

    The window is gated on the CLOCK alone. Gating it on "is there a cached
    output" instead meant a probe that raised never recorded one, so it was
    re-run on every single tick - a probe that fails fast turning into exactly
    the process storm this function exists to prevent, and only while something
    was already wrong.
    """
    last = _probe_ts.get(key)
    if last is not None and (now - last) < every:
        return _probe_out.get(key, default)
    _probe_ts[key] = now           # burn the window BEFORE the risky call
    try:
        _probe_out[key] = fn()
    except Exception:
        _probe_out[key] = default
    return _probe_out[key]


# Details are read aloud by speakable() and by notify.deliver, and they carry
# text this module did not write: an exception message, or calendar_state's
# `error` field straight from a Google API. Both routinely contain newlines and
# URLs, which the house rule forbids in anything spoken. Stripping at the point
# of storage covers every row, including rows added later.
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+")


def _prose(text):
    return " ".join(_URL_RE.sub("", str(text)).split())


def _ago(seconds):
    """Prose, because these strings are spoken as well as displayed."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(round(seconds / 60))} minutes ago"
    if seconds < 172800:
        return f"{int(round(seconds / 3600))} hours ago"
    return f"{int(round(seconds / 86400))} days ago"


# -- mem: FIRST, and first for a reason -------------------------------------
# The runtime box has 5 GB total with roughly 2 GB free while Electron, Python
# and aiohttp are all up. RAM exhaustion is the single most likely way Cortana
# actually dies, and the Restart=always units turn that death into a 5-second
# respawn loop that `systemctl start` reports as SUCCESS and the shell reports
# as nothing at all. This check exists to make that specific failure visible
# BEFORE the kill, because afterwards only the journal knows it happened.
_MEMINFO_RE = re.compile(r"^(\w+):\s+(\d+) kB$")


def parse_meminfo(text):
    """/proc/meminfo -> {field: bytes}.

    This is character for character the parse in Dashboard/app/main.js
    readMeminfo(): same regex, same kB-only filter, same *1024. Two parses of
    one file that disagree is a bug nobody can see - the dashboard tile would
    read 1.9 GB free while the sentinel called that same moment critical, and
    there would be no way to tell which of them was lying.
    """
    out = {}
    for line in str(text).split("\n"):
        m = _MEMINFO_RE.match(line.rstrip("\r"))
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def classify_mem(avail, total):
    # `avail is None`, not `not avail`. Zero bytes available is the single most
    # severe reading this row can take, and the falsy test reported it as "no
    # /proc/meminfo on this machine" - i.e. unmeasurable, which ranks with ok.
    # The one moment the leading check matters most was the one moment it said
    # nothing at all.
    if avail is None or not total:
        return "unknown", "No /proc/meminfo on this machine."
    free_gb, total_gb = avail / GB, total / GB
    if avail <= MEM_BAD:
        return "bad", (f"Only {free_gb:.1f} of {total_gb:.1f} gigabytes free. "
                       "The next thing killed comes back as a silent restart loop.")
    if avail <= MEM_WARN:
        return "warn", f"{free_gb:.1f} of {total_gb:.1f} gigabytes free. Getting tight."
    return "ok", f"{free_gb:.1f} of {total_gb:.1f} gigabytes free."


def _check_mem(now=None):
    try:
        text = Path("/proc/meminfo").read_text()
    except Exception:
        return "unknown", "No /proc/meminfo on this machine."
    mi = parse_meminfo(text)
    avail, total = mi.get("MemAvailable"), mi.get("MemTotal")
    state, detail = classify_mem(avail, total)
    if avail is None or not total:
        return state, detail
    return state, detail, {"mem_free_mb": int(avail / MB),
                           "mem_free_pct": int(100.0 * avail / total)}


# -- disk -------------------------------------------------------------------
def classify_disk(free, total):
    free_gb, total_gb = free / GB, total / GB
    if free <= DISK_BAD:
        return "bad", (f"Only {free_gb:.1f} gigabytes left on the root disk. "
                       "Writes are about to start failing.")
    if free <= DISK_WARN:
        return "warn", f"{free_gb:.1f} gigabytes left of {total_gb:.0f}."
    return "ok", f"{free_gb:.0f} gigabytes free of {total_gb:.0f}."


def _check_disk(now=None):
    usage = shutil.disk_usage(str(config.ROOT))
    state, detail = classify_disk(usage.free, usage.total)
    return state, detail, {"disk_free_gb": round(usage.free / GB, 1),
                           "disk_pct": int(100.0 * (usage.total - usage.free) / usage.total)}


# -- temp -------------------------------------------------------------------
def classify_temp(celsius):
    if celsius is None:
        # Absence is not health. A missing thermal zone means we do not know,
        # and `unknown` is what that is called here.
        return "unknown", "No thermal zone is exposed on this machine."
    if celsius >= TEMP_BAD:
        return "bad", f"Running at {celsius:.0f} degrees. It will throttle or shut down."
    if celsius >= TEMP_WARN:
        return "warn", f"Running warm, {celsius:.0f} degrees."
    return "ok", f"{celsius:.0f} degrees."


def _hottest_zone():
    """Hottest thermal zone in Celsius, or None.

    Zones move between kernels and a laptop exposes several, so each is read in
    its own try - one unreadable zone must not hide the others.
    """
    best = None
    try:
        zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    except Exception:
        return None
    for z in zones:
        try:
            milli = int(z.read_text().strip())
        except Exception:
            continue
        c = milli / 1000.0
        if 0 < c < 150 and (best is None or c > best):   # outside that is sensor junk
            best = c
    return best


def _check_temp(now=None):
    c = _hottest_zone()
    state, detail = classify_temp(c)
    return (state, detail) if c is None else (state, detail, {"temp_c": int(c)})


# -- units: the one that must never become a process storm ------------------
def classify_units(statuses):
    """statuses: {unit: active|inactive|failed|activating|unknown}."""
    if not statuses:
        return "unknown", "systemctl is not on this machine."
    failed = [u for u, s in statuses.items() if s == "failed"]
    down = [u for u, s in statuses.items() if s in ("inactive", "deactivating")]
    busy = [u for u, s in statuses.items() if s == "activating"]
    if failed:
        return "bad", f"{', '.join(failed)} failed."
    if down:
        return "warn", f"{', '.join(down)} is not running."
    if busy:
        return "warn", f"{', '.join(busy)} is still starting."
    live = [u for u, s in statuses.items() if s == "active"]
    if not live:
        return "unknown", "No cortana units are installed here."
    return "ok", f"All {len(live)} services are up."


def _unit_statuses(now):
    """ONE `systemctl --user is-active a b c d` per UNITS_EVERY seconds.

    One invocation for all four units, not four - and hard-throttled on its own
    clock. The 24-processes-a-minute bug came from exactly this shape of check
    sitting on a fast loop. Batching plus the throttle puts a ceiling of twelve
    short-lived processes an hour on it, which is a rounding error in idle burn.
    """
    if not _have("systemctl"):
        return {}
    out = _throttled("units", UNITS_EVERY,
                     lambda: _run(["systemctl", "--user", "is-active"] + list(UNITS)),
                     now)
    lines = [ln.strip() for ln in out.strip().split("\n") if ln.strip()]
    if len(lines) != len(UNITS):
        # systemd prints exactly one line per unit. Anything else means we are
        # reading something we do not understand, and guessing at it would
        # invent an outage out of a parse failure.
        return {u: "unknown" for u in UNITS}
    return dict(zip(UNITS, lines))


def _check_units(now=None):
    return classify_units(_unit_statuses(time.time() if now is None else now))


# -- net --------------------------------------------------------------------
def _tcp_ok(host, port, timeout=3):
    try:
        socket.create_connection((host, port), timeout).close()
        return True
    except OSError:
        return False


def _dns_ok(name="api.anthropic.com"):
    try:
        socket.getaddrinfo(name, 443)
        return True
    except OSError:
        return False


def _tailscale_state(now):
    """Backend state, or empty if tailscale is absent or unreadable.

    Its own ten-minute throttle. The phone link is Tailscale-only so this is
    worth knowing, but not worth a subprocess every minute forever.
    """
    if not _have("tailscale"):
        return ""
    raw = _throttled("tailscale", TAILSCALE_EVERY,
                     lambda: _run(["tailscale", "status", "--peers=false", "--json"], 8),
                     now)
    try:
        return str(json.loads(raw).get("BackendState") or "")
    except Exception:
        return ""


def classify_net(reachable, dns, tailscale):
    if not reachable:
        # The reachability probe used a numeric address, so this cannot be a
        # name-resolution problem - the two are separated on purpose below.
        return "bad", "No route to the internet."
    if not dns:
        return "bad", "The network is up but DNS is not resolving."
    if tailscale and tailscale != "Running":
        return "warn", (f"Online, but Tailscale is {tailscale.lower()} - the phone "
                        "cannot reach the bridge.")
    if tailscale:
        return "ok", "Online, Tailscale up."
    return "ok", "Online."


def _net_facts(now):
    """(reachable, dns_resolves), on the net probe's OWN clock.

    The cadence in _CHECKS was not enough on its own: poll(force=True) ignores
    cadences by design - that is what force means - so a tool call or a restart
    loop could re-open a socket and a resolver on every invocation. The module
    promises no caller can do that; this is what makes the promise true for the
    network the same way _throttled already made it true for systemctl.
    """
    def probe():
        # A numeric address on purpose: connecting by IP asks about routing
        # ONLY, so a failure here can never be confused with broken DNS, which
        # the next line asks about separately. Two questions, two answers - the
        # same reason `curl -o file` versus `curl > file` settled the snap
        # confinement argument in one paste.
        reachable = _tcp_ok(NET_HOST, NET_PORT)
        return reachable, (_dns_ok() if reachable else False)

    return _throttled("net", NET_EVERY, probe, now, default=None)


def _check_net(now=None):
    now = time.time() if now is None else now
    facts = _net_facts(now)
    if facts is None:
        # The probe itself blew up. That is not evidence of an outage, and
        # "No route to the internet." spoken because socket() raised would be
        # a false alarm about the one thing the user cannot check from here.
        return "unknown", "The network could not be probed."
    reachable, dns = facts
    return classify_net(reachable, dns, _tailscale_state(now))


# -- git: she edits her own source ------------------------------------------
def classify_git(dirty, behind, detached=False):
    if detached:
        return "warn", "The repo is not on a branch, so self-updates land nowhere."
    bits = []
    if dirty:
        bits.append(f"{dirty} uncommitted file{'s' if dirty != 1 else ''}")
    if behind:
        bits.append(f"{behind} commit{'s' if behind != 1 else ''} behind")
    if not bits:
        return "ok", "Repo clean and up to date."
    tail = " and ".join(bits)
    return "warn", tail + (". A self-update will conflict." if dirty else ".")


def _git_facts(now):
    """(dirty, behind, branch) from ONE batch of git processes per GIT_EVERY.

    Three subprocesses, and the _CHECKS cadence does not bound them: a forced
    poll skips cadences on purpose. Unthrottled, `system_check` called in a
    loop is three git processes per call - the systemctl storm again, wearing
    a different name.
    """
    root = str(config.ROOT)

    def probe():
        status = _run(["git", "-C", root, "status", "--porcelain"])
        branch = _run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        # No fetch. Fetching from a background loop needs the deploy key, can
        # block on the network, and would rewrite FETCH_HEAD underneath
        # whatever the user is doing. This counts against the LAST fetch, which
        # is honest and free: `behind` staying at zero forever just means
        # nothing has fetched.
        behind_raw = _run(["git", "-C", root, "rev-list", "--count", "HEAD..@{u}"]).strip()
        return (len([ln for ln in status.split("\n") if ln.strip()]),
                int(behind_raw) if behind_raw.isdigit() else 0, branch)

    return _throttled("git", GIT_EVERY, probe, now, default=None)


def _check_git(now=None):
    if not _have("git"):
        return "unknown", "git is not on this machine."
    facts = _git_facts(time.time() if now is None else now)
    if facts is None:
        return "unknown", "The repo could not be read."
    dirty, behind, branch = facts
    state, detail = classify_git(dirty, behind, detached=(branch == "HEAD"))
    return state, detail, {"git_dirty": dirty, "git_behind": behind}


# -- spend ------------------------------------------------------------------
def classify_spend(spend, budget):
    if not budget:
        return "unknown", "No monthly budget is set."
    frac = spend / budget
    money = f"{spend:.2f} dollars of {budget:.0f} this month"
    if frac >= 1.0:
        return "bad", f"Over budget: {money}."
    if frac >= SPEND_WARN:
        return "warn", f"{money}, {int(frac * 100)} percent of the budget."
    return "ok", f"{money}."


def _check_spend(now=None):
    import memory      # lazy, per the house convention - nothing here owns import order
    try:
        spend = memory.month_spend()
    except Exception:
        # An uninitialised or unreadable state.db is not overspending, and the
        # generic handler in poll() would put a raw sqlite message ("no such
        # table: usage") into a string that gets SPOKEN. We cannot measure
        # spend, so say exactly that.
        return "unknown", "Usage has not been recorded yet."
    budget = float(config.BUDGET_MONTHLY_USD)
    state, detail = classify_spend(spend, budget)
    if not budget:
        return state, detail
    return state, detail, {"spend_usd": round(spend, 2),
                           "spend_pct": int(100.0 * spend / budget)}


# -- google: the one with proven, repeated value ----------------------------
# CLAUDE.md, CORTANA.md, README.md and SETUP.md all document the same recurring
# failure: with the OAuth consent screen left in "Testing", Google expires the
# refresh token after SEVEN DAYS, the agenda silently stops updating, and the
# user finds out days later by noticing the dashboard looks wrong. There is a
# one-line fix and nothing surfaces it. That is what this check is for, and it
# is why the fix command is spelled out in the detail string rather than left
# in a runbook nobody opens mid-week.
_AUTH_MARKERS = ("expired", "revoked", "invalid_grant", "google-auth",
                 "no usable token", "unauthorized", "reconnect")


def classify_google(cal, token_exists, token_age, now=None):
    """cal: calendar_state.read(). token_age: seconds since token.json was last
    written, or None.

    token.json's mtime is a real signal, not a proxy: tools/google_auth.creds()
    rewrites the file on every successful refresh, so its mtime is the last
    moment Google actually said yes.
    """
    now = time.time() if now is None else now
    fix = "Run python main.py --google-auth."
    if not token_exists:
        return "bad", "Google is not connected, so there is no agenda. " + fix
    err = str((cal or {}).get("error") or "").lower()
    if err and any(m in err for m in _AUTH_MARKERS):
        return "bad", ("Google access has expired, so the agenda is silently wrong. "
                       + fix)
    if err:
        # _prose BEFORE the slice: this text comes straight from a Google API
        # error, which routinely carries a console URL and a hard newline, and
        # slicing first can cut a URL in half into unreadable rubble that the
        # URL strip no longer recognises.
        return "warn", f"Calendar is erroring: {_prose((cal or {}).get('error'))[:90]}"
    if token_age is not None and token_age > GOOGLE_TOKEN_AGE:
        return "warn", (f"Google last refreshed {_ago(token_age)}, and tokens die at "
                        "seven days unless the consent screen is published. " + fix)
    ts = float((cal or {}).get("ts") or 0)
    if not ts:
        return "unknown", "The agenda has not been read yet this session."
    return "ok", f"Google connected, agenda refreshed {_ago(now - ts)}."


def _check_google(now=None):
    import calendar_state     # lazy, same convention as everywhere else here
    token = Path(config.GMAIL_TOKEN)
    try:
        age = time.time() - token.stat().st_mtime if token.exists() else None
    except OSError:
        age = None
    return classify_google(calendar_state.read(), token.exists(), age, now)


# -- apk --------------------------------------------------------------------
def classify_apk(info, now=None):
    if not info:
        return "unknown", "No phone app has been published yet."
    now = time.time() if now is None else now
    version = str(info.get("version") or "?")
    built = info.get("_built_ts") or 0
    if not built:
        return "warn", f"Phone app {version} is published with no build date."
    age = now - built
    if age > APK_STALE:
        return "warn", f"Phone app {version} was built {_ago(age)}."
    return "ok", f"Phone app {version}, built {_ago(age)}."


def _check_apk(now=None):
    import datetime
    path = Path(config.ROOT) / "mobile" / "dist" / "version.json"
    try:
        info = json.loads(path.read_text())
    except Exception:
        return classify_apk(None, now)
    try:
        raw = str(info.get("builtAt") or "").replace("Z", "+00:00")
        info["_built_ts"] = datetime.datetime.fromisoformat(raw).timestamp()
    except Exception:
        info["_built_ts"] = 0
    return classify_apk(info, now)


# -- the register -----------------------------------------------------------
# ORDER IS THE FILE ORDER, and memory is first on purpose - see _check_mem.
# Each row carries its own cadence: there is no reason to shell out to git as
# often as we read a file in /proc.
_CHECKS = (
    ("mem",    "Memory",      60,          _check_mem),
    ("disk",   "Disk",        300,         _check_disk),
    ("temp",   "Temperature", 120,         _check_temp),
    ("units",  "Services",    UNITS_EVERY, _check_units),
    ("net",    "Network",     NET_EVERY,   _check_net),
    ("git",    "Repo",        GIT_EVERY,   _check_git),
    ("spend",  "Spend",       300,         _check_spend),
    ("google", "Google",      300,         _check_google),
    ("apk",    "Phone app",   900,         _check_apk),
)


def poll(now=None, force=False):
    """Refresh whichever checks are due and return the snapshot.

    The try/except below is the entire failure-isolation story: one probe that
    throws becomes one bad row carrying its own exception text, and every other
    row is still measured and still reported.
    """
    # `now is None`, not `now or ...`, throughout this module: a caller passing
    # 0.0 - a test, or any replay of a recorded timeline - must get 0.0 back and
    # not silently get the wall clock instead. The falsy form hid a bug for one
    # test run and would hide it forever in production, where 0.0 never occurs.
    now = time.time() if now is None else now
    for key, label, every, fn in _CHECKS:
        prev = _cache.get(key)
        if prev and not force and (now - prev["ts"]) < every:
            continue
        try:
            # Every check takes `now` even when it ignores it: sniffing arity
            # here would mean catching TypeError around the call, and a genuine
            # TypeError raised INSIDE a probe would then be retried instead of
            # reported. One uniform signature, no guessing.
            got = fn(now)
            # A check may return a third element: numbers it happened to
            # measure anyway. routines.py compares metrics numerically (">= 90"
            # on disk_pct), which a state word cannot express, so handing them
            # over costs one tuple slot and saves that engine a second probe.
            state, detail = got[0], got[1]
            metric = got[2] if len(got) > 2 else None
        except Exception as e:
            state, detail, metric = "bad", f"This check itself failed: {str(e)[:120]}", None
        if state not in _RANK:
            state = "unknown"
        # _prose, not str(): every detail here is a candidate for TTS, and two
        # of them carry text this module did not write - an exception message
        # and calendar_state's `error` straight from a Google API. Both arrive
        # with newlines and URLs in them. Cleaning at the single point of
        # storage means a row added later cannot forget to.
        _cache[key] = {"state": state, "detail": _prose(detail)[:200], "ts": now,
                       "metric": metric if isinstance(metric, dict) else None}
    return snapshot()


def snapshot():
    """The /local/sentinel contract: {worst, checks:[{key,label,state,detail}]}.

    `metrics` rides alongside as a flat name -> number map. It is a superset of
    the agreed contract, not a change to it: routines.py already reads it
    opportunistically and every other reader can ignore it.
    """
    out, metrics = [], {}
    for key, label, _every, _fn in _CHECKS:
        c = _cache.get(key)
        if not c:
            continue
        out.append({"key": key, "label": label,
                    "state": c["state"], "detail": c["detail"]})
        if c.get("metric"):
            metrics.update(c["metric"])
    return {"worst": worst(), "checks": out, "metrics": metrics}


def worst():
    """ok | warn | bad. `unknown` rows are ignored: not knowing is not an
    incident, and it must never be the reason this says everything is fine."""
    rank = max((_RANK.get(c["state"], 0) for c in _cache.values()), default=0)
    return {0: "ok", 1: "warn", 2: "bad"}[rank]


def rows():
    """Live rows for the routines engine's health trigger, in file order."""
    return snapshot()["checks"]


def publish(now=None, force=False):
    """Poll, then write sentinel_state.json.

    Skip-unchanged, like hud_state.py - but with a floor under it, because two
    readers treat this file's `ts` as proof the sentinel is alive:
    routines.py drops EVERY health routine when it is more than 900 seconds old
    and bridge/watch.py greys the tile at 300. Skipping the write also freezes
    `ts`, so a payload that simply held steady would read as "the sentinel
    died" and silently disable health alerting altogether. HEARTBEAT bounds
    that: unchanged costs one write per two minutes, not 1,440 a day.
    """
    global _last_written, _last_write_ts
    now = time.time() if now is None else now
    payload = poll(now, force=force)
    if payload == _last_written and (now - _last_write_ts) < HEARTBEAT:
        return payload
    tmp = tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent,
                                      delete=False, suffix=".tmp")
    try:
        json.dump(dict(payload, ts=now), tmp)
        tmp.close()
        os.replace(tmp.name, STATE_FILE)   # atomic; no reader sees a half file
    except Exception:
        # Marking the payload written BEFORE the write succeeded meant one
        # failed write froze this file forever: every later publish saw an
        # unchanged payload and skipped, so the last thing on disk was whatever
        # predated the failure and nothing anywhere said so. The write fails on
        # a full disk - which is precisely the condition this module exists to
        # report - so that was the disk row silencing itself.
        try:
            tmp.close()
        except Exception:
            pass
        try:
            os.unlink(tmp.name)   # or every retry leaks a .tmp onto a full disk
        except OSError:
            pass
        raise
    _last_written, _last_write_ts = payload, now
    return payload


def read_state():
    """For any process that is NOT the writer - the bridge serving
    /local/sentinel.

    `stale` is reported, not acted on. A stale file usually means cortana was
    deliberately shut down, which is not itself an incident; the reader decides
    whether to grey the tile out. This module refuses to invent an outage out
    of an absence.
    """
    try:
        d = json.loads(STATE_FILE.read_text())
    except Exception:
        return {"worst": "unknown", "checks": [], "ts": 0, "stale": True}
    d["stale"] = (time.time() - float(d.get("ts") or 0)) > max(300.0, INTERVAL * 5)
    return d


# -- alerting: the whole point ----------------------------------------------
def alerts(now=None):
    """Lines worth interrupting for, since the last thing we said.

    Returns [(key, urgency, line)]. Delivery is NOT done here: notify.deliver
    needs presence, and presence is only knowable inside the cortana process.
    Keeping this a pure diff is what lets the tests pin it and lets the routines
    engine read the same signal without a second copy of the policy.

    Only DEGRADATIONS speak. A row that is still bad an hour later is the same
    news, and repeating it every minute is how a user learns to ignore a voice.
    """
    now = time.time() if now is None else now
    _load_alerted()
    out, changed = [], False
    for key, label, _every, _fn in _CHECKS:
        cur = _cache.get(key)
        if not cur:
            continue
        state = cur["state"]
        was = _alerted.get(key) or {"state": "ok", "ts": 0.0}
        rank, was_rank = _RANK.get(state, 0), _RANK.get(was["state"], 0)
        if rank > was_rank or (rank >= 2 and (now - was["ts"]) >= REALERT):
            out.append((key, "urgent" if rank >= 2 else "normal",
                        f"{label}: {cur['detail']}"))
            _alerted[key] = {"state": state, "ts": now}
            changed = True
        elif rank < was_rank:
            if state == "ok":
                # Recovery is ambient on purpose: worth seeing on the board,
                # never worth speaking over whatever the user is doing.
                out.append((key, "ambient", f"{label} is back to normal."))
            # `unknown` lands here too, because it ranks with `ok` so that it
            # can never raise an alarm - but it is NOT evidence the fault
            # cleared. Announcing "Memory is back to normal." because
            # /proc/meminfo stopped being readable is the install-spotifyd
            # mistake with a voice attached: a probe that changed context
            # reported as a measurement. So the ledger forgets the old state
            # silently, and the fault alerts again if it comes back.
            _alerted[key] = {"state": state, "ts": now}
            changed = True
    if changed:
        # `changed`, not `out`: the unknown branch above moves the ledger
        # without speaking, and a ledger move that is not persisted comes back
        # after the next restart and re-speaks what was already said.
        _save_alerted()
    return out


def _load_alerted():
    """Restore what the user was already told, once per process.

    This is the difference between an alert and an alarm loop. `_alerted` lives
    in memory, cortana.service is Restart=always, and a bad row plus a crash
    loop would otherwise mean the same urgent line spoken on every single
    respawn - five seconds apart, forever. Surviving the restart is the point.
    """
    if _alerted_loaded["done"]:
        return
    _alerted_loaded["done"] = True
    try:
        import memory
        data = json.loads(memory.meta_get(_ALERTED_KEY, "{}"))
    except Exception:
        return                 # no db yet is not a reason to stop alerting
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and "state" in v:
                _alerted[k] = {"state": v.get("state"), "ts": float(v.get("ts") or 0)}


def _save_alerted():
    try:
        import memory
        memory.meta_set(_ALERTED_KEY, json.dumps(_alerted))
    except Exception:
        pass                   # an audit write must never swallow a delivery


def speakable():
    """One or two prose sentences for the system_check tool. Not a list, not
    markdown, no URLs - this goes through TTS."""
    snap = snapshot()
    bad = [c for c in snap["checks"] if c["state"] == "bad"]
    warn = [c for c in snap["checks"] if c["state"] == "warn"]
    if not snap["checks"]:
        return "I have not run the system checks yet."
    if not bad and not warn:
        return "Everything checks out."
    # Sentence-end punctuation is forced rather than assumed: these strings are
    # concatenated and read aloud, and TTS runs two details together into one
    # unparseable sentence when the first one does not stop.
    parts = []
    for c in (bad + warn)[:3]:
        d = str(c["detail"]).strip()
        parts.append(d if d.endswith((".", "!", "?")) else d + ".")
    return " ".join(parts)
