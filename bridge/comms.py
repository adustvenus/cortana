"""Mirrored phone comms: notifications and SMS, plus the confirmed SMS send.

The phone posts what its notification listener and SMS provider saw; this keeps
a bounded window of it in sqlite so the assistant can answer "did anyone text
me" without the phone being reachable at that moment.

Three rules the rest of this file exists to enforce:

  * BOUNDED. This table is fed by a device that generates notifications all day
    and can re-sync its whole backlog after a reinstall. Every ingest prunes by
    count and by age, so the ceiling is a property of the code and not of the
    phone's behaviour.
  * IDEMPOTENT. A re-sync must not double every row. The phone's own id is the
    dedup key where it has one; where it does not (Android notifications often
    do not), the key is a hash of the content plus the minute, which also
    collapses the same notification being re-posted as it updates.
  * NEVER SENDS ON ITS OWN. Sending is a two-step handshake: compose, read
    back, and send only when the same text comes back confirmed. Same standing
    rule as Gmail ("drafts only, never send"), for the same reason - an
    outbound message is not undoable.

Message CONTENT lives here in plaintext, in state.db. That is a deliberate
trade for being able to answer questions about it locally, and it is why the
retention window is short and the body is capped.
"""
import asyncio
import hashlib
import sqlite3
import time

import memory
from bridge import cmdchan
from bridge.settings import log

MAX_ROWS = 500            # hard ceiling on stored rows
MAX_AGE = 14 * 86400      # and on how far back they go
BODY_CAP = 500            # a notification body is a preview, not a document
SYNC_CAP = 200            # rows accepted from one sync call
STAGE_TTL = 300           # a composed message may be confirmed for this long

_MISSING = ("the comms table is not in state.db yet - memory.init() needs the "
            "comms DDL from this build")

# The composed-but-unsent message. In memory ON PURPOSE: a confirmation belongs
# to the conversation that composed it, and a bridge restart in between should
# lose the draft rather than let a "yes" three hours later fire it off.
_staged = {"to": "", "body": "", "ts": 0.0}


# -- storage ---------------------------------------------------------------
def _ext_id(kind, item):
    """Dedup key. The phone's own id when it has one; otherwise the content
    plus the minute, which also collapses one notification being re-posted
    repeatedly as it updates (a download progress bar, a music player)."""
    given = str(item.get("id") or "").strip()
    if given:
        return given[:64]
    raw = "|".join(str(item.get(k) or "")
                   for k in ("app", "from", "title", "text", "body"))
    minute = int(float(item.get("ts") or time.time()) // 60)
    return hashlib.sha1(f"{kind}|{raw}|{minute}".encode()).hexdigest()[:32]


def _row(kind, item, now):
    ts = float(item.get("ts") or now)
    if ts > 1e11:                       # phones send milliseconds; normalise
        ts /= 1000.0
    body = str(item.get("body") or item.get("text") or "")[:BODY_CAP]
    addr = str(item.get("from") or item.get("sender") or "")
    # `sender` is what gets SPOKEN, so it prefers the contact name; `title`
    # keeps the raw address, which is what a reply has to be aimed at. Two
    # fields because collapsing them loses one of the two uses - and `title`
    # already held the counterparty address for outgoing messages, so an SMS
    # row now means the same thing in both directions.
    name = str(item.get("fromName") or "").strip()
    who = (name or addr)[:64]
    title = addr[:120] if kind == "sms" else str(item.get("title") or "")[:120]
    return (ts, kind,
            str(item.get("app") or "")[:64],
            who, title,
            body, _ext_id(kind, item),
            1 if item.get("unread") else 0)


# Why the phone last said it could not read SMS. Kept in memory only: it is a
# statement about the phone's CURRENT permission state, and a stale one read
# back after a reinstall would be worse than none.
_sms_error = {"text": "", "ts": 0.0}


def sms_error():
    return _sms_error["text"]


def ingest(payload, now=None):
    """Store one sync from the phone. Blocking sqlite - call it in a thread."""
    now = now or time.time()
    sms = payload.get("sms") if isinstance(payload, dict) else None
    notes = payload.get("notifications") if isinstance(payload, dict) else None
    # The phone deliberately sends smsError INSTEAD of an empty list when it
    # cannot read messages, because an empty list is a lie - it reads as "you
    # have no texts". Nothing here consumed it, so the one failure the phone
    # can actually explain arrived as silence and Cortana answered "nothing has
    # come through", which is the very sentence the key exists to prevent.
    err = payload.get("smsError") if isinstance(payload, dict) else None
    if err:
        _sms_error["text"] = str(err)[:200]
        _sms_error["ts"] = time.time()
        log("phone cannot read SMS", _sms_error["text"])
    elif isinstance(sms, list):
        _sms_error["text"], _sms_error["ts"] = "", 0.0
    rows = []
    for kind, items in (("sms", sms), ("note", notes)):
        if not isinstance(items, list):
            continue
        for item in items[:SYNC_CAP]:
            if isinstance(item, dict):
                rows.append(_row(kind, item, now))
    if not rows:
        return {"ok": True, "stored": 0, "seen": 0}
    con = memory.connect()
    try:
        before = con.total_changes
        con.executemany(
            "INSERT OR IGNORE INTO comms(ts, kind, app, sender, title, body,"
            " ext_id, unread) VALUES(?,?,?,?,?,?,?,?)", rows)
        stored = con.total_changes - before
        _prune(con, now)
        con.commit()
    except sqlite3.OperationalError as e:
        log("comms ingest failed", e)
        return {"ok": False,
                "error": _MISSING if "no such table" in str(e) else str(e)[:120]}
    finally:
        con.close()
    return {"ok": True, "stored": stored, "seen": len(rows)}


def _prune(con, now=None):
    """Caller commits. Age first, then the count ceiling - in that order, so a
    quiet fortnight cannot leave 500 ancient rows pinned in place."""
    now = now or time.time()
    con.execute("DELETE FROM comms WHERE ts < ?", (now - MAX_AGE,))
    con.execute("DELETE FROM comms WHERE id NOT IN"
                " (SELECT id FROM comms ORDER BY ts DESC, id DESC LIMIT ?)",
                (MAX_ROWS,))


def recent(kind=None, limit=30):
    """Newest first. Returns [] and logs if the table is not there yet - a
    dashboard poller must never see a 500 because a migration is pending."""
    limit = max(1, min(int(limit or 30), 100))
    con = memory.connect()
    try:
        if kind:
            rows = con.execute(
                "SELECT id, ts, kind, app, sender, title, body, unread FROM comms"
                " WHERE kind=? ORDER BY ts DESC LIMIT ?",
                (kind, limit)).fetchall()
        else:
            rows = con.execute(
                "SELECT id, ts, kind, app, sender, title, body, unread FROM comms"
                " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.OperationalError as e:
        log("comms read failed", e)
        return []
    finally:
        con.close()
    return [{"id": r[0], "ts": r[1], "kind": r[2], "app": r[3], "from": r[4],
             "title": r[5], "body": r[6], "unread": bool(r[7])} for r in rows]


def local_view(limit=30):
    """The /local/comms contract shape. Outgoing messages appear in the same
    list with from='me', because a thread reads as a conversation and not as
    two tables."""
    # 'me' is written by _store_outgoing and by nothing else. Defaulting an
    # EMPTY sender to it read an incoming text from a withheld or short-code
    # number back as one the user had sent - "You texted just now: your code is
    # 447122" - which is worse than admitting we do not know who it was from.
    # `from` is the name when the phone knew one, and `number` is always the
    # raw address - so "who texted" can say a person and a reply can still be
    # aimed at a handset.
    sms = [{"id": r["id"], "from": r["from"] or "unknown",
            "number": r.get("title") or "", "body": r["body"],
            "ts": r["ts"], "unread": r["unread"]}
           for r in recent("sms", limit)]
    notes = [{"app": r["app"], "title": r["title"], "text": r["body"],
              "ts": r["ts"]} for r in recent("note", limit)]
    # Carried so the caller can say WHY there is nothing, instead of "nothing
    # has come through" - which is indistinguishable from an empty inbox and is
    # the exact sentence the phone sends smsError to prevent.
    err = sms_error()
    return {"sms": sms, "notes": notes}


def _store_outgoing(to, body, now=None):
    now = now or time.time()
    con = memory.connect()
    try:
        con.execute(
            "INSERT OR IGNORE INTO comms(ts, kind, app, sender, title, body,"
            " ext_id, unread) VALUES(?,?,?,?,?,?,?,0)",
            (now, "sms", "", "me", str(to)[:120], str(body)[:BODY_CAP],
             hashlib.sha1(f"out|{to}|{body}|{now}".encode()).hexdigest()[:32]))
        con.commit()
    except sqlite3.OperationalError as e:
        log("outgoing sms not recorded", e)
    finally:
        con.close()


# -- sending: compose, read back, confirm ----------------------------------
def _norm(text):
    return " ".join(str(text or "").split())


def stage(to, body, now=None):
    """Hold a composed message and return the line to read back to the user."""
    _staged.update(to=_norm(to), body=_norm(body), ts=now or time.time())
    # Asked as a question, not as a script. "Say send it and I will" reads as
    # an instruction that only those three words satisfy, and a user who
    # answered "yes" or "go ahead" - the two most natural replies to a
    # read-back - found nothing happened. What guarantees safety here is the
    # exact to/body match in check_confirmed(), NOT the wording of the yes.
    return f"Ready to text {_staged['to']}: {_staged['body']}. Send it?"


def clear_staged():
    _staged.update(to="", body="", ts=0.0)


def check_confirmed(to, body, now=None):
    """(ok, error). Refuses anything but the exact text that was read back.

    Matching the TEXT rather than trusting a confirm flag is the whole point: it
    makes "compose one message, confirm a different one" structurally
    impossible rather than merely unlikely, and a model that skips the read-back
    finds there is nothing staged to confirm.
    """
    now = now or time.time()
    if not _staged["ts"]:
        return False, ("Nothing is composed yet. I will read the message back "
                       "first and send it once you say so.")
    if now - _staged["ts"] > STAGE_TTL:
        clear_staged()
        return False, ("That draft is too old to send on. I will read it back "
                       "again first.")
    if _norm(to) != _staged["to"] or _norm(body) != _staged["body"]:
        # The mismatch path must NOT arm the new text. Re-staging it here meant
        # the identical confirmed call, repeated - which is exactly what a
        # retrying model does - sent a message no human had ever heard read
        # back: refuse, stage, retry, send. The draft is dropped instead, so
        # the only route to a send stays compose -> read back -> confirm.
        clear_staged()
        return False, ("That is not the message I read back, so nothing went "
                       "out. Compose it again and I will read the new one back "
                       "before it sends.")
    return True, ""


async def read_now(limit=10):
    """Ask the phone for its inbox RIGHT NOW, and fold the answer into the store.

    Until this existed, "what was my last text" could only be as good as
    whatever the phone's 15-minute alarm last happened to push - so a message
    from ten minutes ago was invisible while one from three days ago was
    reported as the latest. A stale answer delivered confidently is worse than
    no answer: nothing about "Mum, 77 hours ago" tells you it is not current.

    The phone has implemented the sms.read command since v2.5.0; there was
    simply no caller. Returns the number of rows stored, or None when the phone
    cannot be reached - in which case the caller falls back to the cache, which
    is still the right thing to say when the phone is off.
    """
    reply = await cmdchan.request("sms.read", {"limit": int(limit)})
    if reply.get("error") or not reply.get("ok"):
        return None
    rows = reply.get("result")
    if not isinstance(rows, list):
        return None
    return ingest({"sms": rows}).get("stored", 0)


async def send(to, body, confirm=False, now=None):
    """The whole outbound path. Returns a dict carrying either ok or error.

    Never sends on the first call, whatever `confirm` says, unless the exact
    text was staged by an earlier one.
    """
    to, body = _norm(to), _norm(body)
    if not to or not body:
        return {"ok": False,
                "error": "A text message needs both a recipient and something to say."}
    if not confirm:
        return {"ok": False, "staged": True, "readback": stage(to, body, now)}
    good, err = check_confirmed(to, body, now)
    if not good:
        return {"ok": False, "staged": True, "error": err}

    reply = await cmdchan.request("sms.send", {"to": to, "body": body})
    if reply.get("error"):
        # Deliberately NOT retried. A timeout can mean the message went out and
        # only the confirmation was lost; sending again to be sure is how one
        # message becomes two.
        return {"ok": False, "error": reply["error"] +
                ". I did not try again - it may have gone out anyway."}
    if not reply.get("ok"):
        return {"ok": False,
                "error": str(reply.get("phoneError")
                             or "the phone refused to send it")[:160]}
    clear_staged()
    # In a thread: this coroutine runs ON the aiohttp event loop (it has to -
    # cmdchan resolves a future there), and a sqlite write from the loop stalls
    # every other phone and dashboard poller for the duration.
    await asyncio.to_thread(_store_outgoing, to, body, now)
    return {"ok": True, "to": to, "body": body}
