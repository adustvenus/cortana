"""The single JSON snapshot the phone renders.

Everything the Dusk dashboard shows, gathered from the same sources the
dashboard reads (hud_state.json, systemd, calendar_state.json, git, Spotify)
plus the board snapshot the MOBILE LINK module pushes over loopback. Each
source is TTL-cached and failure-tolerant: one broken reader degrades its own
section, never the whole feed.
"""
import re
import socket
import threading
import time

import calendar_state
import hud_state
from bridge import brain, hub, pairing, spotify_link, updates, util
from bridge.settings import BRIDGE_VERSION, HOST_NAME, PORT, ROOT, log

# Latest board snapshot from the dashboard module: module order, tasks, and the
# weather ZIP - data that lives only in the dashboard page's localStorage and
# would otherwise be invisible to the phone.
_board = {"data": None, "ts": 0.0}

# Task edits from the phone, waiting for the dashboard to drain and apply
# (the dashboard page owns the task list). Bounded so an absent dashboard
# can't grow this without limit.
_task_ops = []
_task_lock = threading.Lock()


def set_board(snapshot):
    _board["data"] = snapshot
    _board["ts"] = time.time()


# The dashboard's colour tokens, carried inside the board snapshot. Kept
# separately as well as inside `board` so the phone has one stable place to look
# and, more importantly, so the LAST GOOD palette survives a board snapshot that
# arrives without one - otherwise closing the dashboard would strip the phone
# back to its built-in colours, which looks exactly like the theme breaking.
_theme = {"data": None, "ts": 0.0}

_THEME_KEYS = frozenset((
    "--bg-rgb", "--surface-rgb", "--surface2-rgb", "--border-rgb",
    "--panel-rgb", "--hairline-rgb", "--text-rgb", "--text-dim-rgb",
    "--accent-rgb", "--accent2-rgb", "--accent3-rgb", "--peach-rgb",
    "--orb-hi-rgb", "--orb-mid-rgb", "--orb-rgb",
))


# Deliberately a regex and not int(): int() also accepts " 2", "+2" and "1_0"
# (which is TEN). Dashboard/app/main.js guards the same payload with exactly
# this shape, and two validators that disagree are how you end up with a phone
# that is themed and a bubble orb that is not.
_RGB_TRIPLE = re.compile(r"^\d{1,3},\d{1,3},\d{1,3}$")


def _clean_theme(raw):
    """Known tokens holding a literal 'r,g,b' triple, nothing else. The phone
    parses these into colour ints; anything malformed would either crash the
    parse or paint an invisible UI on a device with no console to read."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, value in raw.items():
        if key not in _THEME_KEYS or not isinstance(value, str):
            continue
        if not _RGB_TRIPLE.match(value):
            continue
        nums = [int(p) for p in value.split(",")]
        if all(0 <= n <= 255 for n in nums):
            out[key] = "%d,%d,%d" % tuple(nums)
    return out or None


def set_theme(raw):
    cleaned = _clean_theme(raw)
    if cleaned:
        _theme["data"] = cleaned
        _theme["ts"] = time.time()


def queue_task_op(op):
    with _task_lock:
        _task_ops.append(op)
        del _task_ops[:-200]


def drain_task_ops():
    with _task_lock:
        ops, _task_ops[:] = list(_task_ops), []
        return ops


def board_is_fresh(max_age=90):
    """True when the dashboard pushed a snapshot recently - i.e. it is open and
    will actually apply queued edits. The phone uses this to warn instead of
    leaving a change pending forever."""
    return bool(_board["data"]) and (time.time() - _board["ts"]) < max_age


def cortana_state():
    """Orb state plus liveness. The unit state is authoritative for 'is she
    running' because hud_state.json intentionally stops being rewritten while
    she idles."""
    st = hud_state.read_state()
    service = util.cached("svc", 5, lambda: util.run(
        ["systemctl", "--user", "is-active", "cortana"]) or "unknown")
    ts = float(st.get("ts") or 0)
    age = time.time() - ts if ts else 1e9
    return {"state": st.get("state", "offline"), "agent": st.get("agent", ""),
            "detail": st.get("detail", ""), "mode": st.get("mode", ""),
            "thoughts": st.get("thoughts", [])[-6:], "ts": ts,
            "service": service, "stale": age > 600, "fresh": age < 10}


def git_state():
    """Branch, cleanliness and recent commits of the Cortana checkout - worth
    surfacing because Cortana edits her own source."""
    def read():
        log = util.run(["git", "-C", str(ROOT), "log", "--oneline", "-5"])
        status = util.run(["git", "-C", str(ROOT), "status", "--short"])
        branch = util.run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"])
        return {"branch": branch or "unknown",
                "clean": not status,
                "files": len(status.split("\n")) if status else 0,
                "log": [{"hash": l[:7], "msg": l[8:]} for l in log.split("\n") if l]}
    return util.cached("git", 30, read)


def all_addresses():
    """Every address a phone might reach us on, Tailscale first. The app stores
    these and fails over between them, so a phone paired on the LAN keeps
    working when it leaves the house, and vice versa."""
    def read():
        found = []
        try:
            for line in util.run(["tailscale", "ip", "-4"], timeout=8).splitlines():
                ip = line.strip()
                if ip:
                    found.append(ip)
        except Exception:
            pass
        lan = lan_ip()
        if lan and lan not in found:
            found.append(lan)
        return found
    return util.cached("addresses", 120, read)


def lan_ip():
    """This machine's LAN address, or None. No traffic is sent - connecting a
    UDP socket just asks the kernel which interface would be used."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def reachable_ip():
    """Best single address to bake into a QR: Tailscale if present, else LAN."""
    def read():
        addresses = all_addresses()
        return addresses[0] if addresses else "127.0.0.1"
    return util.cached("reach_ip", 60, read)


def _upcoming():
    """Next few scheduled items. Lazy import and failure-tolerant like every
    other reader here: state.db can be mid-migration or absent on a fresh box,
    and that must degrade this one field, never the whole feed."""
    try:
        import schedule
        return schedule.upcoming(6)
    except Exception as e:
        log("schedule read failed", e)
        return []


def build():
    """The full snapshot pushed to phones and served by /api/state."""
    return {
        "type": "state",
        "host": HOST_NAME,
        "addresses": all_addresses(),
        "port": PORT,
        "bridgeVersion": BRIDGE_VERSION,
        "apk": updates.apk_info(),
        "brainReady": brain.ready(),
        "brainError": brain.error(),
        "cortana": cortana_state(),
        "calendar": util.cached("cal", 15, calendar),
        "git": git_state(),
        "spotify": util.cached("spotify", 20, spotify_link.state),   # 20s: two pollers share one Spotify quota
        "schedule": util.cached("sched", 15, _upcoming),
        "board": _board["data"],
        "boardTs": _board["ts"],
        "theme": _theme["data"],
        "devices": pairing.devices(hub.online_idents()),
        "ts": time.time(),
    }


# Cortana refreshes the calendar every 10 minutes - long enough that a
# just-added or just-deleted event looks like a sync bug. When the file is
# older than this, kick a background refresh here too (same Google token,
# same writer), so anyone looking at the agenda sees at most ~2min of lag.
_CAL_MAX_AGE = 120
_cal_refresh = {"busy": False}


def calendar():
    data = calendar_state.read()
    ts = float(data.get("ts") or 0)
    if time.time() - ts > _CAL_MAX_AGE and not _cal_refresh["busy"]:
        _cal_refresh["busy"] = True

        def go():
            try:
                import config as cortana_config
                if cortana_config.GMAIL_TOKEN.exists():
                    from tools import calendar_tool
                    calendar_state.write(calendar_tool.today_events())
                    util.invalidate("cal")
            except Exception as e:
                from tools import google_auth
                if isinstance(e, google_auth.AuthExpired):
                    # Same rule as the desk loop: never keep serving a stale
                    # agenda behind an auth failure.
                    calendar_state.write_error_clearing(
                        "Google access expired - run: python main.py --google-auth")
                    util.invalidate("cal")
                log("calendar refresh failed", e)
            finally:
                _cal_refresh["busy"] = False

        threading.Thread(target=go, daemon=True, name="cal-refresh").start()
    return data
