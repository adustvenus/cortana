# Cortana Mobile Bridge

The workstation end of the phone link. One aiohttp service under its own
systemd unit (`cortana-bridge.service`), independent of `cortana.service` and
the dashboard so restarting any one never disturbs the other two.

```
                    phone (Cortana Mobile)
                            │
              Tailscale (anywhere) or LAN (at home)
                            │
   ┌────────────────────────▼─────────────────────────┐
   │  api_phone.py  /api/*   token required           │
   │  api_local.py  /local/* loopback · /get  install │
   └────────────────────────┬─────────────────────────┘
        ┌──────────┬────────┼─────────┬───────────┐
     state.py   brain.py  voice.py updates.py onboarding.py
        │          │         │         │
   hud_state    Cortana's  her TTS   mobile/dist
   calendar     real       chain     + git pull
   git/systemd  pipeline             + adb push
   Spotify
```

## Modules

| File | Responsibility |
|---|---|
| `settings.py` | Constants and `log()`. Leaf — imports nothing local. |
| `util.py` | TTL cache (`cached`/`invalidate`) and `run()` for subprocesses. |
| `pairing.py` | Pairing codes, device tokens. Stores SHA-256 hashes only. |
| `auth.py` | Per-request identity: phone bearer token, or loopback dashboard. |
| `hub.py` | Connected phones; broadcast and announcements. |
| `state.py` | The JSON snapshot phones render. |
| `brain.py` | Cortana's real STT + orchestrator, for phone-initiated turns. |
| `voice.py` | Her voice, streamed back (ElevenLabs → OpenAI fallbacks). |
| `spotify_link.py` | Transport control using the dashboard's own Spotify grant. |
| `updates.py` | APK publishing, `git pull --ff-only`, adb push. |
| `onboarding.py` | The QR install page served at `/get`. |
| `api_phone.py` | `/api/*` routes. |
| `api_local.py` | `/local/*` and `/get` routes. |
| `server.py` | Wiring, push loop, entrypoint. |

The dependency graph is acyclic and `settings`, `util`, `hub`, `pairing` and
`spotify_link` are leaves. `bridge/__init__.py` puts the Cortana checkout on
`sys.path` once, so any module can be imported first.

## Endpoints

**`/api/*` — phone, bearer token required** (except `pair`, which trades a
dashboard-issued code for one):

| Route | Purpose |
|---|---|
| `POST /api/pair` | Exchange a 6-digit code for a device token |
| `GET /api/ping` | Liveness + identity |
| `GET /api/state` | One-shot snapshot |
| `GET /api/ws` | Live snapshot stream + announcements |
| `POST /api/converse` | A voice (multipart WAV) or text turn |
| `POST /api/tts` | Stream her voice; `204` = use the phone's own TTS |
| `POST /api/spotify` | play / pause / next / previous |
| `GET /api/apk`, `POST /api/apk/refresh`, `POST /api/apk/adb`, `GET /api/apk/download` | Updates |

**`/local/*` — dashboard, loopback only:** `status`, `qr`, `pair/new`,
`revoke`, `board`.

**`/get`, `/get/apk` — onboarding, deliberately open** but reachable only over
the tailnet or LAN. They serve the signed APK and a deep link built from a code
the scanner was already looking at; a bare `/get` never reveals the active code.

## Operating

```bash
bash bridge/install-bridge.sh          # deps + unit + start
systemctl --user status cortana-bridge
journalctl --user -u cortana-bridge -f # every line is prefixed [bridge]
```

Environment (`.env`): `BRIDGE_PORT` (8765), `BRIDGE_BIND` (0.0.0.0),
`BRIDGE_NAME` (defaults to the hostname; this is the name the phone shows).

**Never port-forward this service.** It binds `0.0.0.0` so the tailnet and LAN
both work; exposure beyond that is not part of the security model.

## Design rules

- **Degrade in sections, never as a whole.** Every reader is TTL-cached and
  keeps its last good value; a broken git or Spotify call blanks its own field
  only. The brain loads lazily, so a missing API key costs you talking, not
  state and pairing.
- **Never fight the desk process.** Phone turns only take the dashboard orb
  when Cortana isn't already mid-turn (`brain._set_hud`), and the Spotify token
  file is re-read before every refresh so a rotation race with Electron
  self-heals.
- **Trust the socket, not the payload.** The adb-install target comes from the
  request's peer address, so a paired phone can only ever aim it at itself.
