"""Cortana launcher = outer failsafe + process supervisor.

Responsibilities:
  - start the HUD, then cortana (main.py)
  - clean exit (code 0, e.g. "time to restart") -> relaunch both
  - crash loop (nonzero exit repeatedly) -> git reset --hard to .last_good,
    then relaunch. This is the safety net that works even when cortana has
    broken itself so badly it cannot run to speak a rollback.
  - always kills the HUD while cortana is down, restarts it on relaunch

This is the autostart entry (systemd). Never edited by self-update
(kept tiny and stable on purpose).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / "venv" / "bin" / "python")
LAST_GOOD = ROOT / ".last_good"
HUD_STATE = ROOT / "hud_state.json"

CRASH_WINDOW = 60      # seconds
CRASH_LIMIT = 3        # crashes within window -> revert
SHUTDOWN_CODE = 42     # must match config.SHUTDOWN_CODE: main.py exits this to stay off
crash_times = []


def start_hud():
    return subprocess.Popen([PY, str(ROOT / "hud.py")], cwd=ROOT)


def stop_hud(proc):
    """Terminate HUD and fully reap the process so no zombie lingers."""
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)          # give Qt event loop time to shut down cleanly
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()                   # must reap after kill — avoids zombie
    # Belt-and-suspenders: reap any residual zombie regardless of how we got here
    try:
        os.waitpid(proc.pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def wait_for_hud(timeout=5.0):
    """Block until the HUD has written a fresh heartbeat to hud_state.json.
    This guarantees the Qt event loop is running and the window is live
    before main.py calls hud_state.set_state() for the first time.
    Falls through after `timeout` seconds so we never hang indefinitely.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.1)
        try:
            s = json.loads(HUD_STATE.read_text())
            # A ts written within the last 2 s means the HUD poll loop is live
            if time.time() - s.get("ts", 0) < 2.0:
                print("[launcher] HUD ready")
                return
        except Exception:
            pass
    print("[launcher] HUD readiness timeout — continuing anyway")


def revert_to_last_good():
    if not LAST_GOOD.exists():
        return
    good = LAST_GOOD.read_text().strip()
    print(f"[launcher] crash loop -> reverting to {good[:8]}")
    subprocess.run(["git", "reset", "--hard", good], cwd=ROOT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run():
    while True:
        hud = start_hud()
        wait_for_hud()                    # ensure HUD is live before main.py starts
        t0 = time.time()
        proc = subprocess.run([PY, str(ROOT / "main.py")], cwd=ROOT)
        code = proc.returncode
        stop_hud(hud)

        if code == SHUTDOWN_CODE:
            print("[launcher] shutdown requested -> stopping, will not relaunch")
            sys.exit(SHUTDOWN_CODE)

        if code == 0:
            print("[launcher] clean restart requested")
            # No flat sleep — next iteration calls wait_for_hud() which gates on
            # actual HUD liveness, not an arbitrary timer.
            continue

        # crash path
        print(f"[launcher] cortana exited with code {code}")
        now = time.time()
        crash_times.append(now)
        while crash_times and now - crash_times[0] > CRASH_WINDOW:
            crash_times.pop(0)
        if len(crash_times) >= CRASH_LIMIT:
            revert_to_last_good()
            crash_times.clear()
        time.sleep(2)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
