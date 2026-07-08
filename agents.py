"""Tool schemas, subagent definitions, tool dispatcher."""
from config import MODEL_LEAD, MODEL_FAST, MODEL_HEAVY, WORKSPACE
import memory
from tools import files as F
# vision / trading / video / gmail are imported lazily inside dispatch() so a
# missing hardware lib (e.g. mss on a headless box) never kills the whole app.


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
    "remember": _schema("remember", "Save a permanent fact/preference about the user.",
                        {"key": {"type": "string"}, "value": {"type": "string"}},
                        ["key", "value"]),
    "delegate": _schema("delegate", "Hand a task to a specialist subagent. Agents: research (web), email (gmail), trading (markets), video (editing), dev (coding/apps).",
                        {"agent": {"type": "string"}, "task": {"type": "string"}},
                        ["agent", "task"]),
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
    },
}

LEAD_TOOL_NAMES = ["delegate", "remember", "shell", "read_file", "write_file",
                   "list_files", "screenshot", "self_update", "confirm_pending",
                   "cancel_pending", "revert_change", "restart", "shutdown"]
LEAD_TOOLS = [TOOL_DEFS[n] for n in LEAD_TOOL_NAMES]


def lead_system():
    md = ""
    try:
        from config import CORTANA_MD
        md = CORTANA_MD.read_text()
    except Exception:
        pass
    return (f"{md}\n\n## Saved memory\n{memory.recall_all()}\n\n"
            f"## Operating notes\nWorkspace: {WORKSPACE}\n"
            "Quick things (screenshots, small file ops, shell one-liners): do directly. "
            "Bigger or specialist work: delegate (research/email/trading/video/dev). "
            "Act first, report after - do not ask permission for workspace actions. "
            f"{_SPOKEN}")


def dispatch(name, args, run_agent=None):
    """Execute a tool. run_agent injected by orchestrator for 'delegate'."""
    if name == "delegate":
        agent = args.get("agent", "")
        if agent not in AGENTS:
            return f"Unknown agent '{agent}'. Options: {', '.join(AGENTS)}"
        return run_agent(agent, args.get("task", ""))
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
    if name in ("self_update", "confirm_pending", "cancel_pending", "revert_change"):
        import selfedit
        if name == "self_update":
            _, msg = selfedit.apply_edit(args["files"], args.get("description", "update"))
            return msg
        if name == "confirm_pending":
            return selfedit.confirm_pending()[1]
        if name == "cancel_pending":
            return selfedit.cancel_pending()[1]
        return selfedit.revert_last()[1]
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
