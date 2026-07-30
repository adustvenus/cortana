# Cortana

A voice-first personal assistant that runs on your own machine, with a
persistent status dashboard and a phone client. Three processes, each its own
systemd user unit, deliberately decoupled — restarting any one never disturbs
the others.

```
   ┌──────────────┐   hud_state.json    ┌────────────────────┐
   │  cortana     │──────────────────▶  │  Dusk Dashboard    │
   │  (main.py)   │   calendar_state    │  (Electron, X11)   │
   │              │                     └─────────┬──────────┘
   │  voice loop  │                          loopback │ pairing, board
   │  agents      │                                   ▼
   │  self-edit   │◀───── same orchestrator ──┌────────────────┐
   └──────────────┘                           │ cortana-bridge │
                                              └───────┬────────┘
                                        Tailscale / LAN │
                                                        ▼
                                              Cortana Mobile (Android)
```

| Unit | What it is | Docs |
|---|---|---|
| `cortana` | The assistant: mic → Whisper → agent loop → voice. Edits its own source under git with a rollback failsafe. | [SETUP.md](SETUP.md) |
| `cortana-dash` | Always-on status board: modules for Cortana's state, music, agenda, tasks, git, weather, a file explorer (tree + graph), and the phone link. Owns power policy - keeps the machine awake, sleeps only the screen. | [Dashboard/README.md](Dashboard/README.md) |
| `cortana-bridge` | Serves the phone: state feed, voice turns, Spotify, updates. | [bridge/README.md](bridge/README.md) |
| — | The Android client (view-only mirror + talk + widget). | [mobile/README.md](mobile/README.md) |

## Getting started

Follow [SETUP.md](SETUP.md) start to finish — it goes from a blank Ubuntu
install to a working assistant, with a verification gate at every step and a
failure matrix for what actually goes wrong. Steps 1–10 cover Cortana herself;
step 11 adds the phone.

Quick version, on a machine that already meets the prerequisites:

```bash
git clone <this repo> ~/cortana && cd ~/cortana
bash install.sh                    # python venv, audio deps, pip packages
nano .env                          # API keys (see .env.example)
./venv/bin/python main.py --text   # prove the brain works before adding voice

bash Dashboard/install-dash.sh     # the status board  (needs Node 18+, X11)
bash bridge/install-bridge.sh      # the phone bridge
```

## Layout

```
cortana/
├── main.py            entrypoint: voice loop, push-to-talk, mode cycling
├── orchestrator.py    lead agent loop, budget cap, self-critique
├── agents.py          subagent definitions + tool dispatch
├── selfedit.py        git-checkpointed self-modification with rollback
├── launcher.py        supervisor: crash-loop detection → revert to last good
├── memory.py          sqlite: conversation log, key-value memory, spend
├── tools/             shell, files, vision, gmail, calendar, trading, video
├── voice/             mic capture, STT, TTS, wake-word matching
├── Dashboard/         Electron status board + the module authoring contract
├── bridge/            the phone bridge service (see bridge/README.md)
└── mobile/            Android client + CI-built APK in mobile/dist/
```

`CORTANA.md` is her persistent context — identity, standing rules, and what she
is allowed to change about herself. It is read at every turn, so edits there
take effect on the next restart.

## Operating

```bash
systemctl --user status cortana cortana-dash cortana-bridge
journalctl --user -u cortana -f          # or -u cortana-bridge
```

Budget is capped in two places: `BUDGET_MONTHLY_USD` in `.env` (she declines
past it) and your Anthropic console limit. Check spend with
`sqlite3 state.db "SELECT ROUND(SUM(cost),2) FROM usage;"` or just ask her.

## Known operational gotchas

Three things that have bitten before and are worth knowing up front:

- **Google tokens die every 7 days** unless the OAuth consent screen is
  *published* (projects in "Testing" get their refresh tokens expired by
  Google). Symptom: the agenda freezes - shows deleted events, misses new ones.
  Diagnose with `python main.py --calendar-debug`.
- **Spotify rate-limits (429)** if polled too hard; the dashboard and the bridge
  share a cool-off file and back off together. `RATE LIMITED · Ns` on the Music
  module is self-healing, not a fault.
- **Some Android skins (OnePlus/OPPO) silently refuse APK installs.** The app
  self-updates via `PackageInstaller`; when a skin blocks that, use
  `bash mobile/push-update.sh` (wireless adb, no cable) or the in-app
  *Install via workstation (adb)* button.

## Design rules

These hold across all three processes and are worth knowing before changing
anything:

- **Decoupled by file, not by API.** Components communicate through small
  atomic JSON state files (`hud_state.json`, `calendar_state.json`,
  `mic_state.json`). A reader crashing never blocks a writer, and any component
  can be restarted alone.
- **Degrade in sections, never as a whole.** A missing mic, an expired Spotify
  grant, an unreachable calendar — each blanks its own area and says why.
  Nothing takes the process down.
- **The recovery chain is sacred.** `launcher.py`, `selfedit.py` and the unit
  files are protected from self-editing; a crash loop reverts the repo to
  `.last_good` before Cortana can make it worse.
- **Least privilege outward.** Gmail is drafts-only, trading is
  recommendations-only, the phone is view-only, and the bridge is never exposed
  beyond your tailnet.
