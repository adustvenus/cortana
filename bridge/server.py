"""Cortana Mobile Bridge - the workstation end of the phone link.

Runs as its own systemd unit (cortana-bridge.service), independent of
cortana.service and the dashboard, so restarting any one of the three never
disturbs the others.

Module map:
    settings.py    constants + logging (leaf; imports nothing local)
    util.py        TTL cache + subprocess helpers
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

from aiohttp import web

from bridge import api_local, api_phone, hub, state
from bridge.settings import (BIND, BRIDGE_VERSION, HOST_NAME, MAX_UPLOAD, PORT,
                             PUSH_INTERVAL, log)


async def _push_loop():
    """Push a fresh snapshot to every connected phone, skipping identical
    payloads so an idle house costs nothing on the wire or the battery."""
    last = ""
    while True:
        await asyncio.sleep(PUSH_INTERVAL)
        if not hub.count():
            continue
        try:
            payload = json.dumps(await asyncio.to_thread(state.build))
        except Exception as e:
            log("state build failed", e)
            continue
        if payload == last:
            continue
        last = payload
        await hub.broadcast(payload)


def make_app():
    app = web.Application(client_max_size=MAX_UPLOAD)   # headroom for phone WAVs
    app.add_routes(api_phone.routes)
    app.add_routes(api_local.routes)

    async def on_start(app):
        hub.bind_loop(asyncio.get_running_loop())
        app["push"] = asyncio.create_task(_push_loop())

    async def on_stop(app):
        app["push"].cancel()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def main():
    log(f"v{BRIDGE_VERSION} listening on {BIND}:{PORT} as '{HOST_NAME}'")
    web.run_app(make_app(), host=BIND, port=PORT, print=None)


if __name__ == "__main__":
    main()
