"""Phase 2 of the scheduler: the tick, in the bridge process.

Why here and not in main.py: cortana.service is DESIGNED to be absent. A spoken
"shut down" exits 42, RestartPreventExitStatus=42 keeps her off, and that is a
feature. cortana-bridge.service is Restart=always. A 7am alarm has to outlive
"stop listening to me", so the ticker belongs to the process that is always
there.

Running both tickers during the cutover is safe by construction rather than by
convention: schedule.claim() is a conditional UPDATE and sqlite serialises
writers, so exactly one process can ever win an occurrence. Set
BRIDGE_SCHEDULER=0 in .env.local to back this out without a code change.

This process has no speaker and does not own hud_state.json, so its notify legs
are what it can actually reach:

    phone -> hub.announce, the WebSocket, with urgency in the frame
    desk  -> speech_inbox.json, drained and spoken by cortana when she is up
    board -> the same inbox; she owns the HUD file, so she applies it

The desk and board legs therefore mean "queued for her", not "said out loud".
That is recorded honestly in `deliveries` as desk/board because the routing
decision was made and acted on here; whether she was up to speak it is the
inbox's business, and inbox.py drops anything too old to be worth hearing.
"""
import threading
import time

import notify
import schedule
from bridge import brain, hub, inbox
from bridge.settings import log

OWNER = "bridge"

# Reminders replayed to a phone that has been away. hub._announces is an
# in-memory deque - the ids survive a restart (announce_seq.json) but the TEXT
# does not, and this unit is Restart=always. Losing "task finished" that way is
# a shrug; losing "your flight is in an hour" is not, and a scheduled item has a
# durable record of its own to rebuild from.
REPLAY_WINDOW = 6 * 3600     # older than this is news nobody wants at 2am
REPLAY_MAX = 5               # a reconnect is never a monologue
REPLAY_SETTLE = 60           # leave the last minute to live delivery
MARK_PREFIX = "phone_sched_seen:"

# Anything that fired since THIS process started is already in hub._announces,
# and the hello handler sends that deque first. Replaying it from `schedules`
# too delivered the same reminder twice on one reconnect, worded differently -
# the exact duplicate this mechanism exists to prevent, arriving by the other
# route. REPLAY_SETTLE alone did not cover it: the deque holds 50 items with no
# age limit, so the overlap is hours wide, not one minute.
#
# The deque is lost on restart and this unit is Restart=always, so "fired before
# we started" is precisely the set the deque cannot cover, and it is what the
# durable record exists to rebuild.
_STARTED = time.time()


def register_legs():
    """Tell notify which surfaces THIS process can reach.

    Split out of start() because it must happen even with BRIDGE_SCHEDULER=0.
    The phone leg belongs to this process and nowhere else - it is the only one
    holding the WebSocket - so hanging it off the scheduler switch meant backing
    the tick out also silently un-registered the phone, and every notify.deliver
    made anywhere in this process (a routine, a completion) recorded
    phone:unavailable while a phone was sitting there connected.
    """
    notify.register(
        phone=lambda text, urgency: hub.announce(text, urgency),
        desk=lambda text, urgency: inbox.put("desk", text, urgency),
        # The HUD line is a 120-char status crumb, same cap main.py uses for
        # its board leg - the inbox is not a place to park an essay.
        board=lambda text, urgency: inbox.put("board", text[:120], urgency))


def start():
    """Register the legs this process can serve, then tick forever."""
    register_legs()
    threading.Thread(target=_loop, daemon=True, name="bridge-scheduler").start()


def _loop():
    import config
    try:
        import memory
        memory.init()          # idempotent; the bridge may well start first
    except Exception as e:
        log("scheduler could not open state.db", e)
        return
    try:
        recovered = schedule.recover()
        if recovered:
            log(f"recovered {recovered} scheduled item(s) stranded mid-fire")
    except Exception as e:
        log("schedule recover failed", e)
    tick_seconds = max(1.0, float(config.SCHED_TICK))
    while True:
        try:
            schedule.tick(fire, owner=OWNER)
        except Exception as e:
            # One bad row must never end the thread: this is the only ticker
            # once the cutover finishes, and a dead one is silent.
            log("scheduler tick failed", e)
        time.sleep(tick_seconds)


def fire(row, fire_ts):
    """Run one due occurrence. Runs on the scheduler thread, never the loop.

    Mirrors main.py's _fire so behaviour does not depend on which process won
    the claim - the only difference is that a turn goes through brain.run_prompt
    (which clears the restart/shutdown flags) instead of orchestrator directly.
    """
    action, payload = row["action"], row["payload"]
    src, ref = f"schedule:{row['id']}", row["id"]

    if action == "delegate":
        state = brain.load()
        if not state["ready"]:
            notify.deliver(f"A scheduled task could not start: {state['error']}",
                           "normal", src=src, ref=ref)
            return
        _memory, orchestrator, _stt = state["mods"]
        import tasks
        msg = tasks.start(payload["agent"], payload["task"],
                          runner=lambda a, t, c: orchestrator.run_agent(a, t, cancel=c))
        # tasks.start returns a line meant for the lead to relay. With no user
        # in the loop it has nowhere to go, so a routine firing into a full slot
        # table would vanish; surface refusals the same way main.py does.
        if msg and msg.lstrip().lower().startswith(("all ", "can't", "cannot")):
            notify.deliver(msg, "normal", src=src, ref=ref)
        return

    if action == "turn":
        # Same readiness check the delegate branch does. Without it a scheduled
        # turn on a box with no API key logged one line and delivered NOTHING -
        # the row still advanced to 'delivered', so the audit said it went out
        # and the user's 7am briefing simply never happened.
        state = brain.load()
        if not state["ready"]:
            notify.deliver(f"A scheduled item could not run: {state['error']}",
                           row["urgency"], src=src, ref=ref)
            return
        reply = brain.run_prompt(payload.get("prompt") or row["title"],
                                 source=src)
        if reply:
            notify.deliver(reply, row["urgency"], src=src, ref=ref)
        else:
            # An empty reply is legitimate (a tool-only turn), but it is also
            # what a wedged turn lock looks like, and the deliveries row would
            # be identical either way. Say which in the journal.
            log(f"{src}: the turn produced nothing to deliver")
        return

    text = payload.get("text") or row["title"]
    if time.time() - fire_ts > 120:
        text = f"This was due at {schedule.to_local(fire_ts):%H:%M}. {text}"
    notify.deliver(text, row["urgency"], src=src, ref=ref)


# -- reconnect replay ------------------------------------------------------
def replay_for(ident, now=None):
    """Reminders that fired while THIS phone was away, rebuilt from `schedules`.

    Returns recorded announcement items for the caller to send to that one
    socket. Blocking sqlite - call it in a thread.

    Exactly-once per device: the high-water mark is the newest fired_ts already
    replayed to this ident, kept in memory.meta. Never kv - recall_all() dumps
    every kv row into the system prompt on every turn, and a per-device cursor
    read aloud to the model is exactly the kind of clutter meta exists to avoid.

    A device seen for the first time gets its mark set to now and nothing
    replayed: an unknown mark means we have no idea what it has already heard,
    and the safe reading of that is "you are current", not "here is your
    afternoon".
    """
    import memory
    now = now or time.time()
    ident = str(ident or "")
    if not ident:
        return []
    key = MARK_PREFIX + ident[:32]
    raw = memory.meta_get(key, "")
    if not raw:
        memory.meta_set(key, repr(now))
        return []
    try:
        mark = float(raw)
    except (TypeError, ValueError):
        mark = now

    con = memory.connect()
    try:
        rows = con.execute(
            # state is NOT the delivery test. A RECURRING row goes back to
            # 'pending' the moment advance() arms the next occurrence, so
            # filtering on ('delivered','firing') silently excluded every
            # repeating alarm - the one kind of reminder people actually rely
            # on - while passing every one-shot test. And 'pending' on its own
            # is no better: claim() stamps fired_ts before roll_forward()
            # decides an occurrence is too late to speak, so a deliberately
            # SUPPRESSED 7am alarm looks identical to a delivered one.
            #
            # `deliveries` is the only record of what was actually routed, so
            # ask it. Matched on src rather than ref because routines write
            # their own row ids into the same ref column.
            "SELECT id, title, fired_ts, urgency FROM schedules"
            " WHERE fired_ts IS NOT NULL AND fired_ts > ? AND fired_ts > ?"
            "   AND fired_ts < ? AND fired_ts < ?"
            "   AND state NOT IN ('acked','cancelled')"
            "   AND urgency != 'ambient'"
            "   AND EXISTS (SELECT 1 FROM deliveries d"
            "                WHERE d.src = 'schedule:' || schedules.id"
            "                  AND d.ts >= schedules.fired_ts - 5)"
            " ORDER BY fired_ts LIMIT ?",
            (mark, now - REPLAY_WINDOW, now - REPLAY_SETTLE, _STARTED, REPLAY_MAX)
        ).fetchall()
    except Exception as e:
        log("reminder replay query failed", e)
        return []
    finally:
        con.close()

    items = []
    newest = mark
    for sid, title, fired_ts, urgency in rows:
        newest = max(newest, float(fired_ts or 0))
        when = schedule.to_local(fired_ts)
        # The row keeps the TITLE, not the sentence that was spoken - an
        # action='turn' reply was generated at fire time and is gone. The title
        # is what the user asked for, so it is the honest thing to repeat.
        # keep=False: this is addressed to ONE device. In the shared replay
        # ring it would be handed to the next phone that reconnects too.
        item = hub.record(f"While you were away, {when:%H:%M}: {title}",
                          urgency or "normal", keep=False)
        if item:
            items.append(item)
    if newest > mark:
        memory.meta_set(key, repr(newest))
    if items:
        log(f"replaying {len(items)} missed reminder(s) to {ident[:8]}")
    return items
