"""/api/* - everything the phone calls. Every route requires a device token
except pairing itself, which trades a dashboard-issued code for one.
"""
import asyncio
import json
import os
import tempfile

from aiohttp import web

from bridge import (auth, brain, hub, pairing, spotify_link, state, updates,
                    util, voice)
from bridge.settings import BRIDGE_VERSION, HOST_NAME, WS_HEARTBEAT


async def pair(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    token, err = pairing.try_pair(body.get("code"), body.get("deviceName"))
    if err:
        return web.json_response({"error": err}, status=403)
    return web.json_response({"token": token, "host": HOST_NAME,
                              "bridgeVersion": BRIDGE_VERSION})


async def ping(request):
    if not auth.device(request):
        return auth.deny()
    return web.json_response({"ok": True, "host": HOST_NAME,
                              "bridgeVersion": BRIDGE_VERSION})


async def snapshot(request):
    if not auth.device(request):
        return auth.deny()
    return web.json_response(await asyncio.to_thread(state.build))


async def websocket(request):
    """Live state feed. The first frame is a full snapshot so a reconnecting
    phone paints immediately rather than waiting for the next push. The socket
    itself is the device's presence signal (see hub.online_idents), and the
    phone's hello frame carries its installed app version for the dashboard."""
    device = auth.device(request)
    if not device:
        return auth.deny()
    ws = web.WebSocketResponse(heartbeat=WS_HEARTBEAT)
    await ws.prepare(request)
    hub.add(ws, device.get("hash", ""))
    try:
        await hub.send(ws, json.dumps(await asyncio.to_thread(state.build)))
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                auth.device(request)      # inbound traffic refreshes last_seen
                try:
                    frame = json.loads(msg.data)
                    if frame.get("type") == "hello":
                        pairing.set_app_version(device.get("hash", ""),
                                                frame.get("version"))
                except Exception:
                    pass
    finally:
        hub.discard(ws)
    return ws


async def task_op(request):
    """Task edits from the phone: queued here, drained and applied by the
    dashboard (its page owns the task list), which then pushes the board back
    so the phone converges. add: {op:'add', t:text}; toggle: {op:'toggle', id}."""
    if not auth.device(request):
        return auth.deny()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    op = str(body.get("op", ""))
    if op == "add" and str(body.get("t", "")).strip():
        state.queue_task_op({"op": "add", "t": str(body["t"]).strip()[:120]})
    elif op == "toggle" and str(body.get("id", "")):
        state.queue_task_op({"op": "toggle", "id": str(body["id"])[:32]})
    else:
        return web.json_response({"error": "bad op"}, status=400)
    return web.json_response({"ok": True, "note": "applies when the dashboard is open"})


async def converse(request):
    """One voice (multipart WAV) or text turn through Cortana's real pipeline."""
    device = auth.device(request)
    if not device:
        return auth.deny()
    wav_path, text = None, ""
    if request.headers.get("Content-Type", "").startswith("multipart/"):
        reader = await request.multipart()
        async for part in reader:
            if part.name == "audio":
                fd, wav_path = tempfile.mkstemp(suffix=".wav")
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(65536)
                        if not chunk:
                            break
                        f.write(chunk)
            elif part.name == "text":
                text = (await part.text()).strip()
    else:
        try:
            text = str((await request.json()).get("text", "")).strip()
        except Exception:
            pass
    if not wav_path and not text:
        return web.json_response({"error": "no audio or text"}, status=400)
    try:
        result = await asyncio.to_thread(brain.run_turn, wav_path, text)
    finally:
        if wav_path:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
    return web.json_response(result, status=200 if "error" not in result else 503)


async def tts(request):
    """Stream Cortana's voice for `text`. 204 means no audio was produced and
    the phone should speak the reply with its own engine."""
    if not auth.device(request):
        return auth.deny()
    try:
        text = str((await request.json()).get("text", "")).strip()
    except Exception:
        text = ""
    if not text:
        return web.json_response({"error": "no text"}, status=400)

    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    put = lambda chunk: loop.call_soon_threadsafe(queue.put_nowait, chunk)
    worker = loop.run_in_executor(None, voice.stream_blocking, text, put)

    first = await queue.get()
    if first is False or first is None:
        await worker
        return web.Response(status=204)

    response = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
    await response.prepare(request)
    await response.write(first)
    while True:
        chunk = await queue.get()
        if chunk is None or chunk is False:
            break
        await response.write(chunk)
    await worker
    await response.write_eof()
    return response


async def spotify(request):
    if not auth.device(request):
        return auth.deny()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    result = await asyncio.to_thread(spotify_link.control, str(body.get("action", "")))
    util.invalidate("spotify")            # next push reflects the change fast
    return web.json_response(result)


async def apk(request):
    if not auth.device(request):
        return auth.deny()
    return web.json_response(updates.apk_info())


async def apk_refresh(request):
    if not auth.device(request):
        return auth.deny()
    return web.json_response(await asyncio.to_thread(updates.refresh_dist))


async def apk_adb(request):
    """Push the update to this phone over wireless adb, for phones whose own
    installer refuses silently."""
    if not auth.device(request):
        return auth.deny()
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        port = int(body.get("port") or 5555)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad port"}, status=400)
    if not 0 < port < 65536:
        return web.json_response({"ok": False, "error": "bad port"}, status=400)

    # Target the socket's peer address, never a client-supplied host: a paired
    # phone can therefore only ever point this at itself.
    ip = (request.remote or "").strip()
    v4_mapped = ip.startswith("::ffff:")
    if not ip or (":" in ip and not v4_mapped):
        return web.json_response({"ok": False, "error": "need an IPv4 phone address"},
                                 status=400)
    if v4_mapped:
        ip = ip[len("::ffff:"):]
    return web.json_response(
        await asyncio.to_thread(updates.adb_install, f"{ip}:{port}"))


async def apk_download(request):
    if not auth.device(request):
        return auth.deny()
    path = updates.apk_path()
    if path is None:
        return web.json_response({"error": "no APK built yet"}, status=404)
    return web.FileResponse(path, headers={
        "Content-Type": "application/vnd.android.package-archive"})


routes = [
    web.post("/api/pair", pair),
    web.get("/api/ping", ping),
    web.get("/api/state", snapshot),
    web.get("/api/ws", websocket),
    web.post("/api/converse", converse),
    web.post("/api/tts", tts),
    web.post("/api/spotify", spotify),
    web.post("/api/tasks", task_op),
    web.get("/api/apk", apk),
    web.post("/api/apk/refresh", apk_refresh),
    web.post("/api/apk/adb", apk_adb),
    web.get("/api/apk/download", apk_download),
]
