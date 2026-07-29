"""/local/* - the Dusk dashboard's MOBILE LINK module, and /get - the phone
onboarding page a scanned QR opens.

/local is loopback-only: it issues pairing codes, revokes devices, and receives
the board snapshot. /get is deliberately open but reachable only over the
tailnet or LAN; see onboarding.py for why that is safe.
"""
import json

from aiohttp import web

from bridge import auth, hub, onboarding, pairing, state, updates
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
    return web.json_response({"ok": True}, headers=auth.CORS)


async def tasks_ops(request):
    """Drain the queue of task edits made on phones; the dashboard applies
    them to its own list and pushes the board back."""
    denied = auth.local_guard(request)
    if denied:
        return denied
    return web.json_response({"ops": state.drain_task_ops()}, headers=auth.CORS)


async def preflight(request):
    return web.Response(status=204, headers=auth.CORS)


routes = [
    web.get("/get", get_page),
    web.get("/get/apk", get_apk),
    web.get("/local/status", status),
    web.get("/local/qr", qr),
    web.post("/local/pair/new", pair_new),
    web.post("/local/revoke", revoke),
    web.post("/local/board", board),
    web.post("/local/tasks/ops", tasks_ops),
    web.options("/local/{tail:.*}", preflight),
]
