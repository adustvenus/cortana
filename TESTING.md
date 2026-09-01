# Testing the eight new features on the Linux box

Everything below was written on the Windows dev box, where **Kotlin cannot be
compiled, Electron cannot be run, and no systemd unit exists**. What follows is
therefore the first time most of this code meets a real machine. Work down in
order — the parts are arranged so that a failure early on explains failures
later, and so you never chase a symptom whose cause is two sections above.

Run `bash selftest.sh` first. It is one command and it answers most of the
questions this document would otherwise ask you one at a time.

---

## What actually shipped

| # | Feature | Where it lives | Ships |
|---|---|---|---|
| 4 | Routines engine + morning brief | `routines.py` | on |
| 5 | Workstation control | `tools/desktop.py` | on (needs apt) |
| 6 | Media as a voice tool | `tools/media.py` | on |
| 7 | System / account sentinel | `sentinel.py` | on |
| 8 | Offline wake word | `wakeword.py` | **dormant** |
| 9 | Knowledge layer (notes + recall) | `tools/notes.py` | on |
| 2 | Push delivery + phone notifications | `bridge/`, `mobile/` | needs APK |
| 3 | Presence (desk + phone) | `presence.py`, `bridge/presence_link.py` | desk on |
| 10 | Comms hub (SMS + notification mirror) | `bridge/comms.py`, `mobile/` | **off until you switch it on** |

She gained ten tools: `desktop`, `media`, `note`, `recall`, `routine`,
`routine_set`, `system_check`, `comms_read`, `sms_send`, `wake_correct`.

---

## Part 0 — install and restart

```bash
cd ~/cortana
git pull
bash install.sh                      # installs the X11 helpers that were missing
./venv/bin/pip install -r requirements.txt
systemctl --user restart cortana cortana-bridge cortana-dash
```

Then **read the journal**, do not assume:

```bash
journalctl --user -u cortana -n 60 --no-pager
journalctl --user -u cortana-bridge -n 40 --no-pager
```

`Restart=always` turns a config error into a silent five-second respawn loop
that `systemctl start` reports as **success**. The journal is the only place it
shows, and it only shows the *first* error — fix one, re-read, repeat. Do not
try to predict the second error.

## Part 1 — the automated gate

```bash
bash selftest.sh 2>&1 | tee /tmp/selftest.txt
```

It checks binaries, Python imports, all sixteen new modules, the tool surface,
the prompt cache breakpoint, every sqlite table, all four units, the state
files, all six loopback endpoints, a direct call into each tool, and the test
suite. It is read-only and safe to run while she is talking.

**If anything in Part 1 fails, stop and paste it.** Everything below assumes a
green selftest.

---

## Part 2 — the voice tests

Say each line out loud. The right-hand column is what a working system does.

### Scheduler and routines
| Say | Expect |
|---|---|
| "Cortana, remind me in two minutes to check the oven." | Confirms the **resolved clock time** back ("two minutes — that's 14:32"). Two minutes later she says it. |
| "Cortana, what's scheduled?" | Reads the list back with ids. |
| "Cortana, give me a morning brief every weekday at 7." | Confirms the first run time. Check it became a *scheduled row*, not a second engine: `./venv/bin/python -c "import schedule,json; print(json.dumps(schedule.upcoming(),indent=1))"` → one item, `kind: routine`, `repeats: true`. |
| "Cortana, whenever the system health goes to warning, tell me." | Creates a routine. Verify with `./venv/bin/python -c "import routines; print(routines.items())"`. |

**The edge-trigger test — the one worth doing properly.** Fake a bad reading and
watch two ticks pass:

```bash
printf '{"worst":"bad","checks":[{"key":"disk","label":"Disk","state":"bad","detail":"92 percent full"}],"ts":%s}' "$(date +%s)" > sentinel_state.json
```

She should speak **once**, within ~30s, and then stay silent on every later tick
while the file still says bad. Repeating is the failure. (`sentinel.py` is also
writing that file, so re-write it if the real poll overwrites you.)

### Workstation control
| Say | Expect |
|---|---|
| "Cortana, what windows do I have open?" | A short list. Before `install.sh`, a plain sentence naming the missing package instead — that is correct behaviour, not a bug. |
| "Cortana, what's the volume at?" | A percentage. |
| "Cortana, put a note on my screen saying the build is done." | A desktop toast. If silent, the unit may lack `DBUS_SESSION_BUS_ADDRESS` — see Known risks. |
| "Cortana, type hello into the focused window." | **Refuses**, and says it needs `DESKTOP_TYPE_ENABLED=1`. Off by default on purpose. |

**The ducking test.** With Spotify playing, note `pactl list sink-inputs \| grep -A15 spotifyd \| grep Volume`, then say "Cortana, set the volume to sixty", then talk to her so ducking engages and releases. The spotifyd sink-input volume must return to where it started. A stranded duck is the failure this test exists for.

### Media
| Say | Expect |
|---|---|
| "Cortana, what's playing?" | Track and artist, or that nothing is. |
| "Cortana, play Nightcall by Kavinsky." | Starts on the **desk**, not the phone. |
| "Cortana, pause." | Pauses whatever is actually playing — try it with a browser video too, not just Spotify. |

### Knowledge layer
| Say | Expect |
|---|---|
| "Cortana, note that the greenhouse roof needs new glass before winter." | Echoes the note back so a mishearing is caught immediately. |
| "Cortana, what did I say about the greenhouse?" | Prose with a quote. |
| "Cortana, what did we decide about the scheduler?" | Should find it in the **conversation log**, not just notes. |

Then prove secrets stayed out — run this **after** the index has had a few
minutes, or it proves nothing:

```bash
./venv/bin/python -c "from tools import notes; print(notes.status())"
./venv/bin/python -c "from tools import notes; print(notes.excluded('/home/$USER/Documents/.env'))"
```

The second must print a non-empty reason. An empty index trivially contains no
secrets, so check the file count is non-zero first.

### Sentinel
| Say | Expect |
|---|---|
| "Cortana, how's the system?" | Only what is wrong, briefly. |
| "Cortana, is anything wrong with Google?" | If the OAuth token is near its 7-day death she should say so and name `python main.py --google-auth`. This is the check with the most proven value — that failure has cost real time before. |

---

## Part 3 — the phone

This is the part most likely to need a second pass, and the part I could verify
least. **The APK is already built and released** - CI went green and published
`mobile-v2.5.0`, so it is waiting for you.

1. On the phone: Settings → *Check for update* → install **v2.5.0**. If your
   OEM blocks that, push it over wireless adb: `bash mobile/push-update.sh <port>`.
   (The workstation needs `git pull` first so `mobile/dist` has the new build.)
2. **Grant, in the phone's own system settings** (none of these can be granted
   from inside the app):
   - Notification access (for the notification mirror)
   - SMS permissions (read / send)
   - Background location (for presence)
   - **Battery optimisation exemption** — without this Android kills the socket
     in deep doze and announcements stop arriving. This is the single most
     likely cause of "it worked for an hour then went quiet".
3. **Turn each capability on in the app's Settings.** Everything ships OFF.
4. Test, in order:
   - Close the app entirely, then from the desk say *"Cortana, remind me in one
     minute to test the phone."* The phone should buzz **with the app closed**.
   - Reply to that notification from the shade. It should arrive as a normal
     turn.
   - "Cortana, who texted me?" → reads mirrored messages.
   - "Cortana, text Mum I'm running late." → she **reads it back and sends
     nothing**. Only after you say yes does it send. If she sends on the first
     call, stop and report it — that is a bug with real-world consequences.

---

## Part 4 — what I could not verify, stated plainly

These are not suspicions; they are things no test on the dev box could touch.

- **Kotlin RUNTIME behaviour.** It compiles - CI built and released v2.5.0
  green, so this is no longer a "does it even build" risk. But the foreground
  service, notification channels, RemoteInput reply, geofencing, the
  NotificationListenerService and SMS have still never *run*. Compiling is not
  working.
- **Doze.** Whether the WebSocket actually survives deep doze on your phone,
  with your OEM's battery manager, is unknowable from here.
- **The `cmd` WS channel** (workstation → phone) is a new protocol addition.
  Both halves were written against the same contract but have never spoken to
  each other.
- **`notify-send` from a systemd user unit** needs `DBUS_SESSION_BUS_ADDRESS`
  in the unit environment. `cortana.service` sets `DISPLAY` and `XAUTHORITY`;
  whether it inherits the session bus is unverified. If toasts are silent, check
  that before blaming `notify-send`.
- **`loginctl lock-session`** returns success when the request is *accepted*.
  Whether a locker is running to honour it is a property of the desktop.
- **The weather clause** in the morning brief is the one code path never
  executed anywhere — it needs `BRIEF_ZIP` set in `.env.local`.
- **Spotify under a real 429.** Three processes now share one quota. The
  cool-off file logic is tested; the real rate limit is not.
- **The wake word ships dormant** and is a no-op until you train a model. See
  below.

## Part 5 — the wake word, when you want it

It is deliberately off. Turning it on is a separate exercise on your Windows
box with the 5080:

1. Train a custom "Cortana" model with openWakeWord's synthetic-data pipeline
   (WSL2 + CUDA is the practical route on Windows).
2. Commit the `.onnx` to `voice/models/cortana.onnx`.
3. On the Linux box: `pip install openwakeword onnxruntime`, then set
   `WAKEWORD_ENGINE=openwakeword` in `.env.local` and restart.
4. Test both directions: a podcast playing for a minute must **not** wake her
   (no Whisper calls in the journal), and "ok Cortana" from across the room
   must. Then break it on purpose — `mv voice/models/cortana.onnx /tmp/` — and
   confirm she is still usable rather than deaf. It is built to fail open.

Falling back to Picovoice Porcupine is fine and needs no training run.

---

## Starting the next session

Paste, in this order:

1. The whole output of `bash selftest.sh`.
2. Which numbered items in Parts 2 and 3 failed, and **what she actually said or
   did** — the exact wording matters more than a description of it.
3. For anything that failed: the relevant journal lines.

```bash
journalctl --user -u cortana --since '-15 min' --no-pager | tail -80
journalctl --user -u cortana-bridge --since '-15 min' --no-pager | tail -40
```

That is enough to debug from. It is deliberately more than feels necessary,
because the alternative is a round trip to a machine that cannot be reached
from where the fixing happens.
