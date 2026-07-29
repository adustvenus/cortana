"""Cortana Mobile Bridge - the workstation end of the phone link.

One small aiohttp service (its own systemd unit, cortana-bridge.service -
independent of cortana.service and the dashboard, so restarting any one of
the three never touches the others). It:

- authenticates phones (pairing codes + bearer tokens, see pairing.py)
- aggregates everything the Dusk dashboard shows into one JSON state feed
  (hud_state.json, systemd liveness, calendar_state.json, git, Spotify) and
  pushes it to phones over a WebSocket
- runs full voice turns for the phone: WAV in -> Whisper STT -> the same
  orchestrator the desk uses -> reply text out, plus a TTS endpoint that
  streams Cortana's actual ElevenLabs voice back
- relays Spotify transport control through the dashboard's own token grant
- serves the current APK + version so the phone can self-update
- accepts a board snapshot from the dashboard's MOBILE LINK module so the
  phone mirrors the real board (module order, tasks, weather ZIP)

Reach: designed for Tailscale. Bind defaults to 0.0.0.0 so the tailnet IP
works; every /api/* call requires a device token, /local/* only answers
loopback (the dashboard). Run: venv/bin/python -m bridge.server
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                      # loads .env; gives ROOT
import hud_state
import calendar_state
from bridge import pairing, spotify_link

BRIDGE_VERSION = "1.0.0"
PORT = int(os.getenv("BRIDGE_PORT", "8765"))
BIND = os.getenv("BRIDGE_BIND", "0.0.0.0")
HOST_NAME = os.getenv("BRIDGE_NAME", "") or socket.gethostname()
ROOT = config.ROOT
DIST = ROOT / "mobile" / "dist"
TTS_CAP = 1500                     # same cap as voice/tts.py speak()

# Cortana brain, loaded lazily so the bridge still serves state/pairing when
# an API key is missing or a dependency is broken - talking just errors.
_brain = {"ready": False, "error": "", "mods": None}


def _load_brain():
    if _brain["ready"] or _brain["error"]:
        return _brain
    try:
        import memory
        import orchestrator
        from voice import stt, speech
        memory.init()
        # The bridge has no speaker: reroute every spoken line (lead preambles,
        # background-task announcements) to the connected phones instead.
        speech.say = _announce_to_phones
        speech.announce = _announce_to_phones
        speech.say_wait = lambda text, timeout=60: _announce_to_phones(text)
        _brain.update(ready=True, mods=(memory, orchestrator, stt))
    except Exception as e:
        _brain["error"] = f"brain unavailable: {e}"
    return _brain


# ── state aggregation ───────────────────────────────────────────────────────
_cache = {}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = hit[1] if hit else {"error": str(e)[:120]}
    _cache[key] = (now, val)
    return val


def _run(cmd, timeout=6):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def _cortana_state():
    st = hud_state.read_state()
    svc = _cached("svc", 5, lambda: _run(
        ["systemctl", "--user", "is-active", "cortana"]) or "unknown")
    ts = float(st.get("ts") or 0)
    age = time.time() - ts if ts else 1e9
    return {"state": st.get("state", "offline"), "agent": st.get("agent", ""),
            "detail": st.get("detail", ""), "mode": st.get("mode", ""),
            "thoughts": st.get("thoughts", [])[-6:], "ts": ts,
            "service": svc, "stale": age > 600, "fresh": age < 10}


def _git_state():
    def go():
        log = _run(["git", "-C", str(ROOT), "log", "--oneline", "-5"])
        status = _run(["git", "-C", str(ROOT), "status", "--short"])
        branch = _run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"])
        return {"branch": branch or "unknown",
                "clean": not status,
                "files": len(status.split("\n")) if status else 0,
                "log": [{"hash": l[:7], "msg": l[8:]} for l in log.split("\n") if l]}
    return _cached("git", 30, go)


def _apk_info():
    def go():
        j = json.loads((DIST / "version.json").read_text())
        return {"version": str(j.get("version", "")),
                "apk": str(j.get("apk", "")),
                "available": bool(j.get("apk")) and (DIST / str(j.get("apk"))).exists()}
    return _cached("apk", 60, go)


_board = {"data": None, "ts": 0.0}


def build_state():
    return {
        "type": "state",
        "host": HOST_NAME,
        "bridgeVersion": BRIDGE_VERSION,
        "apk": _apk_info(),
        "brainReady": _load_brain()["ready"],
        "brainError": _brain["error"],
        "cortana": _cortana_state(),
        "calendar": _cached("cal", 15, calendar_state.read),
        "git": _git_state(),
        "spotify": _cached("spotify", 8, spotify_link.state),
        "board": _board["data"],
        "boardTs": _board["ts"],
        "devices": pairing.devices(),
        "ts": time.time(),
    }


# ── websocket push + phone announcements ────────────────────────────────────
_sockets = set()
_loop = None


def _announce_to_phones(text, **_kw):
    """Replacement for speech.say/announce inside the bridge process: forward
    the line to every connected phone (shown + optionally spoken there)."""
    text = (text or "").strip()
    if not text or _loop is None:
        return
    msg = json.dumps({"type": "announce", "text": text[:500]})
    def send():
        for ws in list(_sockets):
            asyncio.ensure_future(_ws_safe_send(ws, msg))
    _loop.call_soon_threadsafe(send)


async def _ws_safe_send(ws, msg):
    try:
        await ws.send_str(msg)
    except Exception:
        _sockets.discard(ws)


async def _push_loop(app):
    last = ""
    while True:
        await asyncio.sleep(1.5)
        if not _sockets:
            continue
        try:
            snap = await asyncio.to_thread(build_state)
            js = json.dumps(snap)
        except Exception:
            continue
        if js == last:
            continue
        last = js
        for ws in list(_sockets):
            await _ws_safe_send(ws, js)


# ── auth helpers ────────────────────────────────────────────────────────────
def _token_of(request):
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return request.query.get("token", "")


def _device(request):
    return pairing.auth(_token_of(request))


def _deny():
    return web.json_response({"error": "unauthorized"}, status=401)


def _is_loopback(request):
    return (request.remote or "") in ("127.0.0.1", "::1", "localhost")


# ── voice turns ─────────────────────────────────────────────────────────────
_turn_lock = threading.Lock()
_turn_cancel = {"ev": None}


def _run_turn(wav_path, text, device_name):
    """Blocking: STT (if audio) + one full orchestrator turn. A newer phone
    turn cancels the one in flight, mirroring the desk behavior in main.py."""
    b = _load_brain()
    if not b["ready"]:
        return {"error": b["error"]}
    memory, orchestrator, stt = b["mods"]

    prev = _turn_cancel["ev"]
    if prev is not None:
        prev.set()                       # preempt the in-flight phone turn
    cancel = threading.Event()
    _turn_cancel["ev"] = cancel

    with _turn_lock:                     # one phone turn at a time
        if cancel.is_set():
            return {"canceled": True}
        transcript = text
        if wav_path:
            _hud_set("thinking")
            transcript = stt.transcribe(wav_path)
            if not transcript:
                _hud_set("idle")
                return {"transcript": "", "reply": "",
                        "error": "didn't catch that - too quiet or empty"}
        _hud_set("thinking")
        try:
            reply = orchestrator.handle(transcript, cancel=cancel)
        finally:
            _hud_set("idle")
        if reply is None:
            return {"transcript": transcript, "canceled": True}
        # Phone-requested restart/shutdown: the flags live in THIS process, so
        # act on the real service ourselves, then clear them for the next turn.
        if orchestrator.restart_requested():
            orchestrator._restart_flag["do"] = False
            subprocess.Popen(["systemctl", "--user", "restart", "cortana"])
        elif orchestrator.shutdown_requested():
            orchestrator._shutdown_flag["do"] = False
            subprocess.Popen(["systemctl", "--user", "stop", "cortana"])
        return {"transcript": transcript, "reply": reply}


_hud_owned = {"on": False}


def _hud_set(state):
    """Reflect phone turns on the dashboard orb - but never fight the desk
    process: if Cortana is already mid-turn (thinking/working/speaking) and it
    wasn't us who set it, leave the file alone (last-writer-wins otherwise)."""
    cur = hud_state.read_state().get("state", "idle")
    if state == "idle":
        if _hud_owned["on"]:
            _hud_owned["on"] = False
            hud_state.set_state("idle")
        return
    if cur in ("thinking", "working", "speaking") and not _hud_owned["on"]:
        return
    _hud_owned["on"] = True
    hud_state.set_state(state, detail="phone")


# ── TTS streaming (Cortana's real voice to the phone) ───────────────────────
def _tts_stream_blocking(text, put):
    """requests-based ElevenLabs stream -> put(chunk); mirrors voice/tts.py.
    Falls back to non-streaming ElevenLabs, then OpenAI. put(None) = done,
    put(False) = total failure (phone should use its local TTS)."""
    import requests
    text = text[:TTS_CAP]
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVEN_VOICE_ID}/stream",
            headers={"xi-api-key": config.ELEVENLABS_API_KEY,
                     "Content-Type": "application/json"},
            json={"text": text, "model_id": "eleven_flash_v2_5",
                  "optimize_streaming_latency": 4},
            stream=True, timeout=60)
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=4096):
            if chunk:
                put(chunk)
        put(None)
        return
    except Exception as e:
        print("[bridge] tts stream failed, falling back:", e)
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVEN_VOICE_ID}",
            headers={"xi-api-key": config.ELEVENLABS_API_KEY,
                     "Content-Type": "application/json"},
            json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=60)
        r.raise_for_status()
        put(r.content)
        put(None)
        return
    except Exception as e:
        print("[bridge] tts eleven fallback failed:", e)
    try:
        from openai import OpenAI
        r = OpenAI(api_key=config.OPENAI_API_KEY).audio.speech.create(
            model="tts-1", voice="nova", input=text)
        put(r.content)
        put(None)
    except Exception as e:
        print("[bridge] tts openai fallback failed:", e)
        put(False)


# ── handlers: phone API ─────────────────────────────────────────────────────
async def h_pair(request):
    try:
        j = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    token, err = pairing.try_pair(j.get("code"), j.get("deviceName"))
    if err:
        return web.json_response({"error": err}, status=403)
    return web.json_response({"token": token, "host": HOST_NAME,
                              "bridgeVersion": BRIDGE_VERSION})


async def h_ping(request):
    if not _device(request):
        return _deny()
    return web.json_response({"ok": True, "host": HOST_NAME,
                              "bridgeVersion": BRIDGE_VERSION})


async def h_state(request):
    if not _device(request):
        return _deny()
    return web.json_response(await asyncio.to_thread(build_state))


async def h_ws(request):
    dev = _device(request)
    if not dev:
        return _deny()
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    _sockets.add(ws)
    try:
        await _ws_safe_send(ws, json.dumps(await asyncio.to_thread(build_state)))
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                # any inbound frame refreshes last_seen via pairing.auth
                pairing.auth(_token_of(request))
    finally:
        _sockets.discard(ws)
    return ws


async def h_converse(request):
    dev = _device(request)
    if not dev:
        return _deny()
    wav_path, text = None, ""
    ctype = request.headers.get("Content-Type", "")
    if ctype.startswith("multipart/"):
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
            j = await request.json()
            text = str(j.get("text", "")).strip()
        except Exception:
            pass
    if not wav_path and not text:
        return web.json_response({"error": "no audio or text"}, status=400)
    try:
        out = await asyncio.to_thread(_run_turn, wav_path, text, dev.get("name", ""))
    finally:
        if wav_path:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
    status = 200 if "error" not in out else 503
    return web.json_response(out, status=status)


async def h_tts(request):
    if not _device(request):
        return _deny()
    try:
        j = await request.json()
        text = str(j.get("text", "")).strip()
    except Exception:
        text = ""
    if not text:
        return web.json_response({"error": "no text"}, status=400)
    q = asyncio.Queue()
    loop = asyncio.get_running_loop()
    put = lambda c: loop.call_soon_threadsafe(q.put_nowait, c)
    fut = loop.run_in_executor(None, _tts_stream_blocking, text, put)
    first = await q.get()
    if first is False or first is None:
        await fut
        return web.Response(status=204)     # no audio -> phone uses local TTS
    resp = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
    await resp.prepare(request)
    await resp.write(first)
    while True:
        c = await q.get()
        if c is None or c is False:
            break
        await resp.write(c)
    await fut
    await resp.write_eof()
    return resp


async def h_spotify(request):
    if not _device(request):
        return _deny()
    try:
        j = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    out = await asyncio.to_thread(spotify_link.control, str(j.get("action", "")))
    _cache.pop("spotify", None)             # next state push reflects it fast
    return web.json_response(out)


async def h_apk(request):
    if not _device(request):
        return _deny()
    return web.json_response(_apk_info())


async def h_apk_download(request):
    if not _device(request):
        return _deny()
    info = _apk_info()
    if not info.get("available"):
        return web.json_response({"error": "no APK built yet"}, status=404)
    return web.FileResponse(DIST / info["apk"],
                            headers={"Content-Type": "application/vnd.android.package-archive"})


# ── handlers: dashboard (loopback only) ─────────────────────────────────────
_CORS = {"Access-Control-Allow-Origin": "*",
         "Access-Control-Allow-Headers": "Content-Type",
         "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}


def _local_guard(request):
    if not _is_loopback(request):
        return web.json_response({"error": "loopback only"}, status=403, headers=_CORS)
    return None


async def h_local_status(request):
    err = _local_guard(request)
    if err:
        return err
    return web.json_response({
        "bridge": "up", "version": BRIDGE_VERSION, "host": HOST_NAME,
        "port": PORT, "devices": pairing.devices(),
        "pairing": pairing.pair_info(), "lockedFor": int(pairing.locked_for()),
        "phonesConnected": len(_sockets),
    }, headers=_CORS)


async def h_local_pair_new(request):
    err = _local_guard(request)
    if err:
        return err
    return web.json_response(pairing.new_code(), headers=_CORS)


async def h_local_revoke(request):
    err = _local_guard(request)
    if err:
        return err
    try:
        j = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400, headers=_CORS)
    n = pairing.revoke(str(j.get("name", "")))
    return web.json_response({"revoked": n}, headers=_CORS)


async def h_local_board(request):
    err = _local_guard(request)
    if err:
        return err
    try:
        j = await request.json()
        if not isinstance(j, dict):
            raise ValueError("not an object")
    except Exception:
        return web.json_response({"error": "bad json"}, status=400, headers=_CORS)
    # Cap snapshot size so a runaway payload can't balloon bridge memory.
    if len(json.dumps(j)) > 200_000:
        return web.json_response({"error": "snapshot too large"}, status=413, headers=_CORS)
    _board["data"] = j
    _board["ts"] = time.time()
    return web.json_response({"ok": True}, headers=_CORS)


async def h_options(request):
    return web.Response(status=204, headers=_CORS)


# ── app ─────────────────────────────────────────────────────────────────────
def make_app():
    app = web.Application(client_max_size=32 * 1024 * 1024)  # phone WAVs
    app.add_routes([
        web.post("/api/pair", h_pair),
        web.get("/api/ping", h_ping),
        web.get("/api/state", h_state),
        web.get("/api/ws", h_ws),
        web.post("/api/converse", h_converse),
        web.post("/api/tts", h_tts),
        web.post("/api/spotify", h_spotify),
        web.get("/api/apk", h_apk),
        web.get("/api/apk/download", h_apk_download),
        web.get("/local/status", h_local_status),
        web.post("/local/pair/new", h_local_pair_new),
        web.post("/local/revoke", h_local_revoke),
        web.post("/local/board", h_local_board),
        web.options("/local/{tail:.*}", h_options),
    ])

    async def on_start(app):
        global _loop
        _loop = asyncio.get_running_loop()
        app["push"] = asyncio.create_task(_push_loop(app))

    async def on_stop(app):
        app["push"].cancel()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


if __name__ == "__main__":
    print(f"[bridge] v{BRIDGE_VERSION} on {BIND}:{PORT} host={HOST_NAME}")
    web.run_app(make_app(), host=BIND, port=PORT, print=None)
