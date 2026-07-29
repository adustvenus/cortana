"""APK publishing: what the workstation currently has, how to refresh it from
CI, and how to push it to a phone when that phone's own installer refuses.

The phone updates from mobile/dist on this machine (CI commits builds there),
never from GitHub directly - the repo is private and the phone holds no
credentials for it.
"""
import json
import subprocess

from bridge import util
from bridge.settings import DIST, ROOT


def apk_info():
    """{version, apk, available} for the build currently on this machine."""
    def read():
        j = json.loads((DIST / "version.json").read_text())
        name = str(j.get("apk", ""))
        return {"version": str(j.get("version", "")), "apk": name,
                "available": bool(name) and (DIST / name).exists()}
    return util.cached("apk", 60, read)


def apk_path():
    """Absolute path of the current APK, or None when nothing is published."""
    info = apk_info()
    return DIST / info["apk"] if info.get("available") else None


def refresh_dist():
    """Phone-triggered 'check for updates': fast-forward the repo so mobile/dist
    matches what CI last published - the user should never have to remember to
    git pull just to update their phone.

    Only ever a --ff-only pull on a clean tree, so it can neither create
    conflicts nor discard local work; a dirty tree is reported, not forced.
    """
    dirty = util.run(["git", "-C", str(ROOT), "status", "--porcelain"], timeout=15)
    if dirty:
        return {"ok": False, "pulled": False,
                "error": "repo has local changes - pull manually on the workstation"}
    try:
        out = util.run(["git", "-C", str(ROOT), "pull", "--ff-only"], timeout=90)
    except Exception as e:
        return {"ok": False, "pulled": False, "error": f"pull failed: {e}"[:200]}
    util.invalidate("apk", "git")
    return {"ok": True, "pulled": "Already up to date" not in out, "apk": apk_info()}


def adb_install(addr):
    """Install the current APK to a phone over wireless adb.

    This is the privileged install path: it bypasses the system installer UI
    that OxygenOS/ColorOS drop silently. Triggered from the phone itself so the
    user never touches a terminal. Requires Wireless debugging on the phone and
    a prior one-time `adb pair` (see mobile/push-update.sh).
    """
    apk = apk_path()
    if apk is None:
        return {"ok": False, "error": "no APK on the workstation yet"}
    try:
        util.run(["adb", "connect", addr], timeout=20)
    except FileNotFoundError:
        return {"ok": False, "error": "adb not installed on the workstation "
                                      "(sudo apt install -y android-tools-adb)"}
    except Exception as e:
        return {"ok": False, "error": f"adb connect failed: {e}"[:200]}
    try:
        out = subprocess.run(["adb", "-s", addr, "install", "-r", str(apk)],
                             capture_output=True, text=True, timeout=300)
    except Exception as e:
        return {"ok": False, "error": f"adb install failed: {e}"[:200]}
    blob = (out.stdout + out.stderr).strip()
    ok = "Success" in blob
    if not ok and "unauthorized" in blob.lower():
        return {"ok": False, "error": "phone not paired for adb - run once on the "
                                      "workstation: bash mobile/push-update.sh pair "
                                      "<IP:PORT> <CODE>"}
    return {"ok": ok, "version": apk_info().get("version", ""), "output": blob[:300]}
