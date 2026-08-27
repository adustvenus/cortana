"""Loopback client for the comms tools dispatched in agents.py.

The comms store and the phone command channel live in the bridge process, but
the tool that uses them runs wherever the orchestrator happens to be: the
cortana process at the desk, the BRIDGE process for a phone turn. Neither can
reach the other's memory and both can reach 127.0.0.1, so the tools go through
the same loopback API the dashboard uses.

Pure stdlib on purpose. agents.py imports this in a process that has no aiohttp
requirement, so nothing here may import aiohttp - directly or through a bridge
module that does.

Calling a bridge endpoint FROM the bridge (a phone turn) is not a deadlock: the
tool runs on a worker thread while the event loop is free to serve the request.
It is still a real HTTP round trip, which is why every call has a timeout and
every failure comes back as a sentence rather than an exception.
"""
import json
import urllib.error
import urllib.request

from bridge.settings import PORT

# Comfortably past cmdchan.TIMEOUT (20s), so a phone that never answers times
# out THERE, with its own explanatory sentence, rather than here as a socket
# timeout that says nothing useful.
TIMEOUT = 35


def call(method, path, body=None, timeout=TIMEOUT):
    """(data, error-sentence). Never raises."""
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}"), ""
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return payload, str(payload.get("error") or f"the bridge returned {e.code}")
    except Exception:
        # Almost always "the bridge is not running". Say that, not the errno -
        # this string is spoken aloud.
        return {}, ("The phone bridge is not answering, so I can't reach your "
                    "phone right now.")


def _ago(ts, now):
    mins = max(0, int((now - float(ts or 0)) / 60))
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} minutes ago"
    hours = mins // 60
    return "an hour ago" if hours == 1 else f"{hours} hours ago"


def comms_summary(kind="all", limit=8):
    """Spoken-style recap of what the phone mirrored. Prose, no lists."""
    import time
    data, err = call("GET", f"/local/comms?limit={int(limit)}", timeout=10)
    if err:
        return err
    now = time.time()
    sms = data.get("sms") or []
    notes = data.get("notes") or []
    lines = []
    if kind in ("all", "sms"):
        for m in sms[:limit]:
            who = m.get("from") or "unknown"
            lines.append(f"{'You texted' if who == 'me' else who} "
                         f"{_ago(m.get('ts'), now)}: {m.get('body', '')[:160]}")
    if kind in ("all", "notifications"):
        for n in notes[:limit]:
            lines.append(f"{n.get('app') or 'A notification'} "
                         f"{_ago(n.get('ts'), now)}: "
                         f"{(n.get('title') or '')} {(n.get('text') or '')}".strip()[:180])
    if not lines:
        return "Nothing has come through from your phone recently."
    return " ".join(lines)


def sms_send(to, body, confirm=False):
    """Compose or send. Returns the line to say back to the user.

    The refusal path is the important one: without a confirmation this ALWAYS
    returns a read-back and sends nothing, and the bridge enforces that
    independently rather than trusting the flag passed here.
    """
    data, err = call("POST", "/local/sms",
                     {"to": to, "body": body, "confirm": bool(confirm)})
    if err:
        return err
    if data.get("ok"):
        return f"Sent to {data.get('to')}."
    if data.get("staged"):
        return data.get("readback") or data.get("error") or "Nothing sent."
    return data.get("error") or "That message did not go out."
