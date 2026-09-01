# Cortana Mobile — the phone end of the link

Android app (Kotlin, `mobile/`) that pairs your phone to the workstation
running Cortana. Most of it is a **mirror** of the Dusk dashboard in one
scrolling column; the interactions it allows are **talking to Cortana**,
**Spotify transport control**, **tasks**, and **reordering the cards**
(phone-local). No adding or removing modules and no service power buttons —
those stay on the dashboard.

Since **2.5.0** the phone is also a *participant*, not only a viewer, and
every part of that is **off until you turn it on** in Settings:

- **BACKGROUND** — a foreground service holds the link open while the app is
  closed, so an announcement arrives when it is made instead of being replayed
  hours later. Urgency picks the notification channel, and you can type a
  reply straight into the notification.
- **PRESENCE** — coarse home/work/driving, charging and screen state reported
  to the workstation.
- **COMMS** — notification mirroring, and reading/sending SMS on request.

Each of those has a section of its own below, including what it cannot
promise.

```
phone (Cortana Mobile)  ──Tailscale (WireGuard)──▶  workstation
   ▲ WS state pushes, voice, Spotify, APK      cortana-bridge.service (bridge/)
   │                                                │ reads the same sources the
   └── 2x2 sphere widget → talk screen              │ dashboard shows, runs real
                                                    │ orchestrator voice turns
        Dusk dashboard ──▶ MOBILE LINK module ──────┘ (loopback: pairing codes,
                                                       board snapshot, revoke)
```

## It wears the dashboard's colours

The app used to carry the dusk palette as hex literals, so it matched the
dashboard only until you changed the dashboard's background. The board now
derives its whole scheme from that image and publishes the colour tokens
through the bridge; the app adopts them from the state snapshot and caches
them, so it opens in the right palette rather than flashing the defaults.

Themed: every card, label and accent, the header/talk/pairing spheres, the
system bars, and the **2x2 home-screen widget** - whose sphere is redrawn from
the same three gradient stops the dashboard and the desktop bubble orb use, so
all three spheres are one object rather than three lookalikes.

**Not** themed, and it cannot be: the **launcher icon**. Android resolves that
from the manifest at install time and gives an app no way to repaint its own
icon while running. `res/drawable/sphere.xml` exists only to feed that static
icon and is pinned to the shipped default palette. The widget is the closest
the home screen gets to an icon that tracks the board.

Colours arrive only while the dashboard has the **MOBILE LINK** module on the
board (they ride in its snapshot, like tasks and the weather ZIP). The bridge
holds the last good palette, so closing the dashboard leaves the phone's
colours where they are instead of resetting them.

See `../Dashboard/PALETTE.md` for how the colours are chosen.

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

Wireless adb gives you that path with no cable — **and with Tailscale left
on**. The script tries the phone's *tailnet* address first, because connecting
to its LAN address while the VPN is up tends to hang: the phone answers through
the tunnel and the reply never returns the way it went out.

```bash
# Phone, one time: Settings -> About device -> tap Build number 7x
#   -> Developer options -> Wireless debugging ON
#   -> "Pair device with pairing code"  (that dialog's IP:PORT and 6-digit code)
bash mobile/push-update.sh pair 192.168.1.50:41234 123456

# Every update after that. The PORT is on the MAIN Wireless debugging screen
# (it differs from the pairing dialog's, and changes when the toggle is flipped):
bash mobile/push-update.sh 37219

# No argument reuses the last address that worked:
bash mobile/push-update.sh
```

Once the phone is on v1.5 or later this is a fallback rather than the routine:
Settings → **INSTALL VIA WORKSTATION (ADB)** does the same thing from the
phone, and it already targets the tailnet address automatically (the bridge
uses the address the request arrived on), so Tailscale can stay on there too.

## After a signing-key rotation: uninstall first

Distinct from the silent-drop problem above, and it looks different: you get an
explicit failure rather than nothing happening. Android identifies an app by
its signature, so a build signed with a new key is not an update of the old app
— it is a stranger claiming the same package name, and every install path
refuses it:

- tap-to-install → **"App not installed"**
- `adb install -r` → **`INSTALL_FAILED_UPDATE_INCOMPATIBLE`**
- the in-app updater and *INSTALL VIA WORKSTATION (ADB)* → the same, because
  both use `install -r`

There is no flag that works around this; the old app has to go first. App data
and the pairing token go with it, so the phone re-pairs afterwards.

```bash
adb -s <target> uninstall com.cortana.mobile   # or: Settings -> Apps -> Cortana -> Uninstall
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

**Updates are automatic after that.** The bridge's state feed carries the
version in `mobile/dist/version.json`; when it is newer than the installed app
the phone shows an **"Update available"** pop-up and installs it over the link.

**Settings → CHECK FOR UPDATE** asks the workstation to `git pull --ff-only`
first, so CI's latest build lands without anyone touching the workstation. The
pull is refused (and reported) if that checkout has local changes, so it can
never conflict or discard work.

Ship an update = bump `versionCode` **and** `versionName` in
`mobile/app/build.gradle` and push to main. CI does the rest.

Signing uses a keystore that is **never committed**. CI restores it from the
`CORTANA_KEYSTORE_B64` repo secret, signs, and shreds it; `CORTANA_KEYSTORE_PASS`
is the password. Both steps fail the build if their secret is missing, so a
misconfigured secret can never ship an unsigned APK.

The keystore used to be committed here, together with its password in
`build.gradle`, on a public repo — anyone who cloned it could sign an update
this app installs. That key is dead and the current one replaced it. Keep the
new keystore off every machine that does not need to sign: the workstation runs
an agent with an unsandboxed shell, which is the last place a signing key
belongs.

Every build must use the SAME keystore — Android refuses an update whose
signature changed. Rotating it is therefore a breaking change for every
installed phone: see the note below.

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
- **UPCOMING** — the next few timers, alarms and reminders from the
  workstation's schedule table, coloured by urgency. Read-only: scheduling is
  something you ask her for, and the sphere is right there.
- **SENTINEL** — the workstation's own health checks; the header carries the
  worst of them. Healthy rows stay dim and detail-free so the one amber row is
  the thing your eye lands on.
- **PRESENCE** — what the workstation believes about you, next to what this
  phone last told it. They disagree in exactly the case worth debugging.
- **AGENDA / TASKS / GIT / WEATHER** — mirrored read-only. Tasks and the
  weather ZIP come from the board snapshot (they live in the dashboard page's
  localStorage, which nothing else can read).
- **Pull down to refresh** — forces one fresh fetch from the bridge,
  independent of the live stream; every card redraws from it.
- **Reordering** — press and hold any card and drag. The order is saved on the
  phone and from then on overrides the board's layout order; MOBILE LINK stays
  pinned at the top. Modules still can't be added or removed here. UPCOMING,
  SENTINEL and PRESENCE have no dashboard module behind them, so they start at
  the bottom of the list until you drag them somewhere better.
- **Talk screen** (sphere on the top bar, or the **2x2 home-screen widget**):
  tap to record, tap to send. Cortana's real ElevenLabs voice streams back;
  if the voice chain is down you still get the reply as text + phone TTS.
  Typing works too. Tap mid-playback to stop her.

## The background link, and what it cannot promise

`LinkClient` used to be foreground-only: activities called start/stop, so a
closed app had no socket at all. Anything announced while you were away was
**replayed** the next time you opened the app — right for a mirror, useless
for a reminder. "Your build finished" delivered an hour late is a log entry.

Settings → **BACKGROUND** starts `LinkService`, a foreground service that
becomes another holder of the same socket. It shows a permanent low-priority
notification (Android requires one; there is no silent version), and an
announcement now arrives as it is made, on one of three channels:

| bridge urgency | channel | behaviour |
|---|---|---|
| `critical`, `urgent` | Urgent | heads-up, sounds |
| `normal` | Cortana | ordinary notification |
| `ambient` | Ambient | shown, never sounded |

Three channels rather than one because a channel's importance belongs to the
**user** once it exists — an app may lower it and never raise it — so there is
no way to choose "buzz" or "silent" per announcement after the fact. It also
means ambient notes can be muted without muting alarms.

Each announcement carries a **Reply** box. What you type goes to
`POST /api/converse`, the same endpoint the talk screen uses, and the answer
replaces the notification. The reply is handled by the *service*, not a
broadcast receiver, because a receiver gets about ten seconds and a real voice
turn takes minutes.

**Doze is real and this is the part that cannot be verified from a dev box.**
In deep doze Android suspends the socket and every `Handler` timer the app
might have used to notice. Two defences are in place:

1. Settings offers the **battery-optimisation exemption**. (Play Store policy
   forbids most apps from asking for it; this one is sideloaded onto one phone
   by the person who built it, so the policy does not apply — but it stays
   opt-in.)
2. An `AlarmManager.setAndAllowWhileIdle` alarm every ~15 minutes — the only
   timer the platform still honours there — pokes the socket and reconnects it
   if it died. A `ConnectivityManager` callback does the same the instant a
   network reappears, so a Wi-Fi/LTE handoff does not wait out a 30-second
   backoff.

Neither is a guarantee, and OxygenOS/ColorOS/MIUI add their own process
killers on top of the documented behaviour. **The only proof is a phone left
overnight and an announcement fired at 4am.** If overnight announcements go
missing, grant the exemption first; if they still do, the OEM is killing the
process and no in-app change will fix it.

The service is restarted on boot and after an app update (`BootReceiver`),
because otherwise the first reboot turns the feature off while the switch
keeps saying it is on.

## Presence — what actually leaves the phone

**Off by default.** When on, `POST /api/presence` carries:

```json
{"place":"home|out|driving|unknown", "zone":"home|work|elsewhere|unknown",
 "lat":47.606, "lon":-122.332, "charging":true, "driving":false, "screenOn":true}
```

Deliberate frugality, because this runs on a phone and reports to a 5 GB
laptop:

- **Coarse location only.** The question is "is he home", not "which room".
  Coordinates leave rounded to three decimals (~110 m). GPS is never requested.
- **No continuous listener.** Fixes arrive through a `PendingIntent`, so the
  platform delivers them and this process does not have to be alive waiting.
- **Events, not polling.** A fix, a charger, a car stereo — plus one heartbeat
  every half hour so a restarted workstation is not stuck on "unknown". A phone
  on a charger overnight in one place sends two payloads a night.
- **home / work** are points *you* save from Settings ("SET HOME HERE"), with a
  300 m radius — a network fix is only good to a few hundred metres and a tight
  radius would flap all evening. `zone` is an extra beyond the agreed presence
  contract; the workstation is free to ignore it.
- **driving** = a car audio or hands-free Bluetooth device connected. Read from
  the broadcast's own `EXTRA_CLASS`, so it needs no query against the device.
- The broadcast receiver is a **disabled manifest component** until the switch
  is on. A "return if off" guard would still have cost a process start on every
  Bluetooth connect, for every user who never turned presence on.

Turning the switch off stops all of it; **FORGET SAVED PLACES** also erases
home, work and the last fix from the phone.

Android insists background location is a *second* request made after the
foreground one is already granted, so Settings asks separately — and says
plainly that presence stops the moment you leave the app until you grant it.

## Comms hub

Three switches, all off until you turn them on, and all listed in Settings
with exactly what each one sends.

- **Notification mirroring** needs a system grant that no app is allowed to
  request — only deep-link to — so the button opens *Settings → Notification
  access* and you turn Cortana on in the list. Ongoing rows (media players,
  downloads, other apps' foreground services) and group summaries are skipped;
  so are this app's own. Nothing is stored: the mirror is a short in-memory
  queue, posted to `/api/comms/sync` in 20-second batches and forgotten. A
  failed POST puts the batch back rather than eating it.
- **Read SMS** exposes recent inbox messages through the system provider. The
  app is not the default SMS handler and does not want to be.
- **Send SMS** lets her send one when asked, split across segments.

The workstation drives the last two with a `{"type":"cmd", "id", "cmd",
"args"}` frame on the WebSocket (`sms.send`, `sms.read`). The phone — not the
workstation — is the authority: a command for a capability you have not
enabled is **refused with a sentence saying so**, POSTed back to
`/api/cmd/result`, never silently dropped. A workstation that hears nothing
cannot tell "switched off" from "broken".

## Finding your way around

Every non-obvious surface carries a **?** — on each card header in the app, on
the Settings sections, on the talk screen, and on the dashboard's MOBILE LINK
module and MIC picker. Tapping it explains what the thing is, where its data
comes from, and what to do when it looks wrong. The copy assumes a technical
reader: it names the actual files, services and commands involved rather than
paraphrasing them.

## Every permission, and why

Nothing here is granted by installing the app. The dangerous ones are asked for
only when you turn the matching switch on, and every one of them is refusable
without breaking anything else.

| Permission | Asked when | For |
|---|---|---|
| `INTERNET`, `REQUEST_INSTALL_PACKAGES`, `RECORD_AUDIO` | install / first talk | the link, self-update, voice |
| `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE` | install | the background link's service |
| `POST_NOTIFICATIONS` (33+) | BACKGROUND on | *everything* the service shows, its own row included |
| `RECEIVE_BOOT_COMPLETED` | install | restart the service after a reboot or update |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | you tap the button | the doze exemption prompt |
| `ACCESS_COARSE_LOCATION` | PRESENCE on | home / work / elsewhere |
| `ACCESS_BACKGROUND_LOCATION` | separately, after the above | so presence survives leaving the app |
| `BLUETOOTH_CONNECT` (31+), `BLUETOOTH` (<=30) | PRESENCE on | receiving the car-stereo connect broadcast |
| `READ_SMS`, `RECEIVE_SMS` | READ SMS on | recent inbox messages |
| `SEND_SMS` | SEND SMS on | sending one when asked |

**Notification access is not in that list** and cannot be: it is a system
switch in *Settings → Notification access*, and no app may request it, only
deep-link to it.

`specialUse` is the service's foreground type rather than `dataSync`, because
`dataSync` acquires a hard daily runtime cap from Android 15 that a
permanently-held socket would hit every day, and none of the other types
describes "holds one long-lived socket to the machine that owns this app".

## Security model

- Pairing code: shown only on the dashboard (bridge `/local/*` endpoints
  answer loopback only), single-use, 10-min expiry, brute-force lockout.
- Phone credential: 256-bit random bearer token, stored in
  EncryptedSharedPreferences; server stores the hash only. Every `/api/*`
  call requires it.
- Revoke anytime: MOBILE LINK module → ✕ next to the phone (two-tap confirm).
  Each row has its own id, so revoking removes exactly that entry; re-pairing a
  phone replaces its row rather than adding a duplicate. To revoke everything,
  delete `mobile_link.json` on the workstation.
- Transport: Tailscale/WireGuard end-to-end; the bridge binds 0.0.0.0 by
  default so LAN also works, but treat the tailnet as the supported path.

## Edge cases, planned for

| Situation | Behavior |
|---|---|
| Workstation asleep / bridge down | Card reads **MOBILE LINK · DISCONNECTED** with what to check; reconnects with backoff (1s→30s) while the app is open |
| `cortana.service` stopped | Dashboard mirror shows OFFLINE — but talking still works: the bridge runs its own orchestrator turns |
| Phone leaves the house (LAN → cellular) | The bridge advertises every address it answers on (Tailscale first, then LAN); the app rotates through them on failure and promotes whichever works. No re-pairing |
| Wi-Fi ↔ LTE switch | Tailscale re-routes; the WS drops and auto-reconnects |
| Voice turn attempted with no route | The reply area answers in Cortana's voice ("I can't reach the workstation… check Tailscale") with the raw error appended for debugging |
| State pushes arriving during a drag | Deferred until the finger lifts, then applied — a drag is never yanked out from under you |
| Calendar showing a slot that isn't in Google | Working-location, focus-time, birthday and declined entries are filtered out; stale state (Cortana down) is cleared and labelled rather than shown as today's |
| Token revoked on the dash | Phone gets a 401 → "Link revoked" prompt → re-pair (no retry hammering) |
| Update while app open | Version arrives in the next state push → pop-up; "Skip this version" is honored until the next release |
| APK missing on workstation | Update check says so; nothing breaks (user hasn't pulled yet, or CI hasn't run) |
| Both desk and phone talk at once | Separate processes; a newer phone utterance cancels the in-flight phone turn (same preemption rule as the desk) |
| Phone-initiated background task finishes later | Completion is announced to the phone as a banner (the bridge reroutes speech announcements) |
| ElevenLabs/OpenAI TTS both down | Reply text + Android TTS — never a dead end |
| Speech too quiet / silence sent | Bridge's silence gate answers "didn't catch that" instead of hallucinating |
| Spotify tokens refreshed by dash and bridge at once | Both re-read the token file before refreshing and write back atomically; a lost rotation race self-heals on re-read |
| Encrypted store won't open (keystore hiccup, often right after an update) | Retried, never deleted — a transient failure used to wipe the token permanently. Only the token lives there; host, device name and layout are in ordinary storage, so worst case is one scanned QR, not a re-setup. The pairing screen says so when storage is genuinely unavailable |
| Phone asks Cortana to restart/shut down | The bridge maps it to `systemctl --user restart/stop cortana` on the workstation |
| Voice replies while phone is on silent | MediaPlayer uses the assistant audio stream; media volume applies |
| hud_state contention (desk turn + phone turn) | Bridge only writes orb state when the desk isn't mid-turn |

## What is checked before a push, and what is not

Kotlin cannot be compiled on the dev box — no gradle, no Android SDK — so CI is
the first compiler this code meets. `mobile/test_mobile_client.py` (stdlib
only, runs in the repo's ordinary pytest) narrows the gap by pinning the
failures that are invisible in a diff:

- every file balances its `(`, `[` and `{` — the classic bad splice, where the
  damage is two hundred lines below the change;
- every `Prefs.x` / `Presence.x` / `Comms.x` / `LinkClient.x` reference names a
  member that object actually declares (~700 references checked);
- every manifest component has a class behind it, and every `R.drawable.x`
  exists;
- every permission the code checks is declared — an undeclared one can only be
  *refused*, forever, silently, on the phone;
- every card type has all three of a `signatureFor` branch, a `buildCard`
  branch and a Help entry (a missing signature branch means the card renders
  once and then never repaints, which reads as a frozen link);
- `versionCode` and `versionName` moved together;
- every capability switch still defaults to off.

**None of that runs a line of Kotlin.** Type errors, resource resolution, doze
survival and every OEM behaviour remain unverified until the APK is on a phone.

## Building locally (optional — CI is the primary path)

Requires Android SDK 34. From `mobile/`: `gradle assembleRelease` (or open in
Android Studio). Output: `app/build/outputs/apk/release/app-release.apk`.
