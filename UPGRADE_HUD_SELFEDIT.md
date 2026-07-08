# CORTANA — Upgrade: HUD, Self-Edit, Iteration Loop

New files: `hud.py`, `hud_state.py`, `selfedit.py`, `launcher.py`, `.gitignore`.
Modified: `agents.py`, `orchestrator.py`, `main.py`, `cortana.service`,
`requirements.txt`, `config.py`, `voice/mic.py`, `voice/stt.py`, `mic_test.py`.

## 1. Install the one new dependency
```
cd ~/cortana
sudo apt install -y python3-pyqt5     # system Qt libs
./venv/bin/pip install PyQt5
```
If `pip install PyQt5` is slow/fails on this laptop, the apt package alone often
works — test with: `./venv/bin/python -c "import PyQt5; print('ok')"`

## 2. Git must be initialized (self-edit depends on it)
You already set up GitHub, so this is done. Verify:
```
cd ~/cortana && git status
```
Self-edit records a rollback point in `.last_good` and pushes each good change
to GitHub automatically as an offsite backup.

IMPORTANT: because self-edit runs `git add -A` and auto-pushes, the shipped
`.gitignore` MUST stay in place — it keeps `.env`, `credentials.json`,
`token.json`, and `state.db` out of the repo. Without it, your API keys and
Gmail OAuth tokens would be pushed to GitHub on the first self-edit. Verify with
`git check-ignore .env` (it should print `.env`).

## 3. Run — via the launcher now, not main.py
```
./venv/bin/python launcher.py
```
The launcher starts the HUD, then Cortana. On "time to restart" it relaunches
both. If a self-edit ever breaks startup 3x in 60s, it auto-reverts to
`.last_good` before Cortana can even speak — the outer safety net.

(You can still run `./venv/bin/python main.py --text` alone for debugging;
no HUD, no launcher failsafe in that mode.)

## 4. The HUD
- Thin strip pinned to the very top, full width, click-through, always-on-top.
- Waveform grows from screen-center outward, fading toward the top edge.
- Speed/height by state: idle = slow/low, listening/thinking/working = medium,
  speaking = fast/tall. Active agent name shown as centered text.
- Vanishes whenever Cortana is offline/restarting (launcher kills it).

If it doesn't appear: must be an X11 session (`echo $XDG_SESSION_TYPE` = x11).
On some GNOME setups a top strip can hide under the system bar — in `hud.py`
raise `HEIGHT` or the geometry Y offset a few px.

## 5. Self-editing by voice — how it behaves
- "add a feature that does X" / "change your Y to Z" → dev agent writes the FULL
  new file via `self_update`.
- Small & safe (≤2 files, ≤40 changed lines, no deletions) → applied + committed
  automatically.
- Large or deletive → staged as pending, the diff pops open in your text viewer,
  Cortana asks out loud. Say "yes" to apply, "no" to discard.
- Every apply: checkpoint commit first → compile-check → commit if valid, or
  hard-revert if not. Nothing is ever lost.
- "restart" / "time to restart" / "reboot yourself" → clean exit, launcher
  reloads the new code.
- "shut down" / "time to shut down" / "power down" / "go offline" → clean stop;
  the launcher exits and does NOT relaunch (systemd is Restart=on-failure, and a
  shutdown is a clean exit). Comes back on next boot/login, or `systemctl --user
  start cortana`. To keep her off across reboots, `systemctl --user disable cortana`.
  Both commands work two ways: a hard keyword match in `main.py` that fires before
  the LLM (reliable), AND `restart`/`shutdown` tools the agent can call itself
  (e.g. right after applying a self-update). Keyword matches are anchored to the
  whole utterance, so "how do I restart my router" won't trigger them.
- "revert the last change" / "undo that" → rolls back to the previous good state
  (then say restart to load it). The revert is force-with-lease pushed to GitHub
  so the offsite mirror stays in sync with the rewound history.

## 6. Iteration loop (the choppiness fix)
Each request now runs, then a cheap reviewer checks it against what you asked and
feeds back concrete gaps, up to 3 passes, before Cortana speaks once with the
finished result — instead of half-done answers needing another round trip.
Say "keep going" / "try harder" and she can push past the default 3.

## 7. Autostart on boot (optional, when stable)
```
cp ~/cortana/cortana.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cortana
loginctl enable-linger cortana
```
Now points at `launcher.py`, so boot gets HUD + Cortana + crash-revert together.
Keep it OFF (`systemctl --user disable cortana`) until voice/HUD are confirmed
working when launched by hand.

## Failure quick-table (new parts)
| Symptom | Fix |
|---|---|
| No HUD | must be X11; check `import PyQt5` works; bump HEIGHT/Y in hud.py |
| HUD shows but never animates | state file not written — confirm main.py imports hud_state, check ~/cortana/hud_state.json updates |
| self_update says "Not a git repo" | `cd ~/cortana && git init` + one commit |
| Change applied but nothing changed | you must restart to load it — say "restart" |
| Stuck reverting on boot | a committed-good version is broken; `git log --oneline`, `git reset --hard <older-good>` manually |
| Big change never asks | it was under the small-edit threshold and auto-applied; tighten thresholds in selfedit.py |
| Every self-edit says "validation failed, reverted" | validator now uses `sys.executable`; if still failing, run the same `python -m py_compile` by hand to see the real error |
| She never responds to anything spoken | STT silence-gate; confirm `voice/stt.py` reads `dtype="int16"` and lower the `rms < 200` threshold if your mic is quiet |
| Secrets/tokens showed up on GitHub | `.gitignore` missing or edited away — restore it, then `git rm --cached .env credentials.json token.json state.db` and rotate the exposed keys |
