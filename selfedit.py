"""Self-edit safety layer. Every change is git-checkpointed and validated.

Guarantees:
- A last-known-good commit hash is always recorded in .last_good
- Small safe edits apply automatically
- Large/destructive edits stage as 'pending' and require a spoken yes
- Any validation failure hard-reverts to last-good
- 'revert' rolls back to the previous good commit

The launcher provides the outer failsafe: if new code won't even boot,
it reads .last_good and resets before cortana can speak.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAST_GOOD = ROOT / ".last_good"
PENDING = ROOT / "pending_edit.json"

# thresholds for auto-apply vs. ask
MAX_FILES_AUTO = 2
MAX_NET_LINES_AUTO = 40


def _git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _ensure_repo():
    if not (ROOT / ".git").exists():
        raise RuntimeError("Not a git repo. Run git init in the project first.")


def current_commit():
    return _git("rev-parse", "HEAD")


def save_last_good():
    LAST_GOOD.write_text(current_commit())


def get_last_good():
    if LAST_GOOD.exists():
        return LAST_GOOD.read_text().strip()
    return current_commit()


def checkpoint(msg="checkpoint"):
    """Commit pending *tracked* changes so nothing is lost before an edit.

    Only tracked files (`git add -u`) - never untracked scratch/test files, so
    stray .wav/log/temp files can't ride into a commit and get pushed.
    """
    _ensure_repo()
    _git("add", "-u")
    if _git("diff", "--cached", "--name-only"):
        _git("commit", "-m", f"auto-checkpoint: {msg}", check=False)
    if not LAST_GOOD.exists():
        save_last_good()


def _validate():
    """Compile every python file. Returns (ok, message)."""
    py = [str(p) for p in ROOT.rglob("*.py")
          if "venv" not in p.parts and "__pycache__" not in p.parts]
    r = subprocess.run([sys.executable, "-m", "py_compile", *py],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip()[:500]
    return True, "ok"


def _size(files):
    """(#files, net_line_delta, has_delete) for a set of pending writes."""
    net = 0
    for f in files:
        path = ROOT / f["path"]
        old = path.read_text().count("\n") if path.exists() else 0
        new = f["content"].count("\n")
        net += abs(new - old)
    has_delete = any(f.get("delete") for f in files)
    return len(files), net, has_delete


def apply_edit(files, description, force=False):
    """files: [{path, content} | {path, delete:True}]. Returns status string.

    Small & safe -> applied now. Large -> staged pending, needs confirm_pending().
    """
    _ensure_repo()
    nfiles, net, has_delete = _size(files)
    big = (nfiles > MAX_FILES_AUTO or net > MAX_NET_LINES_AUTO or has_delete)

    if big and not force:
        PENDING.write_text(json.dumps({"files": files, "description": description}))
        diff_preview = "\n".join(
            f"  {'DELETE ' if f.get('delete') else ''}{f['path']}" for f in files)
        _write_diff_file(files)
        return ("PENDING_CONFIRM", f"Large change: {nfiles} files, ~{net} lines"
                f"{', includes deletions' if has_delete else ''}.\n{diff_preview}")

    return _do_apply(files, description)


def _do_apply(files, description):
    checkpoint(f"before: {description}")
    good = current_commit()
    try:
        for f in files:
            path = ROOT / f["path"]
            if f.get("delete"):
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f["content"])
        ok, msg = _validate()
        if not ok:
            _git("reset", "--hard", good)
            return ("FAILED", f"Validation failed, reverted. {msg}")
        # Stage ONLY the files in this edit (adds/mods/deletes) - never a blanket
        # `git add -A`, which used to sweep untracked junk into self-updates.
        _git("add", "-A", "--", *[f["path"] for f in files])
        _git("commit", "-m", f"self-update: {description}")
        save_last_good()
        _push_backup()
        return ("APPLIED", f"Applied and committed: {description}")
    except Exception as e:
        _git("reset", "--hard", good, check=False)
        return ("FAILED", f"Error, reverted to safe state: {e}")


def confirm_pending():
    if not PENDING.exists():
        return ("NONE", "No pending change to confirm.")
    data = json.loads(PENDING.read_text())
    PENDING.unlink()
    return _do_apply(data["files"], data["description"])


def cancel_pending():
    if PENDING.exists():
        PENDING.unlink()
        return ("CANCELLED", "Discarded the pending change.")
    return ("NONE", "Nothing pending.")


def revert_last():
    """Roll back to the previous good commit (undo the last applied change)."""
    _ensure_repo()
    try:
        _git("reset", "--hard", "HEAD~1")
        save_last_good()
        _push_backup(force=True)  # revert rewinds history; lease-force keeps the offsite mirror in sync
        return ("REVERTED", "Rolled back the last change. Restart to load it.")
    except Exception as e:
        return ("FAILED", f"Revert failed: {e}")


def _write_diff_file(files):
    """Dump a human-readable preview and open it for the user to eyeball."""
    lines = []
    for f in files:
        lines.append(f"=== {'DELETE ' if f.get('delete') else ''}{f['path']} ===")
        if not f.get("delete"):
            lines.append(f["content"][:4000])
        lines.append("")
    (ROOT / "pending_diff.txt").write_text("\n".join(lines))
    subprocess.Popen(["xdg-open", str(ROOT / "pending_diff.txt")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _push_backup(force=False):
    """Best-effort offsite backup. Never blocks or raises.

    force=True uses --force-with-lease so a revert (which rewinds HEAD) can still
    update the mirror without a non-fast-forward rejection, while refusing to
    clobber commits it hasn't seen.
    """
    cmd = ["git", "push"]
    if force:
        cmd.append("--force-with-lease")
    cmd += ["origin", "HEAD"]
    subprocess.Popen(cmd, cwd=ROOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
