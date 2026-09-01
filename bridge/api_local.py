"""/local/* - the Dusk dashboard's MOBILE LINK module, and /get - the phone
onboarding page a scanned QR opens.

/local is loopback-only: it issues pairing codes, revokes devices, and receives
the board snapshot. /get is deliberately open but reachable only over the
tailnet or LAN; see onboarding.py for why that is safe.
"""
import asyncio
import json

from aiohttp import web

from bridge import (auth, comms, hub, onboarding, pairing, presence_link,
                    state, updates, watch)
from bridge.settings import (BRIDGE_VERSION, HOST_NAME, MAX_BOARD_SNAPSHOT, PORT)


# ── onboarding (open, tailnet/LAN only) ─────────────────────────────────────
async def get_page(request):
    code = onboarding.valid_code(request.query.get("c", ""))
    host = request.host.split(":")[0]     # the address the phone actually used
    return web.Response(text=onboarding.install_page(code, host),
                        content_type="text/html")


async def get_apk(request):
    path = updates.apk_path()
    if path is None:
        return web.json_response({"error": "no APK built yet"}, status=404)
    return web.FileResponse(path, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Disposition": 'attachment; filename="cortana-mobile.apk"'})


# ── dashboard module (loopback only) ────────────────────────────────────────
async def status(request):
    denied = auth.local_guard(request)
    if denied:
        return denied
    return web.json_response({
        "bridge": "up", "version": BRIDGE_VERSION, "host": HOST_NAME,
        "port": PORT, "devices": pairing.devices(hub.online_idents()),
        "pairing": pairing.pair_info(), "lockedFor": int(pairing.locked_for()),
        "phonesConnected": hub.count(),
        "apk": updates.apk_info(),      # per-device up-to-date/available labels
    }, headers=auth.CORS)


async def pair_new(request):
    denied = auth.local_guard(request)
    if denied:
        return denied
    return web.json_response(pairing.new_code(), headers=auth.CORS)


async def revoke(request):
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400, headers=auth.CORS)
    # Prefer the per-entry id; `name` is accepted for older dashboards, where
    # it means "the first entry with this name", not "all of them".
    ident = body.get("id") or body.get("name", "")
    return web.json_response({"revoked": pairing.revoke(str(ident))},
                             headers=auth.CORS)


async def qr(request):
    """QR for the dashboard module: the /get URL carrying the ACTIVE pairing
    code. It exists only while a code is live, so scanning it is exactly as
    privileged as reading the code printed beside it."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    info = pairing.pair_info()
    if not info:
        return web.json_response({"url": None}, headers=auth.CORS)
    url = f"http://{state.reachable_ip()}:{PORT}/get?c={info['code']}"
    matrix, err = onboarding.qr_matrix(url)
    if matrix is None:
        return web.json_response({"url": url, "error": err}, headers=auth.CORS)
    return web.json_response({"url": url, "matrix": matrix}, headers=auth.CORS)


async def board(request):
    """Board snapshot from the dashboard: module order, tasks, weather ZIP -
    state that lives only in the dashboard page's localStorage."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except Exception:
        return web.json_response({"error": "bad json"}, status=400, headers=auth.CORS)
    if len(json.dumps(body)) > MAX_BOARD_SNAPSHOT:
        return web.json_response({"error": "snapshot too large"}, status=413,
                                 headers=auth.CORS)
    state.set_board(body)
    # Colour tokens ride along in the snapshot. Lifted out and kept separately
    # so the phone reads them from one stable place, and so a snapshot that
    # arrives without them leaves the last good palette standing.
    state.set_theme(body.get("theme"))
    return web.json_response({"ok": True}, headers=auth.CORS)


async def tasks_ops(request):
    """Drain the queue of task edits made on phones; the dashboard applies
    them to its own list and pushes the board back."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    return web.json_response({"ops": state.drain_task_ops()}, headers=auth.CORS)


async def upcoming(request):
    """Next few scheduled items for the dashboard's UPCOMING tile.

    A dedicated route rather than another field on /local/status: that one is
    polled every 5s by the MOBILE LINK module and carries the whole device
    list, and the schedule changes far too rarely to ride along with it.
    """
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        import schedule
        items = schedule.upcoming(6)
    except Exception as e:
        # Never 500 a dashboard poller - the tile shows its last good value and
        # the reason lands in the journal.
        return web.json_response({"items": [], "error": str(e)[:200]},
                                 headers=auth.CORS)
    return web.json_response({"items": items}, headers=auth.CORS)


async def presence(request):
    """Merged presence: the desk half cortana writes, the phone half the app
    posts, and whether a socket is open right now."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        data = await asyncio.to_thread(presence_link.snapshot)
    except Exception as e:
        return web.json_response({"desk": "unknown", "phone": "closed",
                                  "place": "unknown", "error": str(e)[:200]},
                                 headers=auth.CORS)
    return web.json_response(data, headers=auth.CORS)


async def comms_view(request):
    """Recent mirrored SMS and notifications for the dashboard."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        limit = int(request.query.get("limit", "30"))
    except (TypeError, ValueError):
        limit = 30
    try:
        data = await asyncio.to_thread(comms.local_view, limit)
    except Exception as e:
        return web.json_response({"sms": [], "notes": [], "error": str(e)[:200]},
                                 headers=auth.CORS)
    return web.json_response(data, headers=auth.CORS)


async def sms(request):
    """Compose or send one text message, for the tool in agents.py.

    Loopback only, and the send is refused unless the EXACT text staged by an
    earlier call comes back confirmed. Standing rule, same as Gmail: composed
    here, sent only on an explicit yes.
    """
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except Exception:
        # A bare list or string parses fine and then AttributeErrors on .get,
        # which aiohttp turns into a 500 with a traceback in the journal. A
        # 400 says what is actually wrong.
        return web.json_response({"error": "bad json"}, status=400,
                                 headers=auth.CORS)
    result = await comms.send(body.get("to"), body.get("body"),
                              confirm=bool(body.get("confirm")))
    return web.json_response(result, headers=auth.CORS)


async def routines(request):
    """Routine definitions and their fire counts, for the dashboard tile."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        data = await asyncio.to_thread(watch.routines)
    except Exception as e:
        return web.json_response({"items": [], "error": str(e)[:200]},
                                 headers=auth.CORS)
    return web.json_response(data, headers=auth.CORS)


async def sentinel(request):
    """Health checks, passed through as written plus how old they are."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    try:
        data = await asyncio.to_thread(watch.sentinel)
    except Exception as e:
        return web.json_response({"worst": "ok", "checks": [],
                                  "error": str(e)[:200]}, headers=auth.CORS)
    return web.json_response(data, headers=auth.CORS)


async def preflight(request):
    return web.Response(status=204, headers=auth.CORS)


routes = [
    web.get("/get", get_page),
    web.get("/get/apk", get_apk),
    web.get("/local/status", status),
    web.get("/local/schedule", upcoming),
    web.get("/local/presence", presence),
    web.get("/local/comms", comms_view),
    web.get("/local/routines", routines),
    web.get("/local/sentinel", sentinel),
    web.post("/local/sms", sms),
    web.get("/local/qr", qr),
    web.post("/local/pair/new", pair_new),
    web.post("/local/revoke", revoke),
    web.post("/local/board", board),
    web.post("/local/tasks/ops", tasks_ops),
    web.options("/local/{tail:.*}", preflight),
]
