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

## Adding modules

`package/MODULES.md` is the contract — paste its bootstrap prompt plus the
package files into an AI chat, or hand it to Cortana's dev agent.
