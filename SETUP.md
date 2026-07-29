# CORTANA — Setup Guide

From blank laptop to talking assistant. Do steps in order. Each step has a
verification gate — do not continue until it passes.

**Manual items you must fetch yourself are marked `[MANUAL]`.**

---

## Project tree

```
cortana/
├── SETUP.md            this file
├── install.sh          one-shot installer
├── requirements.txt
├── .env.example        copy to .env, add keys
├── config.py           all settings
├── main.py             entrypoint (voice + text modes)
├── orchestrator.py     lead agent loop, budget cap
├── agents.py           subagents + tool definitions
├── memory.py           sqlite + CORTANA.md persistent context
├── cortana.service     systemd unit (always-on)
├── cortana-bridge.service  systemd unit (phone link, STEP 11)
├── bridge/             mobile bridge server (phone pairing, state, voice)
├── mobile/             Android app source + CI-built APK (mobile/dist)
├── tools/
│   ├── files.py        shell + file ops (workspace-scoped)
│   ├── vision.py       screenshots
│   ├── gmail_tool.py   search/read/DRAFT-only
│   ├── trading.py      yfinance now, real-time interface for later
│   └── video.py        ffmpeg wrapper
└── voice/
    ├── mic.py          PTT recording + VAD continuous listening
    ├── stt.py          Whisper API (+ local fallback path)
    ├── tts.py          ElevenLabs -> OpenAI -> espeak chain
    └── wake.py         "ok cortana" match + addressed-classifier
```

---

## STEP 1 — Install Ubuntu `[MANUAL]`

1. On another computer: download Ubuntu 24.04 LTS Desktop ISO (ubuntu.com/download).
2. Flash to 8GB+ USB stick with balenaEtcher (etcher.balena.io) or Rufus (Windows).
3. Boot laptop from USB (mash F12/F2/Del at power-on for boot menu).
4. Install: **Erase disk and install Ubuntu** (full wipe, per plan). Normal installation, download updates ON.
5. Create your admin user (yourself), reboot, remove USB.
6. **Critical:** at the login screen, click your name, then the **gear icon bottom-right → "Ubuntu on Xorg"**. Wayland (the default) breaks global hotkeys and screenshots. Do this every login, or make permanent:
   ```
   sudo nano /etc/gdm3/custom.conf     # uncomment: WaylandEnable=false
   ```
7. Terminal (Ctrl+Alt+T):
   ```
   sudo apt update && sudo apt upgrade -y
   ```

**GATE:** `echo $XDG_SESSION_TYPE` prints `x11`. If `wayland`, redo 6.

---

## STEP 2 — Create the sandbox user

Cortana runs under its own account. Full autonomy inside it; your main account stays untouched. This IS the "unlimited access" — scoped so one bad email/webpage instruction can't nuke the machine.

```
sudo adduser cortana            # pick a password
sudo usermod -aG audio,video cortana
```

Log out, log into **cortana** (Xorg again — gear icon). All remaining steps happen as cortana.

**GATE:** `whoami` prints `cortana`.

---

## STEP 3 — Install the project

Copy the `cortana/` project folder to `/home/cortana/cortana` (USB stick, or from your account: `sudo cp -r /path/to/cortana /home/cortana/ && sudo chown -R cortana:cortana /home/cortana/cortana`).

```
cd ~/cortana
bash install.sh
```

Installs: python venv, portaudio (mic), ffmpeg (video+audio playback), espeak-ng (offline TTS fallback), all pip packages.

**GATE:** ends with "=== Installed ===" and no red errors.
If pip fails on `sounddevice`: `sudo apt install -y libportaudio2` and rerun.

---

## STEP 4 — API keys `[MANUAL]` — three accounts

**Anthropic (the brain):**
1. console.anthropic.com → sign up → Billing → add card, buy $10-25 credit.
2. **Settings → Limits: set monthly spend limit $50** (hard stop, matches app cap).
3. API Keys → create → copy `sk-ant-...`.

**OpenAI (ears — Whisper STT + backup voice):**
1. platform.openai.com → sign up → Billing → add $5-10.
2. API Keys → create → copy `sk-...`.

**ElevenLabs (voice):**
1. elevenlabs.io → sign up → Starter plan (~$5/mo) or free tier to test.
2. Profile → API key → copy.
3. Default voice = Rachel (light female). To change: pick voice on site, copy its Voice ID into `.env`.

Then:
```
cd ~/cortana && nano .env      # paste all three keys, save (Ctrl+O, Ctrl+X)
```

**GATE:** `cat .env` shows three real keys, no `...` placeholders.

---

## STEP 5 — First contact (text mode)

Always prove the brain works before adding ears/mouth.

```
./venv/bin/python main.py --text
```

Test sequence, in order:
| Say | Proves |
|---|---|
| `hello, who are you` | API key + model + identity |
| `create a file called test.txt saying hi, then list files` | autonomous shell/file tools |
| `take a screenshot and tell me what you see` | vision (must be on desktop terminal, not SSH) |
| `what is ES=F trading at, quick read on the 15 minute chart` | trading agent + delegation |
| `research: latest news on the S&P today` | web search subagent |
| `remember that I trade NQ futures mainly` | persistent memory |

`quit`, restart, ask `what do you remember about me` — memory survives restart.

**GATE:** all six work. Fix anything here before Step 6 — every later problem is easier to debug in text mode.

---

## STEP 6 — Gmail `[MANUAL]` (~15 min, one-time)

1. console.cloud.google.com → New project → "cortana".
2. APIs & Services → Library → **Gmail API** → Enable.
3. APIs & Services → OAuth consent screen → External → fill name/email → **Audience → Test users → add your own Gmail** (skips Google verification).
4. Credentials → Create Credentials → OAuth client ID → **Desktop app** → Download JSON.
5. Save it as `~/cortana/credentials.json`.
6. Text mode: `search my email for anything from today` → browser opens → log in → allow. Creates `token.json`; never asks again.

**GATE:** email search returns results; `ask it to draft a reply` → draft appears in Gmail Drafts folder. It can never send — drafts only, by design.

---

## STEP 7 — Voice

```
./venv/bin/python main.py
```

- **Hold F9**, speak, release → Cortana answers out loud.
- **F10** cycles: `ptt` → `wake` ("ok cortana, ...") → `open` (just talk; a cheap classifier decides if you're addressing her, and logs every decision for future learning).

Notes:
- wake/open transcribe every speech burst → small constant Whisper cost. PTT = zero idle cost. Default is PTT for a reason.
- **Headphones strongly recommended in wake/open** — otherwise her own voice can retrigger the mic.
- Built-in mic fine for now. USB mic later: plug in, `python -c "import sounddevice as sd; print(sd.query_devices())"`, put its index in `.env` as `MIC_DEVICE=`.

**GATE:** full loop — speak "hold F9: what time is it" → spoken reply in Rachel's voice.

---

## STEP 8 — Trading + video smoke tests

Trading (voice or text):
- Open TradingView on a chart → "look at my screen, buy or sell here?" → screenshot + data + BUY/SELL/HOLD lean with invalidation level.
- Data = yfinance, ~15min delayed, treated as live per your call. Real-time later: implement `RealtimeProvider` in `tools/trading.py` (Databento/Tradovate/IBKR), flip one line (`PROVIDER=`). Nothing else changes.
- Execution is hard-absent. No order code exists anywhere.

Video:
- Drop a clip in `~/workspace/`.
- "trim clip.mp4 to the first 30 seconds, call it short.mp4" → check `~/workspace/short.mp4`.

---

## STEP 9 — Always-on daemon

```
mkdir -p ~/.config/systemd/user
cp ~/cortana/cortana.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cortana
loginctl enable-linger cortana
```

Watch logs: `journalctl --user -u cortana -f`
Auto-login (so mic/hotkeys exist at boot): Settings → Users → cortana → Automatic Login ON.

**GATE:** reboot → wait 60s → hold F9 and talk without opening anything.

---

## STEP 10 — Cost control

- Two caps already set: Anthropic console limit + in-app `BUDGET_MONTHLY_USD=50` (Cortana refuses politely at cap).
- Check spend anytime: "how much have you cost me this month" or:
  ```
  sqlite3 ~/cortana/state.db "SELECT ROUND(SUM(cost),2) FROM usage;"
  ```
- Expected: light-moderate daily use ≈ **$15-40/mo** tokens + ~$5 ElevenLabs + ~$1-3 Whisper.
- If high: check which model burns most (`SELECT model, ROUND(SUM(cost),2) FROM usage GROUP BY model;`) — usually fix is routing more work to Haiku via `.env`.

---

## STEP 11 — Phone link (Cortana Mobile, Android) `[MANUAL]`

The phone mirrors the Dusk dashboard (view-only, scrolling) and talks to
Cortana with her real voice. Full guide: `mobile/README.md`. Short version:

1. Bridge on this machine:
   ```
   cd ~/cortana && git pull && bash bridge/install-bridge.sh
   ```
2. Tailscale on both devices (tailscale.com, free) — same tailnet. Note the
   workstation's Tailscale name (`tailscale status`).
3. Dusk dashboard → edit mode (Ctrl-E) → add the **MOBILE LINK** module →
   **PAIR A PHONE** → a 6-digit code appears.
4. Phone: install the APK from the latest `mobile-v*` GitHub release (or
   `~/cortana/mobile/dist/cortana-mobile.apk`), open it, enter the Tailscale
   name + code. Add the 2x2 **Cortana sphere widget** to the home screen for
   one-tap talking.
5. App updates are automatic: bump the version in `mobile/app/build.gradle`,
   push to main, `git pull` here — every paired phone gets the update pop-up.

**GATE:** MOBILE LINK module shows the phone's name ONLINE; the phone shows
"Linked to <this machine>"; tapping the sphere and asking "what time is it"
answers in Cortana's voice.

---

## SELF-CHECK — "if the prior step was done right, does the next work?"

| Step | Assumes | If assumption breaks |
|---|---|---|
| 2 | Xorg session | gate in step 1 catches it |
| 3 | internet + apt access | corporate/hotel wifi blocks apt → use phone hotspot |
| 5 | valid Anthropic key + credit | 401 → key typo; 400 model not found → see matrix below |
| 6 | browser available on machine | headless install → do OAuth on desktop once, copy token.json over |
| 7 | working mic + X11 hotkeys | matrix below |
| 9 | user session at boot | auto-login + linger, both listed |

## FAILURE MATRIX — alternate paths

| Symptom | Cause | Fix / Plan B |
|---|---|---|
| F9/F10 do nothing | Wayland session | Xorg at login (step 1.6). Plan B: run `main.py --text` in a terminal — full brain, keyboard input |
| Screenshot tool errors | Wayland, or running over SSH | Same Xorg fix; must run on the desktop itself |
| `PortAudioError` / no mic | ALSA device confusion | `python -c "import sounddevice as sd; print(sd.query_devices())"` → set `MIC_DEVICE=` index in `.env` |
| Listens to room noise / never triggers (wake/open) | VAD threshold | `VAD_THRESHOLD` in `.env`: raise (500-800) if too sensitive, lower (150-250) if deaf |
| She hears herself, loops | speakers + open mode | headphones, or stay in PTT |
| No spoken reply, console shows "TTS fallback" | ElevenLabs key/credit/rate limit | auto-falls to OpenAI voice, then espeak. Nothing to do unless you want Rachel back — check ElevenLabs account |
| Robot voice (espeak) | both TTS keys failing | check both keys + billing |
| STT empty / slow | OpenAI key or no net | offline: `pip install faster-whisper`, set `USE_LOCAL=True` in `voice/stt.py` |
| API error 400 "model not found" | model string rotated | platform.claude.com/docs → Models page → update `MODEL_*` in `.env` |
| API error 401 | bad Anthropic key | re-copy from console |
| Budget message | cap hit | raise `BUDGET_MONTHLY_USD` + Anthropic console limit |
| Gmail "app not verified" block | consent screen | add your Gmail under Test users (step 6.3) |
| Gmail auth loop | stale token | `rm ~/cortana/token.json`, re-auth |
| `ES=F` returns nothing | yahoo hiccup / bad symbol | retry; symbols: ES=F NQ=F CL=F GC=F; stocks plain (AAPL) |
| Daemon runs but deaf/blind | no DISPLAY/audio in service | auto-login ON + linger; Plan B: skip systemd, add `main.py` as a Startup Application (GUI: "Startup Applications" app) |
| Whole machine feels at risk | injection paranoia (healthy) | she already: separate user, drafts-only email, zero trade execution. Keep it that way |
| Phone can't pair | bridge down / wrong host / stale code | `systemctl --user status cortana-bridge`; use the Tailscale name, not LAN IP; codes die in 10 min — tap PAIR A PHONE again |
| Phone shows LINK DOWN | workstation asleep or tailnet off | wake the machine, check `tailscale status` on both ends; the app reconnects on its own |
| Phone never offers an update | workstation hasn't pulled CI's dist commit | `cd ~/cortana && git pull` (or ask Cortana to) |
| "App not installed" on update | APK signed with a different key | keep `mobile/keystore/` as-is; if the keystore ever changed, uninstall then reinstall once |

## FUTURE HOOKS (built-in, dormant)

- **Real-time market data:** `tools/trading.py` → `RealtimeProvider` stub, one-line swap.
- **True wake word (free, offline):** openWakeWord gating the mic before any API call — replaces transcribe-then-match in `main.py`.
- **Learning who you're talking to:** every open-mode decision logs to `address_log` table → future training data.
- **UI/app development:** `dev` agent already exists (Opus, full shell) — say "delegate to dev: build me X".
- **USB mic:** `MIC_DEVICE=` in `.env`.
