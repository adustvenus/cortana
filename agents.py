"""Tool schemas, subagent definitions, tool dispatcher."""
import datetime

from config import (CACHE_ENABLED, CACHE_TTL, MODEL_LEAD, MODEL_FAST,
                    MODEL_HEAVY, ROOT, SCHED_TZ, WORKSPACE)
import memory
from tools import files as F
# vision / trading / video / gmail are imported lazily inside dispatch() so a
# missing hardware lib (e.g. mss on a headless box) never kills the whole app.


# Documents an agent should have verbatim in context. These are big, stable and
# read on every step, which is exactly what prompt caching is for: without it
# the dev agent must rediscover the module contract by reading files, and with
# it uncached MODULES.md would be re-billed on every step of every task.
REFERENCE_DOCS = {
    "dev": ("Dashboard/package/MODULES.md",),
}


def _doc(rel):
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _cached(blocks):
    """Put the cache breakpoint on the last block given.

    Everything up to and including it is re-used across turns; anything appended
    AFTER it may change freely without invalidating the prefix. Caching is a
    prefix match, so ordering is the whole game - one volatile byte early in the
    prefix costs you the entire cache."""
    if CACHE_ENABLED and blocks:
        blocks[-1] = {**blocks[-1],
                      "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL}}
    return blocks


def agent_system(name):
    """System blocks for a subagent: its brief, then its reference documents,
    with the breakpoint after them."""
    a = AGENTS[name]
    blocks = [{"type": "text", "text": a["system"]}]
    for rel in REFERENCE_DOCS.get(name, ()):
        doc = _doc(rel)
        if doc:
            header = "# Reference document: " + rel
            blocks.append({"type": "text",
                           "text": header + "\n\n" + doc})
    return _cached(blocks)


def _schema(name, desc, props, req):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": req}}


class RestartRequested(Exception):
    """Raised by the restart tool; main.py catches it and exits code 0."""


class ShutdownRequested(Exception):
    """Raised by the shutdown tool; main.py exits with config.SHUTDOWN_CODE so
    the launcher stops instead of relaunching."""


TOOL_DEFS = {
    "shell": _schema("shell", "Run a bash command in the workspace. Full autonomy, no approval needed.",
                     {"command": {"type": "string"}}, ["command"]),
    "read_file": _schema("read_file", "Read a text file in the workspace.",
                         {"path": {"type": "string"}}, ["path"]),
    "write_file": _schema("write_file", "Create/overwrite a text file in the workspace.",
                          {"path": {"type": "string"}, "content": {"type": "string"}},
                          ["path", "content"]),
    "list_files": _schema("list_files", "List workspace files.",
                          {"path": {"type": "string"}}, []),
    "screenshot": _schema("screenshot", "Capture the user's screen. Use when asked 'what is this', 'look at this', chart analysis, etc.",
                          {}, []),
    "gmail_search": _schema("gmail_search", "Search Gmail. Standard Gmail query syntax.",
                            {"query": {"type": "string"},
                             "max_results": {"type": "integer"}}, ["query"]),
    "gmail_read": _schema("gmail_read", "Read one email by id from gmail_search.",
                          {"id": {"type": "string"}}, ["id"]),
    "gmail_draft": _schema("gmail_draft", "Create a DRAFT email. Never sends.",
                           {"to": {"type": "string"}, "subject": {"type": "string"},
                            "body": {"type": "string"}}, ["to", "subject", "body"]),
    "quote": _schema("quote", "Current quote. Futures need =F suffix: ES=F, NQ=F, CL=F, GC=F.",
                     {"symbol": {"type": "string"}}, ["symbol"]),
    "history": _schema("history", "OHLCV history CSV. period e.g. 1d,5d,1mo; interval e.g. 1m,15m,1h,1d.",
                       {"symbol": {"type": "string"}, "period": {"type": "string"},
                        "interval": {"type": "string"}}, ["symbol"]),
    "video": _schema("video", "Run ffmpeg/ffprobe. Pass args after program name, paths relative to workspace.",
                     {"args": {"type": "string"}}, ["args"]),
    "continue_work": _schema("continue_work",
                             "Resume work that stopped at its step limit, with the full "
                             "transcript intact. Use when the user asks you to continue, "
                             "keep going, or finish something you ran out of steps on - "
                             "never redo the task from scratch instead. 'which' is 'lead' "
                             "for your own work, or the agent name (e.g. 'dev').",
                             {"which": {"type": "string"},
                              "nudge": {"type": "string"}}, []),
    "remember": _schema("remember", "Save a permanent fact/preference about the user.",
                        {"key": {"type": "string"}, "value": {"type": "string"}},
                        ["key", "value"]),
    "remind": _schema("remind",
                      "Schedule something for later: a reminder, timer, alarm, or recurring "
                      "job. 'when' is a LOCAL ISO-8601 timestamp which YOU compute from the "
                      "clock in the '## Now' section of your system prompt - 'in 20 minutes' "
                      "means that clock plus 20 minutes. 'rrule' makes it recurring (RFC 5545, "
                      "e.g. FREQ=DAILY;BYHOUR=7;BYMINUTE=0) and 'when' is then the first "
                      "occurrence. kind is timer|alarm|reminder and only changes how long a "
                      "late one is still worth speaking. Default action speaks 'text'; use "
                      "action='turn' with 'prompt' when it needs fresh thinking AT THE TIME "
                      "(a morning briefing), or action='delegate' with agent+task for "
                      "background work. urgency ambient|normal|urgent|critical - critical "
                      "reaches every surface, so it is for alarms only. ALWAYS say the "
                      "resolved time back to the user so a misread is caught out loud.",
                      {"text": {"type": "string"}, "when": {"type": "string"},
                       "rrule": {"type": "string"}, "kind": {"type": "string"},
                       "urgency": {"type": "string"}, "action": {"type": "string"},
                       "prompt": {"type": "string"}, "agent": {"type": "string"},
                       "task": {"type": "string"}, "require_ack": {"type": "boolean"}},
                      ["text", "when"]),
    "schedule_list": _schema("schedule_list",
                             "Everything scheduled, with ids and next fire times. Use it "
                             "before cancelling so you cancel the right one.",
                             {"include_done": {"type": "boolean"}}, []),
    "schedule_set": _schema("schedule_set",
                            "Change one scheduled item by id (from schedule_list). ack=true "
                            "stops one that is still reminding you; cancel=true drops it and "
                            "every future occurrence.",
                            {"id": {"type": "integer"}, "ack": {"type": "boolean"},
                             "cancel": {"type": "boolean"}}, ["id"]),
    "delegate": _schema("delegate",
                        "Hand a task to a specialist subagent. Agents: research (web), email (gmail), "
                        "trading (markets), video (editing), dev (coding/apps). background=true runs it "
                        "on a worker and returns instantly - you MUST then tell the user it's underway; "
                        "the result is announced automatically when done. background=false blocks and "
                        "returns the result inline - ONLY for quick lookups (one search, one quote) "
                        "whose answer you need to finish THIS reply.",
                        {"agent": {"type": "string"}, "task": {"type": "string"},
                         "background": {"type": "boolean"}},
                        ["agent", "task", "background"]),
    "task_status": _schema("task_status",
                           "Status of background tasks. Optional id for one task's detail.",
                           {"id": {"type": "integer"}}, []),
    "cancel_task": _schema("cancel_task", "Cancel a running background task by id.",
                           {"id": {"type": "integer"}}, ["id"]),
    "self_update": _schema("self_update", "Edit Cortana's OWN source code. Provide the FULL new content of each file. Small safe edits apply automatically; large ones ask the user to confirm out loud. Always git-checkpointed and auto-reverted if it fails to compile.",
                           {"files": {"type": "array", "items": {
                               "type": "object",
                               "properties": {"path": {"type": "string"},
                                              "content": {"type": "string"},
                                              "delete": {"type": "boolean"}}}},
                            "description": {"type": "string"}},
                           ["files", "description"]),
    "confirm_pending": _schema("confirm_pending", "Apply the previously staged large self-update after the user said yes.", {}, []),
    "cancel_pending": _schema("cancel_pending", "Discard the staged large self-update after the user said no.", {}, []),
    "revert_change": _schema("revert_change", "Roll back the last applied self-update to the previous good state.", {}, []),
    "restart": _schema("restart", "Cleanly restart Cortana so code changes take effect. Use when the user says to restart, or after applying a self-update.", {}, []),
    "desktop": _schema("desktop",
                       "Operate this workstation's desktop. ONE tool, many actions - pick with "
                       "'action'. list_windows; focus_window / close_window (target = part of a "
                       "window title, or an id from list_windows - an ambiguous title is refused, "
                       "not guessed); launch (target = a command name or a .desktop id); open "
                       "(target = a file path or URL); clipboard_get; clipboard_set (text); "
                       "volume (no level reads it back; level is 0-100 or a relative step like "
                       "+10; or mute on|off|toggle - this is the ROOM volume, Spotify's own level "
                       "while you talk is ducking and looks after itself); notify (title + text, "
                       "a desktop toast - only when the user asked to SEE something); "
                       "lock_screen; type_text (keystrokes into whatever window has focus, off by "
                       "default). Several actions need X11 helpers that may not be installed - "
                       "you get a plain sentence naming the missing package. Relay it and move "
                       "on; do not try to work around it with shell.",
                       {"action": {"type": "string"}, "target": {"type": "string"},
                        "text": {"type": "string"}, "title": {"type": "string"},
                        "level": {"type": "string"}, "mute": {"type": "string"},
                        "urgency": {"type": "string"}},
                       ["action"]),
    "media": _schema("media",
                     "Control what is playing: music on Spotify, or whatever else is making "
                     "noise on this machine. action is one of play, pause, next, previous, "
                     "status, play_query, volume. Use play_query with 'query' to start something "
                     "specific ('play Nightcall' -> action='play_query', query='Nightcall'); "
                     "plain 'play' only resumes what was already on. 'pause' pauses whatever is "
                     "actually playing, not just Spotify. 'volume' takes 'percent' 0-100, or "
                     "query 'up', 'down', 'mute', 'unmute'. Returns one short spoken sentence.",
                     {"action": {"type": "string"}, "query": {"type": "string"},
                      "percent": {"type": "integer"}}, ["action"]),
    "note": _schema("note",
                    "Save something the user dictates verbatim: a note, a journal entry, a "
                    "decision, anything whose exact wording is worth keeping. Use this for "
                    "anything with substance - `remember` is only for one-line standing "
                    "preferences that belong in every prompt. Everything saved here is "
                    "searchable later with `recall`. kind is note|journal|idea|log.",
                    {"text": {"type": "string"}, "title": {"type": "string"},
                     "tags": {"type": "string"}, "kind": {"type": "string"}},
                    ["text"]),
    "recall": _schema("recall",
                      "Search everything written down: saved notes, EVERY past conversation you "
                      "have had with the user, and their local documents. Reach for this before "
                      "saying you don't remember - 'what did I say about X', 'find that thing "
                      "about Y', anything older than the last few turns. Plain words, no search "
                      "operators; the answer comes back as prose ready to read out.",
                      {"query": {"type": "string"}, "limit": {"type": "integer"}},
                      ["query"]),
    "routine": _schema("routine",
                       "Create or edit a standing rule: WHEN something happens, DO something. "
                       "trigger is calendar, health or presence - or leave trigger out and pass "
                       "an rrule (RFC 5545) for a time of day, which becomes an ordinary "
                       "scheduled item pointing back at the routine. cond is the predicate: "
                       "presence takes from/to states, health takes metric/op/value against the "
                       "system sentinel, calendar takes when=starts_in with minutes. Routines "
                       "are EDGE-triggered: they fire once when the condition becomes true, not "
                       "repeatedly while it holds, and min_gap seconds must pass before the same "
                       "one speaks again. action defaults to say - put the words in 'text', "
                       "which may contain {event}, {minutes}, {from} or {to}; use 'brief' for "
                       "the morning briefing, 'delegate' with agent+task for background work, or "
                       "'turn' with a prompt when it needs fresh thinking AT THE TIME. Re-using "
                       "a name EDITS that routine. Say the resolved rule back to the user so a "
                       "misread is caught out loud.",
                       {"name": {"type": "string"}, "trigger": {"type": "string"},
                        "cond": {"type": "object"}, "action": {"type": "string"},
                        "text": {"type": "string"}, "prompt": {"type": "string"},
                        "agent": {"type": "string"}, "task": {"type": "string"},
                        "rrule": {"type": "string"}, "when": {"type": "string"},
                        "urgency": {"type": "string"}, "min_gap": {"type": "integer"}},
                       ["name"]),
    "routine_set": _schema("routine_set",
                           "List, enable, disable or delete routines. Call it with NO arguments "
                           "to see what exists before changing anything - the names it returns "
                           "are what you pass back in. enabled=false silences one without losing "
                           "it; delete=true removes it along with any time-of-day trigger it "
                           "armed.",
                           {"name": {"type": "string"}, "enabled": {"type": "boolean"},
                            "delete": {"type": "boolean"}}, []),
    "system_check": _schema("system_check",
                            "How the machine and her accounts are actually doing right now: "
                            "memory, disk, temperature, services, network, this repo, spend, "
                            "Google auth and the phone app build. Use it when asked how things "
                            "are or whether anything is wrong, and BEFORE blaming a tool for "
                            "failing - an expired Google token or a full disk explains most of "
                            "it. Every check is cached at its own cadence, so calling this is "
                            "cheap. full=true returns every row; the default returns only what "
                            "is wrong.",
                            {"full": {"type": "boolean"}}, []),
    "comms_read": _schema("comms_read",
                          "Recent text messages and phone notifications mirrored from the "
                          "user's phone. kind is sms|notifications|all. Use it when asked who "
                          "texted, what came in, or whether anything needs answering. It reads a "
                          "local copy, so it works even when the phone is unreachable.",
                          {"kind": {"type": "string"}, "limit": {"type": "integer"}}, []),
    "sms_send": _schema("sms_send",
                        "Send a text message from the user's phone. TWO CALLS, ALWAYS. Call it "
                        "first WITHOUT confirm: nothing is sent and it returns the message read "
                        "back to you. Say that line to the user, and only call it again with "
                        "confirm=true after they explicitly say yes. The recipient and body must "
                        "be word-for-word identical between the two calls or the send is "
                        "refused. Same standing rule as Gmail - never send anything the user has "
                        "not just agreed to. If it comes back saying the phone did not answer, "
                        "do NOT call it again: the message may have gone out and only the "
                        "confirmation was lost. Tell the user that instead.",
                        {"to": {"type": "string"}, "body": {"type": "string"},
                         "confirm": {"type": "boolean"}}, ["to", "body"]),
    "wake_correct": _schema("wake_correct",
                            "Correct your last open-mode judgement about whether an overheard "
                            "utterance was meant for you. Use it when the user says you answered "
                            "something that was not for you (addressed false), or that you "
                            "ignored them when they were talking to you (addressed true). This "
                            "teaches the classifier this room; do not use it for anything else.",
                            {"addressed": {"type": "boolean"}}, ["addressed"]),
    "shutdown": _schema("shutdown", "Cleanly shut Cortana down and stay off (the launcher will NOT relaunch). Use only when the user clearly asks to shut down, power off, or go offline.", {}, []),
}

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

_SPOKEN = ("Replies are spoken aloud via TTS: plain prose, short, no markdown, "
           "no bullet lists, no URLs read out.")

AGENTS = {
    "research": {
        "model": MODEL_LEAD,
        "tools": [],
        "server_tools": [WEB_SEARCH],
        "system": f"Web research specialist. Search, synthesize, answer concisely with the key facts. {_SPOKEN}",
    },
    "email": {
        "model": MODEL_FAST,
        "tools": ["gmail_search", "gmail_read", "gmail_draft"],
        "system": f"Email specialist. Search/read Gmail, summarize, create drafts. NEVER send - drafts only. {_SPOKEN}",
    },
    "trading": {
        "model": MODEL_LEAD,
        "tools": ["quote", "history", "screenshot"],
        "system": ("Futures/markets analyst. Use quote/history for data; use screenshot when the user is "
                   "looking at a chart (e.g. TradingView). Give a clear BUY, SELL, or HOLD lean with the level "
                   "that invalidates it and one-line reasoning. Data may be ~15min delayed - note the timestamp. "
                   f"NEVER attempt to execute a trade. {_SPOKEN}"),
    },
    "video": {
        "model": MODEL_LEAD,
        "tools": ["video", "list_files", "shell"],
        "system": ("Video editor. Use ffprobe first to inspect inputs, then ffmpeg to cut/merge/overlay/"
                   f"convert. Files live in the workspace. Report the output filename when done. {_SPOKEN}"),
    },
    "dev": {
        "model": MODEL_HEAVY,
        "tools": ["shell", "read_file", "write_file", "list_files", "self_update"],
        "system": ("Software engineer for Cortana herself and future UI/app/automation projects. "
                   "To change Cortana's own code use self_update with the FULL new file content - "
                   "never hand-edit via shell. Write, run, and debug code in the workspace "
                   f"autonomously. Summarize results briefly. {_SPOKEN}"),
        # Real engineering work legitimately needs more than 15 tool calls.
        # Safe to raise here specifically: dev almost always runs in the
        # background (own worker thread), so a longer budget costs wall-clock
        # time on that thread, not responsiveness for the user.
        "max_iters": 30,
    },
}

# The lead's surface is large now, and that is a deliberate trade: every one of
# these is a distinct VERB the user says out loud, and handing any of them to a
# subagent would put a round trip between "pause the music" and the music
# pausing. The cost is a bigger cached prefix, which is billed at 0.1x on read -
# not a bigger bill. What keeps it workable is that each new capability is ONE
# coarse tool with an `action`, rather than one tool per thing it can do.
LEAD_TOOL_NAMES = ["delegate", "task_status", "cancel_task", "continue_work",
                   "remind", "schedule_list", "schedule_set",
                   "routine", "routine_set",
                   "desktop", "media", "note", "recall",
                   "system_check", "comms_read", "sms_send", "wake_correct",
                   "remember", "shell",
                   "read_file", "write_file", "list_files", "screenshot",
                   "self_update", "confirm_pending", "cancel_pending",
                   "revert_change", "restart", "shutdown"]
LEAD_TOOLS = [TOOL_DEFS[n] for n in LEAD_TOOL_NAMES]


def lead_system():
    """System blocks for the lead, ordered stable-first.

    The cache breakpoint sits after the static half, so tools + CORTANA.md +
    operating notes are re-used across turns instead of re-billed. Saved memory
    is deliberately AFTER it: recall_all() changes the moment `remember` is
    called, and a volatile byte inside a cached prefix invalidates all of it."""
    md = ""
    try:
        from config import CORTANA_MD
        md = CORTANA_MD.read_text()
    except Exception:
        pass
    stable = (f"{md}\n\n"
              f"## Operating notes\nWorkspace: {WORKSPACE}\n"
              "You are the chief of staff, not the workforce. The user talks to YOU; "
              "specialists do the work. Your job on every request: acknowledge, route, "
              "get back to listening.\n"
              "- Routing: anything multi-step, slow, or specialist-shaped goes to "
              "delegate with background=true - coding/app changes -> dev, web lookups/"
              "summaries -> research, inbox work -> email, market analysis -> trading, "
              "media -> video. When you hand off, SAY SO in a few words and end your "
              "turn ('On it - dev is making that change. I'll tell you when it's done.'). "
              "Do not wait for the result.\n"
              "- Do directly ONLY what is faster to do than to explain: a screenshot, "
              "one shell one-liner, reading one file, answering from knowledge or "
              "conversation memory. If you're not sure a task fits in 2-3 tool calls, "
              "it doesn't - delegate it instead of chaining shell/read_file/write_file "
              "calls yourself. You have a hard step limit; running it out mid-task "
              "wastes the user's time far more than a quick handoff would.\n"
              "- delegate background=false is reserved for a single quick lookup whose "
              "answer the CURRENT sentence needs (one quote, one search). Coding/build "
              "work is NEVER background=false, even if it looks small - always dev, "
              "always background=true.\n"
              "- When you call a tool, any text you write alongside it is spoken aloud "
              "first - use it for a short acknowledgment or nothing at all.\n"
              "- Completed background tasks are announced automatically and appear in "
              "the conversation log as [background task N ...]. Use task_status when "
              "asked how work is going; cancel_task to stop one.\n"
              "- Time-shaped requests go to `remind`, never to a background task "
              "that sleeps. Read the clock in '## Now', convert relative times "
              "yourself, and say the resolved time back ('Seven tomorrow morning "
              "- set.') so a misread is caught out loud. Anything that needs "
              "fresh thinking when it fires is action='turn' with a prompt, not "
              "a canned line written now.\n"
              "- Before using the restart tool, check task_status; if tasks are "
              "running, say what would be lost and get a confirmation first.\n"
              "Act first, report after - do not ask permission for workspace actions. "
              f"{_SPOKEN}")
    return _cached([{"type": "text", "text": stable}]) + [
        {"type": "text", "text": "## Saved memory\n" + memory.recall_all()},
        {"type": "text", "text": _now_block()}]


def _now_block():
    """Current local time, so 'in 20 minutes' can become a real timestamp.

    MUST stay AFTER the _cached() breakpoint, and it is placed last precisely
    because it is the most volatile string in the whole prompt - it changes
    every second. One move above the breakpoint would turn every single turn
    into a full cache WRITE at CACHE_WRITE_MULT (2x input price), which is the
    exact failure the block ordering in lead_system() exists to prevent.
    Ordering AMONG post-breakpoint blocks is free, so last costs nothing.
    """
    now = datetime.datetime.now().astimezone()
    return ("## Now\n"
            f"{now:%A %Y-%m-%d %H:%M:%S} (UTC{now:%z})\n"
            f"Pass timestamps to scheduling tools in this form: "
            f"{now.replace(microsecond=0).isoformat(timespec='seconds')}\n"
            f"Timezone: {SCHED_TZ or 'system local'}")


def dispatch(name, args, run_agent=None, cancel=None, resume=None):
    """Execute a tool. run_agent injected by orchestrator for 'delegate'.
    cancel: the caller's (lead's) cancel token - forwarded only to a SYNCHRONOUS
    delegate, so interrupting the current voice turn also stops it. Background
    delegates get their own independent cancel token from tasks.start and must
    keep running after this turn ends - that one is untouched here.
    resume: injected by orchestrator for 'continue_work'."""
    if name == "delegate":
        agent = args.get("agent", "")
        if agent not in AGENTS:
            return f"Unknown agent '{agent}'. Options: {', '.join(AGENTS)}"
        task_text = args.get("task", "")
        if bool(args.get("background", True)):
            import tasks
            return tasks.start(agent, task_text,
                               runner=lambda a, t, c: run_agent(a, t, cancel=c))
        return run_agent(agent, task_text, cancel=cancel)
    if name == "continue_work":
        if resume is None:
            return "continue_work is unavailable in this context."
        return resume(args.get("which") or "lead", args.get("nudge", ""), cancel)
    if name == "task_status":
        import tasks
        return tasks.status_summary(args.get("id"))
    if name == "cancel_task":
        import tasks
        return tasks.cancel(int(args["id"]))
    if name == "shell":
        return F.run_shell(args["command"])
    if name == "read_file":
        return F.read_file(args["path"])
    if name == "write_file":
        return F.write_file(args["path"], args["content"])
    if name == "list_files":
        return F.list_files(args.get("path", "."))
    if name == "screenshot":
        from tools import vision as V
        return V.screenshot()
    if name == "remember":
        return memory.remember(args["key"], args["value"])
    if name in ("remind", "schedule_list", "schedule_set"):
        import schedule       # lazy: dateutil is only needed for recurrence
        if name == "remind":
            return schedule.create(args)
        if name == "schedule_list":
            return schedule.summary(bool(args.get("include_done")))
        return schedule.set_state(int(args["id"]),
                                  ack=bool(args.get("ack")),
                                  cancel=bool(args.get("cancel")))
    if name in ("self_update", "confirm_pending", "cancel_pending", "revert_change"):
        import selfedit
        import tasks
        # Code writes are serialized with background dev tasks - concurrent
        # writers would corrupt selfedit's git checkpoint/rollback chain.
        with tasks.code_lock() as got:
            if not got:
                return ("A dev task is editing code right now - wait for it to "
                        "finish (task_status) or cancel it first.")
            if name == "self_update":
                _, msg = selfedit.apply_edit(args["files"], args.get("description", "update"))
                return msg
            if name == "confirm_pending":
                return selfedit.confirm_pending()[1]
            if name == "cancel_pending":
                return selfedit.cancel_pending()[1]
            return selfedit.revert_last()[1]
    if name == "desktop":
        from tools import desktop as D   # lazy: only shells out to X11/pactl binaries
        return D.desktop(args)
    if name == "media":
        from tools import media as M     # lazy: requests only loads when music is asked for
        return M.media(args["action"], args.get("query", ""), args.get("percent"))
    if name in ("note", "recall"):
        from tools import notes as N     # lazy: opens/creates the index on first use
        if name == "note":
            return N.add(args["text"], title=args.get("title", ""),
                         tags=args.get("tags", ""), kind=args.get("kind", "note"))
        return N.recall(args["query"], limit=int(args.get("limit") or 3))
    if name in ("routine", "routine_set"):
        import routines                  # lazy: pulls in calendar_state, notify and presence
        if name == "routine":
            return routines.create(args)
        return routines.set_state(args.get("name"), enabled=args.get("enabled"),
                                  delete=bool(args.get("delete")))
    if name == "system_check":
        import sentinel                  # lazy: reads /proc, may shell out to systemctl
        sentinel.poll()                  # cheap - each check is cached at its own cadence
        if args.get("full"):
            return "\n".join("%s [%s]: %s" % (c["label"], c["state"], c["detail"])
                              for c in sentinel.rows()) or "No checks have run yet."
        return sentinel.speakable()
    if name in ("comms_read", "sms_send"):
        # Deliberately the loopback client, not bridge.comms: the store and the
        # phone socket live in the BRIDGE process, this dispatch runs in
        # whichever process holds the orchestrator, and only one of them can be
        # right. bridge.client is stdlib-only, so importing it never drags
        # aiohttp into the cortana process.
        from bridge import client
        if name == "comms_read":
            return client.comms_summary(args.get("kind", "all"),
                                        int(args.get("limit") or 8))
        return client.sms_send(args.get("to", ""), args.get("body", ""),
                               confirm=bool(args.get("confirm")))
    if name == "wake_correct":
        from voice import wake as W      # lazy, per this module's convention
        return W.correct_last(bool(args["addressed"]))
    if name == "restart":
        raise RestartRequested()
    if name == "shutdown":
        raise ShutdownRequested()
    if name == "quote":
        from tools import trading as T
        return T.get_quote(args["symbol"])
    if name == "history":
        from tools import trading as T
        return T.get_history(args["symbol"], args.get("period", "5d"),
                             args.get("interval", "15m"))
    if name == "video":
        from tools import video as VID
        return VID.ffmpeg_edit(args["args"])
    if name in ("gmail_search", "gmail_read", "gmail_draft"):
        from tools import gmail_tool as G  # lazy: avoids OAuth at import
        if name == "gmail_search":
            return G.gmail_search(args["query"], args.get("max_results", 10))
        if name == "gmail_read":
            return G.gmail_read(args["id"])
        return G.gmail_draft(args["to"], args["subject"], args["body"])
    return f"Unknown tool {name}"
