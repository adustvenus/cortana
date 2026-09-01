#!/usr/bin/env bash
# Cortana self-test. Run on the LINUX box, paste the whole output.
#
#   bash selftest.sh
#
# This exists because of the two-machine problem in CLAUDE.md: the box that runs
# Cortana is not the box she is edited on, and a debugging round trip between
# them is expensive. One command that reports EVERYTHING at once beats ten that
# each confirm a hypothesis.
#
# It is read-only. It starts nothing, stops nothing, installs nothing, and sends
# nothing anywhere. Safe to run at any time, including while she is talking.
#
# Deliberately NOT `set -e`: a failing check is data, not a reason to stop.

cd "$(dirname "$0")" || exit 1
# Same probe as install-services.sh and install.sh: this repo has used three
# venv names over time, and reporting on the wrong interpreter would say a
# dependency is missing when it is installed in the one the service uses.
# RUNS the interpreter rather than testing the executable bit: a venv whose base
# python was upgraded away still has an executable python that cannot execute,
# and reporting every import as missing off the back of that would send the next
# debugging session in entirely the wrong direction.
PY=""
for d in venv cortana_venv .venv; do
    if [ -d "$d" ] && "$d/bin/python" -c "" 2>/dev/null; then PY="./$d/bin/python"; break; fi
done
if [ -z "$PY" ]; then
    for d in venv cortana_venv .venv; do
        [ -d "$d" ] && echo "  WARNING: $d/ exists but its python will not run - venv is broken, run: bash install.sh"
    done
    PY="python3"
fi
BRIDGE_PORT="${BRIDGE_PORT:-8765}"
OK=0; BAD=0

hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
pass() { printf '  ok   %-30s %s\n' "$1" "$2"; OK=$((OK+1)); }
fail() { printf '  FAIL %-30s %s\n' "$1" "$2"; BAD=$((BAD+1)); }
note() { printf '       %-30s %s\n' "$1" "$2"; }

# curl on this box is the SNAP build, which has a private /tmp: `curl -o FILE`
# fails with exit 23 while the transfer itself succeeds. Never let curl open its
# own output file here - redirect and let the unconfined shell do the open().
get() { curl -s --max-time 5 "http://127.0.0.1:${BRIDGE_PORT}$1" 2>/dev/null; }

echo "Cortana self-test  --  $(date -Is)  --  $(hostname)"
echo "commit $(git rev-parse --short HEAD 2>/dev/null)  branch $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"

# ── 1. host ────────────────────────────────────────────────────────────────
hdr "host"
note "python" "$($PY -V 2>&1) at $PY"
note "memory" "$(free -m 2>/dev/null | awk '/^Mem:/{print $7" MB available of "$2" MB"}')"
note "disk" "$(df -h / 2>/dev/null | awk 'NR==2{print $4" free of "$2}')"
for b in wmctrl xdotool xclip xprintidle playerctl pdftotext notify-send pactl ffmpeg; do
    if command -v "$b" >/dev/null 2>&1; then pass "bin:$b" "$(command -v $b)"
    else fail "bin:$b" "MISSING -> bash install.sh"; fi
done
note "DISPLAY" "${DISPLAY:-(unset - X11 actions will refuse)}"

# ── 2. python deps ─────────────────────────────────────────────────────────
# Checked against requirements.txt itself, not a list copied out of it. A
# hand-maintained list answers "do the imports I remembered work", which is a
# different and weaker question than "is this environment what the manifest
# says it should be" - and it silently stops covering anything added later.
hdr "python deps (vs requirements.txt)"
$PY - <<'EOF'
import re
from importlib.metadata import version, PackageNotFoundError
missing = []
try:
    lines = open("requirements.txt", encoding="utf-8").read().splitlines()
except Exception as e:
    print("  FAIL cannot read requirements.txt: %s" % e)
    lines = []
for raw in lines:
    line = raw.split("#")[0].strip()
    if not line:
        continue
    name = re.split(r"[<>=!~\[]", line)[0].strip()
    try:
        print("  ok   %-26s %s" % (name, version(name)))
    except PackageNotFoundError:
        print("  FAIL %-26s NOT INSTALLED" % name)
        missing.append(name)
if missing:
    print("       -> ./venv/bin/python -m pip install -r requirements.txt")
EOF

# ── 3. every new module actually imports ───────────────────────────────────
hdr "feature modules"
$PY - <<'EOF'
import importlib
mods = ["schedule", "notify", "presence", "routines", "sentinel", "wakeword",
        "tools.desktop", "tools.media", "tools.notes",
        "bridge.inbox", "bridge.client", "bridge.comms", "bridge.cmdchan",
        "bridge.presence_link", "bridge.scheduler", "bridge.watch"]
for m in mods:
    try:
        importlib.import_module(m)
        print("  ok   import:%-24s" % m)
    except Exception as e:
        print("  FAIL import:%-24s %s: %s" % (m, type(e).__name__, e))
EOF

# ── 4. tool surface ────────────────────────────────────────────────────────
hdr "tool surface"
$PY - <<'EOF'
try:
    import memory; memory.init()
    import agents
    names = agents.LEAD_TOOL_NAMES
    missing = [n for n in names if n not in agents.TOOL_DEFS]
    print("  %s lead tools%-19s %d exposed" % ("ok  " if not missing else "FAIL", "", len(names)))
    if missing:
        print("       missing schemas:", missing)
    for want in ("remind", "routine", "desktop", "media", "note", "recall",
                 "system_check", "comms_read", "sms_send", "wake_correct"):
        print("  %s tool:%-25s" % ("ok  " if want in names else "FAIL", want))
    blocks = agents.lead_system()
    cached = [i for i, b in enumerate(blocks) if "cache_control" in b]
    clock_last = "## Now" in blocks[-1]["text"]
    print("  %s prompt cache%-18s breakpoint at %s, clock last=%s"
          % ("ok  " if cached == [0] and clock_last else "FAIL", "", cached, clock_last))
except Exception as e:
    print("  FAIL agents.py                    %s: %s" % (type(e).__name__, e))
EOF

# ── 5. database ────────────────────────────────────────────────────────────
hdr "database"
$PY - <<'EOF'
try:
    import memory
    memory.init()
    con = memory.connect()
    have = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    for t in ("log", "kv", "meta", "usage", "address_log", "tasks",
              "schedules", "routines", "deliveries", "comms"):
        print("  %s table:%-24s" % ("ok  " if t in have else "FAIL", t))
    mode = con.execute("pragma journal_mode").fetchone()[0]
    print("  %s journal_mode%-18s %s" % ("ok  " if mode.lower() == "wal" else "FAIL", "", mode))
    n = con.execute("select count(*) from schedules where state='pending'").fetchone()[0]
    print("       pending schedules             %d" % n)
    con.close()
except Exception as e:
    print("  FAIL sqlite                       %s: %s" % (type(e).__name__, e))
EOF

# ── 6. services ────────────────────────────────────────────────────────────
hdr "services"
for u in cortana cortana-bridge cortana-dash cortana-spotifyd; do
    s=$(systemctl --user is-active "$u" 2>/dev/null)
    if [ "$s" = "active" ]; then pass "unit:$u" "active"
    else
        # Restart=always turns a config error into a silent respawn loop that
        # `systemctl start` reports as success, so show the last real line.
        why=$(journalctl --user -u "$u" -n 3 --no-pager 2>/dev/null | tail -1 | cut -c1-90)
        fail "unit:$u" "$s  |  $why"
    fi
done

# ── 6b. is the running code the code on disk? ──────────────────────────────
# Python caches an imported module for the life of the process, and most tools
# here are imported lazily on first use. So a service started before a git pull
# keeps serving the OLD module while a fresh `python` on the same box gets the
# new file - the same command works from a shell and fails by voice, with
# nothing in any log to say why. It cost a full debugging round once.
hdr "code freshness"
newest=$(find . -name '*.py' -not -path './venv/*' -not -path './cortana_venv/*'               -not -path '*/__pycache__/*' -printf '%T@ %p
' 2>/dev/null |
         sort -rn | head -1)
newest_ts=${newest%% *}; newest_ts=${newest_ts%%.*}
note "newest .py" "$(date -d @${newest_ts:-0} 2>/dev/null) ${newest##* }"
for u in cortana cortana-bridge; do
    started=$(systemctl --user show "$u" -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
    epoch=$(systemctl --user show "$u" -p ActiveEnterTimestamp --value 2>/dev/null)
    if [ -z "$epoch" ]; then note "$u" "(not running)"; continue; fi
    started_ts=$(date -d "$epoch" +%s 2>/dev/null || echo 0)
    if [ "${newest_ts:-0}" -gt "${started_ts:-0}" ]; then
        fail "stale:$u" "started $epoch, BEFORE the newest source edit -> systemctl --user restart $u"
    else
        pass "fresh:$u" "started after the last source change"
    fi
done

# ── 7. state files ─────────────────────────────────────────────────────────
hdr "state files"
now=$(date +%s)
for f in hud_state.json calendar_state.json presence_desk.json sentinel_state.json speech_inbox.json mic_state.json; do
    if [ -f "$f" ]; then
        age=$(( now - $(stat -c %Y "$f" 2>/dev/null || echo "$now") ))
        note "$f" "${age}s old"
    else
        note "$f" "(absent - normal if that producer has not run yet)"
    fi
done

# ── 8. bridge endpoints ────────────────────────────────────────────────────
hdr "bridge endpoints (loopback)"
for ep in /local/status /local/schedule /local/routines /local/sentinel /local/presence /local/comms; do
    body=$(get "$ep")
    if [ -z "$body" ]; then
        fail "GET $ep" "no response - is cortana-bridge up?"
    elif printf '%s' "$body" | grep -q '"error"'; then
        fail "GET $ep" "$(printf '%s' "$body" | cut -c1-100)"
    else
        pass "GET $ep" "$(printf '%s' "$body" | cut -c1-70)"
    fi
done

# ── 9. the tools, called directly ──────────────────────────────────────────
hdr "tool smoke test (no voice, no API calls)"
$PY - <<'EOF'
import memory
memory.init()
checks = [
    # poll() first, exactly as the system_check tool's dispatch does. Calling
    # speakable() alone in a FRESH process reports "I have not run the system
    # checks yet" - true of this process, and completely misleading about the
    # running one, which has been polling for minutes.
    ("sentinel",  lambda: (__import__("sentinel").poll(),
                           __import__("sentinel").speakable())[1]),
    ("desktop",   lambda: __import__("tools.desktop", fromlist=["x"]).desktop({"action": "volume"})),
    ("media",     lambda: __import__("tools.media", fromlist=["x"]).media("status")),
    ("notes",     lambda: __import__("tools.notes", fromlist=["x"]).status()),
    ("wakeword",  lambda: __import__("wakeword").reason()),
    ("presence",  lambda: __import__("presence").read_desk()),
    ("schedule",  lambda: __import__("schedule").summary()),
    ("routines",  lambda: str(__import__("routines").items())),
]
for name, fn in checks:
    try:
        out = str(fn()).replace("\n", " ")[:78]
        print("  ok   %-14s %s" % (name, out))
    except Exception as e:
        print("  FAIL %-14s %s: %s" % (name, type(e).__name__, str(e)[:60]))
EOF

# ── 10. what the sentinel actually says ────────────────────────────────────
# Printed in full rather than truncated into the endpoint line above: a `worst`
# of bad names no row, and "something is wrong" without saying what is the least
# useful thing a health check can report.
hdr "sentinel rows"
$PY - <<'EOF'
try:
    import sentinel
    sentinel.poll()
    rows = sentinel.rows()
    if not rows:
        print("  (no rows - the checks have not produced anything yet)")
    for c in rows:
        mark = {"ok": "  ok  ", "warn": "  WARN", "bad": "  BAD "}.get(c["state"], "  ?   ")
        print("%s %-10s %s" % (mark, c["label"], c["detail"]))
except Exception as e:
    print("  FAIL sentinel: %s: %s" % (type(e).__name__, e))
EOF

# ── 11. the suite ──────────────────────────────────────────────────────────
hdr "unit tests"
# pytest is NOT in requirements.txt - it is a test dependency, and the runtime
# box does not need it to run Cortana. Say that plainly instead of reporting a
# bare "No module named pytest", which reads like something is broken.
if $PY -c "import pytest" 2>/dev/null; then
    $PY -m pytest -q 2>&1 | tail -4
else
    echo "  skipped - pytest not installed (not needed to RUN Cortana)."
    echo "  To enable the suite here:  $PY -m pip install pytest"
fi

hdr "summary"
echo "  shell checks: $OK ok, $BAD failed"
echo
echo "Paste this whole output into the next Claude session."
