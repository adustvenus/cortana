"""Cortana Mobile Bridge - the workstation end of the phone link.

Runs as its own systemd unit (cortana-bridge.service), independent of
cortana.service and the dashboard, so restarting any one of the three never
disturbs the others.

Module map:
    settings.py    constants + logging (leaf; imports nothing local)
    util.py        TTL cache + subprocess helpers
    cmdchan.py     workstation -> phone commands ("cmd" frames + REST replies)
    comms.py       mirrored SMS/notifications; the confirmed SMS send
    inbox.py       speech_inbox.json, this process's desk leg
    presence_link.py  desk + phone + socket, merged into one presence view
    scheduler.py   the scheduler tick, and the reminder replay on reconnect
    watch.py       read-only views of routines / sentinel_state.json
    client.py      loopback client the agents.py comms tools call
    pairing.py     pairing codes, device tokens (hashed at rest)
    auth.py        per-request identity: phone token, or loopback dashboard
    hub.py         connected phones; broadcast + announcements
    state.py       the JSON snapshot phones render
    brain.py       Cortana's real pipeline for phone-initiated turns
    voice.py       her voice, streamed back to the phone
    spotify_link.py transport control via the dashboard's own Spotify grant
    updates.py     APK publishing, git refresh, adb push
    onboarding.py  the QR install page
    api_phone.py   /api/*  (token required)
    api_local.py   /local/* (loopback) and /get (tailnet/LAN)
    server.py      this file: wiring, push loop, entrypoint

Reach: designed for Tailscale, with the LAN as a second address. Bind defaults
to 0.0.0.0 so both work; every /api/* call requires a device token and /local/*
answers loopback only. Never port-forward this service.

Run: venv/bin/python -m bridge.server
"""
import asyncio
import json
import time

from aiohttp import web

from bridge import api_local, api_phone, hub, scheduler, state, util
from bridge.settings import (BIND, BRIDGE_VERSION, HOST_NAME, MAX_UPLOAD, PORT,
                             PUSH_FLOOR, PUSH_INTERVAL, SCHEDULER, log)


async def _push_loop():
    """Push a fresh snapshot to every connected phone, deduped with a floor.

    The dedup here used to be unconditional, and was dead code: state.build()
    stamps a fresh `ts` on every call, so no two payloads ever compared equal
    and the old "an idle house costs nothing" claim was false - a full snapshot
    went out every 1.5s forever, to every phone.

    It cannot simply be repaired either. The app runs its pending-edit
    reconciliation only on an incoming state frame, so a perfectly silent idle
    board would leave a phone's task edits pending with no timeout and no
    warning. Hence PUSH_FLOOR: dedupe the chatter, never go quiet.
    """
    last_key, last_push = "", 0.0
    while True:
        await asyncio.sleep(PUSH_INTERVAL)
        if not hub.count():
            continue
        try:
            snap = await asyncio.to_thread(state.build)
            key = util.dedup_key(snap)
            payload = json.dumps(snap)
        except Exception as e:
            log("state build failed", e)
            continue
        now = time.monotonic()
        if key == last_key and now - last_push < PUSH_FLOOR:
            continue
        last_key, last_push = key, now
        # Guarded: hub.send swallows per-socket failures, but an unexpected
        # raise here would end this task permanently and silently - phones keep
        # a live socket and simply stop receiving state. One bad push is worth
        # a log line, never the loop.
        try:
            await hub.broadcast(payload)
        except Exception as e:
            log("broadcast failed", e)


def make_app():
    app = web.Application(client_max_size=MAX_UPLOAD)   # headroom for phone WAVs
    app.add_routes(api_phone.routes)
    app.add_routes(api_local.routes)

    async def on_start(app):
        hub.bind_loop(asyncio.get_running_loop())
        app["push"] = asyncio.create_task(_push_loop())
        # The phone leg of notify belongs to this process and nowhere else - it
        # is the only one holding the WebSocket - so registering it is what
        # turns "your flight is in an hour" from an audit row reading
        # phone:unavailable into a buzz in someone's pocket. That is true
        # whether or not this process owns the TICK, which is why it is not
        # behind the switch: BRIDGE_SCHEDULER=0 backs out the tick, not the
        # phone. register_legs() also claims desk and board (both go to
        # speech_inbox.json, which this process owns).
        scheduler.register_legs()
        if SCHEDULER:
            # The tick touches sqlite, so it runs on its own thread and nothing
            # it does happens on this loop.
            scheduler.start()
        else:
            log("scheduler tick disabled by BRIDGE_SCHEDULER=0")

    async def on_stop(app):
        # .get, not [] - on_start raising before it stored the task would make
        # cleanup raise KeyError on top of the real error and bury it.
        task = app.get("push")
        if task is not None:
            task.cancel()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def main():
    log(f"v{BRIDGE_VERSION} listening on {BIND}:{PORT} as '{HOST_NAME}'")
    # Bound the graceful phase: a restart should be quick even if a phone
    # request is mid-flight. The turn itself runs on a daemon thread, so
    # nothing here waits on Cortana finishing a long job.
    web.run_app(make_app(), host=BIND, port=PORT, print=None,
                shutdown_timeout=5)


if __name__ == "__main__":
    main()
