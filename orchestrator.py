"""Lead agent loop + subagent execution + cost tracking + budget cap."""
import anthropic

from config import (ANTHROPIC_API_KEY, BUDGET_MONTHLY_USD, MAX_TOKENS, MODEL_LEAD,
                    PRICES)
import agents
import memory

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _track(model, usage):
    pin, pout = PRICES.get(model, (3.0, 15.0))
    cost = usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout
    memory.add_usage(model, usage.input_tokens, usage.output_tokens, cost)


def _loop(model, system, messages, tools, max_iters=15):
    for _ in range(max_iters):
        resp = client.messages.create(model=model, max_tokens=MAX_TOKENS,
                                      system=system, messages=messages, tools=tools)
        _track(model, resp.usage)
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                out = agents.dispatch(b.name, b.input, run_agent=run_agent)
            except Exception as e:
                out = f"TOOL ERROR ({b.name}): {e}"
            content = out if isinstance(out, list) else [
                {"type": "text", "text": str(out)[:12000]}]
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": content})
        messages.append({"role": "user", "content": results})
    return "I hit my step limit on that task. Ask me to continue if you want more."


def run_agent(name, task):
    a = agents.AGENTS[name]
    tools = [agents.TOOL_DEFS[t] for t in a["tools"]] + a.get("server_tools", [])
    print(f"  [delegating -> {name}]")
    return _loop(a["model"], a["system"],
                 [{"role": "user", "content": task}], tools)


def handle(user_text):
    spent = memory.month_spend()
    if spent >= BUDGET_MONTHLY_USD:
        return (f"Monthly budget cap of {BUDGET_MONTHLY_USD:.0f} dollars reached "
                f"({spent:.2f} spent). Raise BUDGET_MONTHLY_USD in the env file to continue.")
    memory.log_turn("user", user_text)
    msgs = memory.recent_messages(12)
    if not msgs:
        msgs = [{"role": "user", "content": user_text}]
    try:
        reply = _loop(MODEL_LEAD, agents.lead_system(), msgs, agents.LEAD_TOOLS)
    except anthropic.APIStatusError as e:
        reply = f"API error: {e.status_code}. Check model names and billing in the Anthropic console."
    except Exception as e:
        reply = f"Something broke: {e}"
    memory.log_turn("assistant", reply)
    return reply
