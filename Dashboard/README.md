# Dusk Dashboard — Cortana's status/home display

Standalone Electron app wrapping `package/Dusk Dashboard.dc.html`. Fully
offline at runtime (React, Babel, and both font families are vendored in
`package/vendor/`). Completely decoupled from Cortana: it only reads
`hud_state.json` and drives systemd user units, so restarting either side
never affects the other. It replaces the old `hud.py` strip (still in the
repo, disabled; re-enable with `CORTANA_LEGACY_HUD=1`).

## Install (on the Linux machine)

```bash
cd ~/cortana && git pull
bash Dashboard/install-dash.sh     # npm-installs Electron (one-time network), icon, service
systemctl --user start cortana-dash
```

Requires: Node.js + npm, an X11 session (the compositor must be on for the
transparent bubble), systemd user session (already required by cortana.service).

## Behavior

| Situation | What happens |
|---|---|
| 2+ screens | Frameless fullscreen dashboard on the non-primary screen |
| 1 screen | Hidden; floating always-on-top **bubble orb** top-left |
| Click bubble | Opens the fullscreen dashboard |
| Esc / minimize / close (X) | Back to the bubble — close never quits |
| Screen plugged/unplugged | Re-evaluates automatically |
| Really quit | Tray menu, or right-click the bubble |
| Launched twice | Second launch focuses the existing instance |

## The AI module

The only module on the default board. Shows: pulsing orb (color/speed follow
state: idle / listening / thinking / working / speaking; grey when offline),
state line, live "thinking" feed, and a hud.py-style waveform strip.
**Click the orb** for the power menu: START / RESTART / SHUT DOWN — these run
`systemctl --user <action> cortana`.

Liveness comes from `systemctl --user is-active`, not the state file's
timestamp (Cortana intentionally stops rewriting the file while idle).

The module also has a **MIC** picker (visible at larger module sizes): click
the device name to cycle through every input device the machine currently has.
Cortana publishes them to `mic_state.json`; your pick is written to
`mic_select.json` and re-read on her next capture — no restart. The choice is
stored by *name*, so it survives replugging and overrides `MIC_NAME` /
`MIC_DEVICE` in `.env`. Showing `—` means no input devices were found (or
Cortana isn't running). The **?** beside it explains all of this in place.

With no microphone at all she stays up rather than crashing: F9 answers "I have
no input device", F10 refuses to leave push-to-talk, and plugging one in needs
no restart.

## Multiple agents

`app/agents.json` is the registry. Add an entry (id, name, stateFile,
systemdUnit) and relaunch — extra agents appear as compact rows in the AI
module with their own restart/power buttons. Agents without a `systemdUnit`
are status-only. State files use the `hud_state.json` shape:
`{"state": "...", "agent": "", "detail": "", "thoughts": [], "ts": epoch}`.

## Edge cases & recovery

- **Killed the process / closed everything?** Launch "Dusk Dashboard" from the
  application menu (or `systemctl --user start cortana-dash`). The systemd
  unit also restarts it automatically on crashes.
- **Cortana down?** Dashboard stays up, module shows OFFLINE, orb menu offers START.
- **Corrupt/missing hud_state.json?** Module shows OFFLINE; no crash.
- **Layout broke / want a clean board?** Edit mode (⌘/Ctrl-E) → RESET LAYOUT.
- **Self-edit safety:** Cortana's self-edit layer refuses to touch
  `Dashboard/app/`, `package/support.js`, and `package/vendor/` (see
  `selfedit.py PROTECTED`). Module authoring in the `.dc.html` stays allowed,
  per `package/MODULES.md`.

## MOBILE LINK module (phone pairing)

Add it from the edit tray. It talks (loopback-only) to the mobile bridge
service (`cortana-bridge`, see `../bridge/README.md` and `../mobile/README.md`)
and is the dashboard end of the phone link:

- **Status** — bridge up/down, this machine's name and port, how many phones
  are live.
- **Devices** — each paired phone by name with ONLINE / SEEN *time*. Tap ✕
  twice to revoke one instantly.
- **PAIR A PHONE** — issues a 6-digit code (single use, 10-minute life, 5 wrong
  tries locks pairing for 5 minutes) **and a QR code**. Scanning the QR with a
  phone camera downloads the app from this machine and pairs it in one step —
  nothing to type.
- **Board snapshot** — pushes module order, tasks and the weather ZIP to the
  bridge every 20s so the phone mirrors this exact board.
- **?** — explains all of the above in place.

If it shows BRIDGE OFFLINE: `systemctl --user start cortana-bridge`.

## Editing the board

Ctrl-E toggles edit mode. Each module gets: drag to move, **◢** (bottom-right)
and **◤** (top-left) resize handles, ∞ link, ◐ surface, ✕ remove (tap twice).
The **▼** button beside DONE EDITING collapses the tray so the whole board is
visible while you arrange; edit handles stay active.

The engine refuses to commit a layout where modules overlap (toast: BLOCKED),
and a saved layout that somehow contains overlaps self-heals on load —
overlappers are relocated to free space rather than left stacked.

## Sleep mode (burn-in guard)

Click the AI orb → **SLEEP SCREEN**: the display turns off while the machine
(and Cortana) keep running. It wakes on a **keyboard press only** — pointer
devices are disabled while dark, so a nudged mouse can't relight the panel,
and they're re-enabled automatically on wake (or on any exit path, so a crash
can't strand the mouse off). Uses `xset` + `xinput`; without `xinput` the
screen still sleeps, the mouse just becomes a wake source too.

## Adding modules

`package/MODULES.md` is the contract — paste its bootstrap prompt plus the
package files into an AI chat, or hand it to Cortana's dev agent.
