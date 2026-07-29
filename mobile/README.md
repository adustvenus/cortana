# Cortana Mobile — the phone end of the link

Android app (Kotlin, `mobile/`) that pairs your phone to the workstation
running Cortana. It is a **view-only mirror** of the Dusk dashboard in one
scrolling column, plus two deliberate exceptions: **talking to Cortana** and
**Spotify transport control**. No layout editing, no service power buttons,
no task editing from the phone — those stay on the dashboard.

```
phone (Cortana Mobile)  ──Tailscale (WireGuard)──▶  workstation
   ▲ WS state pushes, voice, Spotify, APK      cortana-bridge.service (bridge/)
   │                                                │ reads the same sources the
   └── 2x2 sphere widget → talk screen              │ dashboard shows, runs real
                                                    │ orchestrator voice turns
        Dusk dashboard ──▶ MOBILE LINK module ──────┘ (loopback: pairing codes,
                                                       board snapshot, revoke)
```

## One-time setup

1. **Workstation:** `bash bridge/install-bridge.sh` (installs + starts
   `cortana-bridge.service` on port 8765).
2. **Tailscale** on both the workstation and the phone, same tailnet
   (tailscale.com — free for personal use). The bridge speaks plain HTTP
   because the tailnet layer already encrypts and authenticates the path.
   **Never port-forward the bridge to the open internet.**
3. **Dashboard:** edit mode (Ctrl-E) → tray → add **MOBILE LINK** → tap
   **PAIR A PHONE**. A 6-digit code appears (10-minute lifetime, single use,
   5 wrong tries locks pairing for 5 minutes).
4. **Phone:** install the APK (below), enter the workstation's Tailscale
   name/IP + the code. Done — the phone stores a 256-bit token in encrypted
   storage; the bridge stores only the token's SHA-256.

## Fastest install: scan the QR

The MOBILE LINK module shows a **QR code** next to the pairing code (tap
**PAIR A PHONE** first). Scan it with the phone camera → the bridge serves a
small install page over the tailnet → **1 · DOWNLOAD THE APP** (allow the
install when Android asks) → **2 · OPEN CORTANA & PAIR** deep-links into the
app and pairs it automatically. No GitHub login, no typing.

The `/get` page and APK download are deliberately unauthenticated — they're
only reachable over the tailnet/LAN and serve nothing secret; the pairing
code rides inside the QR itself, so scanning is exactly as privileged as
reading the code off the dashboard. Everything under `/api/*` stays
token-guarded.

## How updates install (and why it isn't a normal app store)

Android reserves *silent* installs for the Play Store and privileged apps —
that restriction isn't a side effect of self-hosting, it's the platform's
security model. Within it, the app uses the best available path, in order:

1. **PackageInstaller session with `USER_ACTION_NOT_REQUIRED`** (Android 12+).
   An app updating *itself*, as its own installer-of-record, may commit with
   no prompt at all — a real silent self-update, store-like. This is the
   default path and it also sidesteps the system installer UI that
   OxygenOS/ColorOS drop silently.
2. **The platform's confirmation dialog**, if it refuses the silent path
   (`STATUS_PENDING_USER_ACTION` → we surface the prompt).
3. **Workstation adb push** — Settings → *Install via workstation (adb)*. The
   phone asks the bridge to install to it over wireless adb: the privileged
   path, no terminal, no cable.

If you ever want *fully* normal behavior (background updates, no prompts, no
sideload warnings), the only real route is publishing to Google Play's
internal-testing track — a one-time $25 developer account, app stays private
to your testers, and Play handles updates natively. Everything else on
Android is a variation of the three paths above.

## If installs fail silently (OnePlus / OPPO / realme)

OxygenOS and ColorOS drop sideloads and in-app updates **with no error
message** — you tap Install and nothing happens. It is not the APK, the
permission grant, or the bridge; those skins block the installer UI itself.
adb installs take the privileged path instead, so they always work.

Wireless adb gives you that path with no cable:

```bash
# Phone, one time: Settings -> About device -> tap Build number 7x
#   -> Developer options -> Wireless debugging ON
#   -> "Pair device with pairing code"  (note the IP:PORT and 6-digit code)
bash mobile/push-update.sh pair 192.168.1.50:41234 123456

# Every update after that (phone on the same Wi-Fi, wireless debugging on):
bash mobile/push-update.sh
```

Worth trying once on OxygenOS 15, which may restore normal installs:
Settings → Security & privacy → More security & privacy → **Install unknown
apps** → Cortana → allow; and Play Store → Play Protect → ⚙ → turn off
scanning during the install.

## Getting the APK (manual path)

CI (`.github/workflows/mobile-apk.yml`) builds a signed APK on every push to
`main` that touches `mobile/`, commits it to `mobile/dist/`, and tags a
GitHub release (`mobile-v<version>`). First install: download from the
release (or `mobile/dist/cortana-mobile.apk` after a workstation
`git pull`) and sideload — Android asks once to allow installs from your
browser/file manager.

**Updates are automatic after that:** the bridge's state feed carries the
version in `mobile/dist/version.json`; when it is newer than the installed
app, the phone shows an **"Update available"** pop-up, downloads the APK over
the link, and hands it to the Android installer (the restart-to-update step).
Ship an update = bump `versionCode` + `versionName` in
`mobile/app/build.gradle`, push to main, `git pull` on the workstation.

Signing uses the committed keystore `mobile/keystore/cortana-release.p12` so
every build carries the same signature (Android refuses mismatched updates).
Anyone with repo access can therefore sign an installable update — fine for a
private personal repo; if that changes, move the keystore to a CI secret and
set `CORTANA_KEYSTORE_PASS`.

## What's on screen

- **MOBILE LINK** — link state, the dashboard machine's name (the dash name
  on the phone; the phone's name shows on the dashboard's module), bridge
  version, latest announcement.
- **CORTANA** — live orb state (idle/listening/thinking/working/speaking/
  offline), talking mode, agent/detail, the thoughts feed, systemd state.
- **MUSIC** — Spotify now-playing (through the dashboard's own token grant;
  the phone holds no Spotify credentials) with play/pause/next/previous.
  Playing Spotify **on the phone itself** works fine — the bridge reports the
  active device's name, so the dash and phone stay in sync either way.
- **AGENDA / TASKS / GIT / WEATHER** — mirrored read-only. Module order
  follows the real board via the MOBILE LINK module's snapshot; tasks and the
  weather ZIP come from that same snapshot (they live in the dashboard page's
  localStorage, which nothing else can read).
- **Talk screen** (sphere on the top bar, or the **2x2 home-screen widget**):
  tap to record, tap to send. Cortana's real ElevenLabs voice streams back;
  if the voice chain is down you still get the reply as text + phone TTS.
  Typing works too. Tap mid-playback to stop her.

## Finding your way around

Every non-obvious surface carries a **?** — on each card header in the app, on
the Settings sections, on the talk screen, and on the dashboard's MOBILE LINK
module and MIC picker. Tapping it explains what the thing is, where its data
comes from, and what to do when it looks wrong. The copy assumes a technical
reader: it names the actual files, services and commands involved rather than
paraphrasing them.

## Security model

- Pairing code: shown only on the dashboard (bridge `/local/*` endpoints
  answer loopback only), single-use, 10-min expiry, brute-force lockout.
- Phone credential: 256-bit random bearer token, stored in
  EncryptedSharedPreferences; server stores the hash only. Every `/api/*`
  call requires it.
- Revoke anytime: MOBILE LINK module → ✕ next to the phone (two-tap confirm),
  or delete `mobile_link.json` on the workstation to revoke everything.
- Transport: Tailscale/WireGuard end-to-end; the bridge binds 0.0.0.0 by
  default so LAN also works, but treat the tailnet as the supported path.

## Edge cases, planned for

| Situation | Behavior |
|---|---|
| Workstation asleep / bridge down | Phone shows LINK DOWN, reconnects with backoff (1s→30s) for as long as the app is open |
| `cortana.service` stopped | Dashboard mirror shows OFFLINE — but talking still works: the bridge runs its own orchestrator turns |
| Wi-Fi ↔ LTE switch | Tailscale re-routes; the WS drops and auto-reconnects |
| Token revoked on the dash | Phone gets a 401 → "Link revoked" prompt → re-pair (no retry hammering) |
| Update while app open | Version arrives in the next state push → pop-up; "Skip this version" is honored until the next release |
| APK missing on workstation | Update check says so; nothing breaks (user hasn't pulled yet, or CI hasn't run) |
| Both desk and phone talk at once | Separate processes; a newer phone utterance cancels the in-flight phone turn (same preemption rule as the desk) |
| Phone-initiated background task finishes later | Completion is announced to the phone as a banner (the bridge reroutes speech announcements) |
| ElevenLabs/OpenAI TTS both down | Reply text + Android TTS — never a dead end |
| Speech too quiet / silence sent | Bridge's silence gate answers "didn't catch that" instead of hallucinating |
| Spotify tokens refreshed by dash and bridge at once | Both re-read the token file before refreshing and write back atomically; a lost rotation race self-heals on re-read |
| Encrypted prefs corrupted (Android keystore reset) | Store is recreated; user re-pairs instead of the app crash-looping |
| Phone asks Cortana to restart/shut down | The bridge maps it to `systemctl --user restart/stop cortana` on the workstation |
| Voice replies while phone is on silent | MediaPlayer uses the assistant audio stream; media volume applies |
| hud_state contention (desk turn + phone turn) | Bridge only writes orb state when the desk isn't mid-turn |

## Building locally (optional — CI is the primary path)

Requires Android SDK 34. From `mobile/`: `gradle assembleRelease` (or open in
Android Studio). Output: `app/build/outputs/apk/release/app-release.apk`.
