"""/api/* - everything the phone calls. Every route requires a device token
except pairing itself, which trades a dashboard-issued code for one.
"""
import asyncio
import json
import os
import tempfile
import threading

from aiohttp import web

from bridge import (auth, brain, cmdchan, comms, hub, pairing, presence_link,
                    scheduler, spotify_link, state, updates, util, voice)
from bridge.settings import BRIDGE_VERSION, HOST_NAME, WS_HEARTBEAT, log


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
                        # Replay anything announced while this phone was away.
                        # Without it a completion that landed with no socket
                        # open was simply lost.
                        for item in hub.pending_after(frame.get("lastAnnounce")):
                            await hub.send(ws, json.dumps(item))
                        # ...and anything the deque could not hold. It is an
                        # in-memory ring and this unit is Restart=always, so a
                        # phone that was offline ACROSS a restart got nothing
                        # from the line above. Scheduled items have a durable
                        # record, so they are rebuilt from it - per device, and
                        # only once. sqlite in a thread: never on the loop.
                        for item in await asyncio.to_thread(
                                scheduler.replay_for, device.get("hash", "")):
                            await hub.send(ws, json.dumps(item))
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
    elif op == "remove" and str(body.get("id", "")):
        state.queue_task_op({"op": "remove", "id": str(body["id"])[:32]})
    elif op == "zip" and str(body.get("zip", "")).isdigit():
        state.queue_task_op({"op": "zip", "zip": str(body["zip"])[:5]})
    else:
        return web.json_response({"error": "bad op"}, status=400)
    # The phone treats this as "queued", not "applied" - it shows the change as
    # pending until the board snapshot comes back carrying it (the handshake).
    return web.json_response({"ok": True, "queued": True,
                              "dashboardOpen": state.board_is_fresh()})


def _turn_thread(fn, *args):
    """Run a blocking turn on a DAEMON thread, exposed as an asyncio future.

    Not asyncio.to_thread: its executor is joined during shutdown, so a turn
    still running - an orchestrator loop can last minutes - blocked
    "systemctl restart cortana-bridge" until it finished. A rescued turn has to
    outlive the REQUEST; it must not outlive a deliberate restart.
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def settle(setter, value):
        if not fut.done():
            setter(value)

    def work():
        try:
            result = fn(*args)
            loop.call_soon_threadsafe(settle, fut.set_result, result)
        except BaseException as e:                      # noqa: BLE001 - relayed
            loop.call_soon_threadsafe(settle, fut.set_exception, e)

    threading.Thread(target=work, daemon=True, name="phone-turn").start()
    return fut


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _announce_result(result, why):
    """Send a reply the phone will never receive over HTTP to the announcement
    channel instead, where the hello replay can deliver it later."""
    reply = str((result or {}).get("reply", "")).strip()
    log(f"{why}: reply {len(reply)} chars -> {'announced' if reply else 'nothing to send'}")
    if reply:
        hub.announce(reply)


def _rescue_reply(fut):
    """Handler was cancelled outright. Rarer than it sounds - see converse()."""
    try:
        result = fut.result() or {}
    except Exception as e:
        log(f"rescued turn raised: {e}")
        return
    _announce_result(result, "phone left mid-turn (handler cancelled)")


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
    # shield: closing the app aborts this HTTP request, and without it the
    # await is cancelled and the reply is discarded even though the turn keeps
    # running on its worker thread. She was still thinking; the answer just had
    # nowhere to go. Now the turn always finishes, and if the phone has gone the
    # reply is announced instead - which the hello replay then delivers when the
    # app comes back.
    turn = _turn_thread(brain.run_turn, wav_path, text)
    # The wav has to outlive the REQUEST, not just the await. On a disconnect
    # the shielded turn keeps running and has usually not reached STT yet, so
    # deleting the file in a finally: pulled it out from under the turn - it
    # then failed on a missing file and there was no reply left to rescue.
    # Clean up when the TURN ends.
    if wav_path:
        turn.add_done_callback(lambda _f, p=wav_path: _unlink(p))
    try:
        result = await asyncio.shield(turn)
    except asyncio.CancelledError:
        turn.add_done_callback(_rescue_reply)
        raise
    # Cancellation is NOT the usual way a departed phone shows up. The request
    # body is already read, so aiohttp keeps running the handler and only
    # discovers the peer is gone when it writes the response - by which point
    # this function has returned and there is nothing left to rescue. That is
    # why closing the app produced no rescue log at all. Check the transport
    # directly instead of waiting for a cancellation that never comes.
    gone = request.transport is None or request.transport.is_closing()
    log(f"turn complete (client {'gone' if gone else 'present'})")
    if gone:
        _announce_result(result, "phone gone at reply time")
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

    # bridge/voice.py currently defines no functions at all, so this attribute
    # does not exist and the handler 500'd on every call. 204 is the documented
    # degradation - the phone speaks the reply with its own engine - and it is
    # the honest answer for "this box cannot make audio for you".
    stream = getattr(voice, "stream_blocking", None)
    if stream is None:
        log("no TTS streamer in bridge/voice.py - the phone will speak it itself")
        return web.Response(status=204)

    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    put = lambda chunk: loop.call_soon_threadsafe(queue.put_nowait, chunk)

    def pump():
        """The sentinel is posted in a finally:, ALWAYS.

        Without it, a streamer that raised - a revoked ElevenLabs key, no
        network - left `await queue.get()` below waiting on an item nobody
        would ever put. The request never completed, the connection never
        closed, and shutdown_timeout=5 could not reclaim it: one bad key wedged
        a socket per attempt with nothing in the journal.
        """
        try:
            stream(text, put)
        except Exception as e:
            log("tts stream failed", e)
        finally:
            put(None)

    worker = loop.run_in_executor(None, pump)

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


async def presence(request):
    """Where the phone thinks the user is. Coordinates are classified into a
    coarse place and then dropped - see presence_link.record()."""
    if not auth.device(request):
        return auth.deny()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    kept = await asyncio.to_thread(presence_link.record, body)
    # Echo back only what was kept, so the app can show what the workstation
    # actually knows rather than what it sent.
    return web.json_response({"ok": True, "place": kept["place"]})


async def comms_sync(request):
    """Mirrored notifications and SMS. Idempotent: re-syncing the same backlog
    stores nothing new."""
    if not auth.device(request):
        return auth.deny()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    result = await asyncio.to_thread(comms.ingest, body)
    return web.json_response(result, status=200 if result.get("ok") else 503)


async def cmd_reply(request):
    """The phone answering a "cmd" frame.

    404 means the id is unknown - it timed out, it was already answered, or the
    bridge restarted in between. The phone must treat that as final and NOT
    retry: for an SMS, a retry is a second text message.
    """
    if not auth.device(request):
        return auth.deny()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    payload = {"ok": bool(body.get("ok")),
               "result": body.get("result"),
               "phoneError": str(body.get("error") or "")[:200]}
    if not cmdchan.resolve(body.get("id"), payload):
        return web.json_response({"error": "unknown request"}, status=404)
    return web.json_response({"ok": True})


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
    web.post("/api/presence", presence),
    web.post("/api/comms/sync", comms_sync),
    web.post("/api/cmd/reply", cmd_reply),
    # The shipped v2.5.0 APK posts to /api/cmd/result. Two agents built the two
    # halves of this channel, each recorded the path as "mine to name", and
    # neither reconciled - so every command reply 404'd and every sms.send
    # timed out as "the phone did not respond". Aliased rather than renamed
    # because the phone is already in the user's pocket and a route is cheaper
    # than an APK round trip; both paths stay valid.
    web.post("/api/cmd/result", cmd_reply),
    web.post("/api/tasks", task_op),
    web.get("/api/apk", apk),
    web.post("/api/apk/refresh", apk_refresh),
    web.post("/api/apk/adb", apk_adb),
    web.get("/api/apk/download", apk_download),
]
