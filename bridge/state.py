"""The single JSON snapshot the phone renders.

Everything the Dusk dashboard shows, gathered from the same sources the
dashboard reads (hud_state.json, systemd, calendar_state.json, git, Spotify)
plus the board snapshot the MOBILE LINK module pushes over loopback. Each
source is TTL-cached and failure-tolerant: one broken reader degrades its own
section, never the whole feed.
"""
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


def queue_task_op(op):
    with _task_lock:
        _task_ops.append(op)
        del _task_ops[:-200]


def drain_task_ops():
    with _task_lock:
        ops, _task_ops[:] = list(_task_ops), []
        return ops


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
        "spotify": util.cached("spotify", 8, spotify_link.state),
        "board": _board["data"],
        "boardTs": _board["ts"],
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
                log("calendar refresh failed", e)
            finally:
                _cal_refresh["busy"] = False

        threading.Thread(target=go, daemon=True, name="cal-refresh").start()
    return data
