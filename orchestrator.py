"""Lead agent loop + subagent execution + cost tracking + budget cap +
self-critique iteration + HUD state broadcasting."""
import anthropic

from config import (ANTHROPIC_API_KEY, BUDGET_MONTHLY_USD, EFFORT_HEAVY, EFFORT_LEAD,
                    MAX_TOKENS, MODEL_HEAVY, MODEL_LEAD, MODEL_FAST, PRICES,
                    reasoning_kwargs)
import agents
from agents import RestartRequested, ShutdownRequested
import memory
import hud_state
from voice import speech

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_restart_flag = {"do": False}
_shutdown_flag = {"do": False}
# Reset each handle(). Only the LEAD loop mutates these (subagent threads run
# _loop concurrently and must not affect the critique gate):
# tool_calls counts lead tool use; sync_work is True only if something beyond
# fire-and-forget delegation ran - background handoffs skip the critique pass.
_turn = {"tool_calls": 0, "sync_work": False}

_BG_ONLY_TOOLS = {"task_status", "cancel_task", "remember"}

# Transcripts of loops that ran out of steps, keyed by who was running ("lead",
# or the subagent name). Hitting the ceiling used to just discard `messages` -
# the entire tool-call transcript - so a follow-up "keep going" restarted from
# twelve lines of plain text out of SQLite and redid work that was already
# done. Keeping it lets continue_work resume mid-task with full context.
# Bounded because these hold whole conversations.
_stalled = {}
STALL_MAX = 6

# Named so callers can detect these outcomes instead of matching prose.
CANCELLED_MSG = "Cancelled."
STEP_LIMIT_MSG = ("I hit my step limit on that task. The work so far is parked, "
                  "not lost - say continue and I pick up where I stopped.")


def restart_requested():
    return _restart_flag["do"]


def shutdown_requested():
    return _shutdown_flag["do"]


def _track(model, usage):
    pin, pout = PRICES.get(model, (3.0, 15.0))
    cost = usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout
    memory.add_usage(model, usage.input_tokens, usage.output_tokens, cost)


def _effort_for(model):
    """Heavy tier thinks harder; the lead stays snappy because its turn is spoken."""
    return EFFORT_HEAVY if model == MODEL_HEAVY else EFFORT_LEAD


def _loop(model, system, messages, tools, max_iters=15, agent_label="",
          is_lead=False, cancel=None, effort=None, stall_key=None):
    # Adaptive thinking, resolved once per loop - {} for models that reject it.
    reasoning = reasoning_kwargs(model, effort or _effort_for(model))
    for _ in range(max_iters):
        if cancel is not None and cancel.is_set():
            return CANCELLED_MSG
        resp = client.messages.create(model=model, max_tokens=MAX_TOKENS,
                                      system=system, messages=messages, tools=tools,
                                      **reasoning)
        _track(model, resp.usage)
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": resp.content})
        # Preamble text riding along with tool calls: the lead SPEAKS it before
        # the work runs ("On it - handing this to dev."), so the user gets an
        # instant acknowledgment instead of silence. Subagents feed the HUD only.
        for b in resp.content:
            if b.type == "thinking":
                # Summarized reasoning drives the HUD's live feed. Never spoken -
                # it is the model's scratch work, not an answer to the user.
                summary = (getattr(b, "thinking", "") or "").strip()
                if summary:
                    hud_state.think(summary.splitlines()[-1][:140])
            elif b.type == "text" and b.text.strip():
                line = b.text.strip()
                hud_state.think(line[:140])
                if is_lead:
                    speech.say(line[:400])
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            if is_lead:
                _turn["tool_calls"] += 1
                is_bg_delegate = (b.name == "delegate"
                                  and bool(b.input.get("background", True)))
                if not is_bg_delegate and b.name not in _BG_ONLY_TOOLS:
                    _turn["sync_work"] = True
            if b.name == "delegate":
                tgt = b.input.get("agent", "")
                hud_state.set_state("working", agent=tgt)
                hud_state.think(f"delegating to {tgt}: {b.input.get('task','')[:80]}")
            else:
                hud_state.set_state("working", agent=agent_label or b.name)
                hud_state.think(f"{b.name}: {str(b.input)[:80]}")
            try:
                out = agents.dispatch(b.name, b.input, run_agent=run_agent,
                                      cancel=cancel, resume=resume_stalled)
            except RestartRequested:
                _restart_flag["do"] = True
                out = "Restarting now to load changes."
            except ShutdownRequested:
                _shutdown_flag["do"] = True
                out = "Shutting down. Goodbye."
            except Exception as e:
                out = f"TOOL ERROR ({b.name}): {e}"
            content = out if isinstance(out, list) else [
                {"type": "text", "text": str(out)[:12000]}]
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": content})
        messages.append({"role": "user", "content": results})
    # Out of steps, not out of work. Park the transcript so it can be resumed
    # instead of thrown away.
    if stall_key:
        if len(_stalled) >= STALL_MAX and stall_key not in _stalled:
            _stalled.pop(next(iter(_stalled)))
        _stalled[stall_key] = {"model": model, "system": system, "tools": tools,
                               "messages": messages, "max_iters": max_iters,
                               "agent_label": agent_label, "is_lead": is_lead,
                               "effort": effort}
    return STEP_LIMIT_MSG


def stalled_keys():
    """Who currently has parked, resumable work."""
    return sorted(_stalled)


def resume_stalled(key="lead", nudge="", cancel=None):
    """Continue a loop that ran out of steps, with its full tool transcript.

    Popped before running: if this attempt stalls again _loop re-parks it, and
    if it raises we do not leave a stale transcript to be resumed twice.
    """
    st = _stalled.pop(key, None)
    if not st:
        have = ", ".join(stalled_keys()) or "nothing"
        return f"No parked work for '{key}'. Currently parked: {have}."
    msgs = st["messages"]
    msgs.append({"role": "user", "content":
                 (nudge or "Continue from exactly where you stopped. Do not "
                           "redo completed steps; finish the remaining work.")})
    hud_state.set_state("working", agent=st["agent_label"] or "continuing")
    hud_state.think(f"resuming parked work: {key}")
    return _loop(st["model"], st["system"], msgs, st["tools"],
                 max_iters=st["max_iters"], agent_label=st["agent_label"],
                 is_lead=st["is_lead"], cancel=cancel, effort=st["effort"],
                 stall_key=key)


def run_agent(name, task, cancel=None):
    """Run a subagent to completion. cancel: threading.Event checked between
    steps - background tasks pass their own task-scoped event; a synchronous
    (background=false) delegate is passed the LEAD's cancel, so interrupting
    the current voice turn also aborts a sync subagent mid-flight."""
    a = agents.AGENTS[name]
    tools = [agents.TOOL_DEFS[t] for t in a["tools"]] + a.get("server_tools", [])
    print(f"  [delegating -> {name}]")
    hud_state.set_state("working", agent=name)
    return _loop(a["model"], a["system"],
                 [{"role": "user", "content": task}], tools, agent_label=name,
                 max_iters=a.get("max_iters", 15), cancel=cancel,
                 effort=a.get("effort"), stall_key=name)


def _critique(user_text, answer):
    """Cheap gap-check. Returns '' if complete, else a short gap list to fix."""
    try:
        r = client.messages.create(
            model=MODEL_FAST, max_tokens=200,
            system=("You are a strict reviewer. Given a user request and an assistant's "
                    "result, reply DONE if the request is fully and correctly satisfied. "
                    "Otherwise reply with a short bullet list of concrete gaps to fix. "
                    "Be terse. Do not nitpick style."),
            messages=[{"role": "user",
                       "content": f"REQUEST:\n{user_text}\n\nRESULT:\n{answer}"}])
        _track(MODEL_FAST, r.usage)
        t = "".join(b.text for b in r.content if b.type == "text").strip()
        return "" if t.upper().startswith("DONE") else t
    except Exception:
        return ""


def handle(user_text, max_refine=1, cancel=None):
    """cancel: threading.Event the caller can set to preempt this turn (e.g. a
    newer voice command arrived). Checked between every model/tool step in the
    lead loop; a mid-loop cancel returns None so the caller speaks nothing
    stale and moves straight to the newer request."""
    spent = memory.month_spend()
    if spent >= BUDGET_MONTHLY_USD:
        return (f"Monthly budget cap of {BUDGET_MONTHLY_USD:.0f} dollars reached "
                f"({spent:.2f} spent). Raise BUDGET_MONTHLY_USD in the env file to continue.")
    memory.log_turn("user", user_text)
    msgs = memory.recent_messages(12)
    if not msgs:
        msgs = [{"role": "user", "content": user_text}]

    _turn["tool_calls"] = 0
    _turn["sync_work"] = False
    hud_state.set_state("thinking")   # feed was already cleared when we went idle last turn
    hud_state.think("understanding the request")
    try:
        reply = _loop(MODEL_LEAD, agents.lead_system(), msgs, agents.LEAD_TOOLS,
                      is_lead=True, cancel=cancel, effort=EFFORT_LEAD,
                      stall_key="lead")
        if reply == CANCELLED_MSG:
            memory.log_turn("assistant", "(interrupted by a newer request)")
            return None
        # Self-critique / iterate: only when SYNCHRONOUS work ran AND the lead
        # actually produced a real answer. Skip it when: background-only turn
        # (the reply is just an acknowledgment - the real work reports later
        # and re-critiquing here would re-add the latency async exists to
        # remove); or the lead hit its step limit (critiquing a give-up message
        # just launches a second, equally doomed full loop - the exact "hang"
        # this guard exists to prevent).
        if (not _restart_flag["do"] and not _shutdown_flag["do"]
                and _turn["sync_work"] and reply != STEP_LIMIT_MSG and len(reply) > 40):
            for i in range(max_refine):
                if cancel is not None and cancel.is_set():
                    memory.log_turn("assistant", "(interrupted by a newer request)")
                    return None
                gaps = _critique(user_text, reply)
                if not gaps:
                    break
                hud_state.set_state("working", agent=f"refining {i + 1}/{max_refine}")
                hud_state.think(f"self-check found gaps, refining ({i + 1}/{max_refine})")
                msgs.append({"role": "assistant", "content": reply})
                msgs.append({"role": "user",
                             "content": f"Not complete yet. Fix these gaps, then give the full result:\n{gaps}"})
                reply = _loop(MODEL_LEAD, agents.lead_system(), msgs, agents.LEAD_TOOLS,
                              is_lead=True, cancel=cancel, effort=EFFORT_LEAD,
                              stall_key="lead")
                if reply == CANCELLED_MSG:
                    memory.log_turn("assistant", "(interrupted by a newer request)")
                    return None
    except anthropic.APIStatusError as e:
        reply = f"API error: {e.status_code}. Check model names and billing in the Anthropic console."
    except Exception as e:
        reply = f"Something broke: {e}"
    memory.log_turn("assistant", reply)
    return reply
