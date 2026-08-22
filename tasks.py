"""Background task manager: delegated agent work runs on worker threads so the
lead (the voice you talk to) answers instantly and reports results when done.

Concurrency rules (user-chosen):
- Code-touching work (dev agent, self_update) is SERIALIZED behind code_lock -
  two writers on the repo/workspace would corrupt selfedit's git checkpoints.
- Everything else (research/email/trading/video) runs freely in parallel,
  capped at MAX_CONCURRENT.
- Cancellation is cooperative: the flag is checked between agent steps.

Completions: spoken via speech.announce (waits for a quiet moment), logged to
the conversation so the lead remembers them, persisted to the tasks table,
and pushed to the HUD/dashboard thoughts feed.
"""
import itertools
import threading
import time
from contextlib import contextmanager

from config import BUDGET_MONTHLY_USD
import memory
import hud_state
from voice import speech

MAX_CONCURRENT = 4
CODE_AGENTS = {"dev"}          # agents whose work must hold the code lock
ANNOUNCE_CHARS = 320           # spoken completion summary cap
FINISHED_KEEP = 20             # finished records kept in memory (see _prune)

_code_lock = threading.Lock()
_reg_lock = threading.Lock()
_ids = itertools.count(1)
_tasks = {}                    # id -> record dict


def _prune():
    """Drop all but the newest FINISHED_KEEP completed records. Call holding
    _reg_lock. Running and queued tasks are never touched.

    _tasks used to grow for the whole process lifetime, and each record pins
    the full uncapped agent result plus a threading.Event - on a service that
    is Restart=always and therefore effectively never restarts.

    Safe to drop old entries because nothing indexes them: cancel() already
    refuses anything not running/queued, status_summary() lists only the last
    8, and the durable record goes to sqlite via memory.log_task() in _report.
    In particular the phone's "a completion survives the app being closed"
    guarantee rides on bridge.hub's announcement deque, not on this dict - the
    only thing lost is status_summary(id) for a long-finished id.
    """
    done = sorted((t for t in _tasks.values()
                   if t["status"] not in ("running", "queued")),
                  key=lambda t: t["id"])
    for t in done[:-FINISHED_KEEP]:
        _tasks.pop(t["id"], None)


@contextmanager
def code_lock(timeout=0.5):
    """Shared writer lock for self_update and dev tasks. Yields False if a dev
    task holds it (caller should tell the user to wait), True when held."""
    got = _code_lock.acquire(timeout=timeout)
    try:
        yield got
    finally:
        if got:
            _code_lock.release()


def start(agent, task_text, runner):
    """Launch task in the background. runner(agent, task, cancel_event) -> str.
    Returns a short spoken-style status string for the lead to relay."""
    if memory.month_spend() >= BUDGET_MONTHLY_USD:
        return "Can't start the task - the monthly budget cap is reached."
    with _reg_lock:
        running = [t for t in _tasks.values() if t["status"] in ("running", "queued")]
        if len(running) >= MAX_CONCURRENT:
            return (f"All {MAX_CONCURRENT} task slots are busy: "
                    f"{', '.join(_label(t) for t in running)}. Cancel one or wait.")
        tid = next(_ids)
        rec = {"id": tid, "agent": agent, "task": task_text,
               "status": "queued" if agent in CODE_AGENTS and _code_lock.locked() else "running",
               "result": "", "started": time.time(), "finished": None,
               "cancel": threading.Event()}
        _tasks[tid] = rec
    threading.Thread(target=_run, args=(rec, runner), daemon=True,
                     name=f"task-{tid}-{agent}").start()
    memory.log_task(tid, agent, task_text, "started", "")
    hud_state.think(f"task {tid} -> {agent}: {task_text[:80]}")
    queued = rec["status"] == "queued"
    return (f"Task {tid} handed to the {agent} agent"
            + (" - queued behind current code work; it starts automatically." if queued
               else " - running in the background.")
            + " I'll report when it's done.")


def _run(rec, runner):
    needs_lock = rec["agent"] in CODE_AGENTS
    if needs_lock:
        _code_lock.acquire()           # blocks while another code task runs
    try:
        if rec["cancel"].is_set():
            rec["status"], rec["result"] = "cancelled", "cancelled before start"
            return
        rec["status"] = "running"
        try:
            result = runner(rec["agent"], rec["task"], rec["cancel"])
            rec["result"] = str(result or "").strip()
            rec["status"] = "cancelled" if rec["cancel"].is_set() else "done"
        except Exception as e:
            rec["status"], rec["result"] = "failed", f"{type(e).__name__}: {e}"
    finally:
        if needs_lock:
            _code_lock.release()
        rec["finished"] = time.time()
        _report(rec)          # persists to sqlite before the record can be pruned
        with _reg_lock:
            _prune()


def _report(rec):
    tid, agent, status = rec["id"], rec["agent"], rec["status"]
    short = rec["result"][:ANNOUNCE_CHARS]
    memory.log_task(tid, agent, rec["task"], status, rec["result"][:4000])
    # Into the conversation log so the lead knows about it on later turns.
    memory.log_turn("assistant",
                    f"[background task {tid} ({agent}) {status}] {rec['result'][:600]}")
    hud_state.think(f"task {tid} ({agent}) {status}")
    if status == "done":
        speech.announce(f"{agent.capitalize()} agent finished task {tid}. {short}")
    elif status == "failed":
        speech.announce(f"Task {tid} with the {agent} agent failed. {short}")
    elif status == "cancelled":
        speech.announce(f"Task {tid} ({agent}) cancelled.")


def cancel(tid):
    rec = _tasks.get(tid)
    if not rec:
        return f"No task {tid}. {status_summary()}"
    if rec["status"] not in ("running", "queued"):
        return f"Task {tid} already {rec['status']}."
    rec["cancel"].set()
    return (f"Cancelling task {tid} ({rec['agent']}) - it stops at the next step "
            "boundary; a step already mid-flight finishes first.")


def active():
    return [t for t in _tasks.values() if t["status"] in ("running", "queued")]


def _label(t):
    return f"task {t['id']} ({t['agent']})"


def active_summary():
    """Short spoken description of in-flight work, or '' when idle."""
    a = active()
    if not a:
        return ""
    return " and ".join(f"{_label(t)}: {t['task'][:60]}" for t in a)


def status_summary(tid=None):
    if tid is not None:
        rec = _tasks.get(int(tid))
        if not rec:
            return f"No task {tid}."
        age = int(time.time() - rec["started"])
        return (f"Task {rec['id']} ({rec['agent']}) is {rec['status']} "
                f"after {age}s. {rec['result'][:400]}").strip()
    if not _tasks:
        return "No background tasks yet this session."
    lines = []
    for t in sorted(_tasks.values(), key=lambda x: x["id"])[-8:]:
        lines.append(f"task {t['id']} {t['agent']}: {t['status']} - {t['task'][:50]}")
    return "; ".join(lines)
