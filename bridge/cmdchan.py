"""Workstation -> phone commands, over a socket that has only ever run the
other way.

Everything on the WebSocket today is push: the bridge sends "state" and
"announce", the phone sends one "hello" and nothing else. Asking the phone to
send an SMS inverts that, so this adds a third frame type, "cmd", with a request
id, and takes the answer back over REST.

Why the reply is REST and not the socket: it authenticates through the same
bearer token as every other phone call, it survives the socket flapping between
the question and the answer (which is the normal state of an Android app), and
it keeps the WS read loop out of request/response bookkeeping.

Failure modes, all of which are reachable and none of which may hang:

  * No phone connected -> refused immediately, in a sentence. NOT a timeout;
    the difference matters because it is the one case the user can fix.
  * Old app -> LinkClient's `when (type)` has no else branch, so an unknown
    frame is silently dropped. The request times out and says so. This is
    exactly why the frame type is additive: it degrades to a timeout instead of
    erroring the socket for everything else.
  * Bridge restart mid-request -> the pending map is in-process, so the future
    dies with it. The phone's reply then hits a bridge that never heard of the
    id and gets a 404. It must not retry, and neither do we.
  * Reply lost after the phone already acted -> we report "no confirmation",
    which is honest and ambiguous. There is NO automatic retry on any send:
    two identical text messages are a worse outcome than one unconfirmed one.
  * Flood / leak -> MAX_PENDING refuses new requests rather than growing, and
    every entry is removed in a finally: plus swept by age on the next call.
  * Late or duplicate reply -> resolve() returns False and logs; the future is
    already gone.
"""
import asyncio
import json
import secrets
import time

from bridge import hub
from bridge.settings import log

TIMEOUT = 20.0            # phone-side work is a permission-gated API call
MAX_PENDING = 8           # far above real use; this is the bound, not a budget
STALE = 3.0               # multiple of the timeout after which an entry is junk

_pending = {}             # rid -> {"fut", "cmd", "ts", "timeout"}


def pending_count():
    return len(_pending)


def _sweep(now=None):
    """Drop finished or overdue entries.

    request() removes its own entry in a finally:, so in the normal path this
    finds nothing. It exists because "the handler was killed between the await
    and the finally" is not a hypothesis you get to disprove on a box you cannot
    reach - and an unbounded map on a Restart=always service is how a memory
    leak becomes a mystery.
    """
    now = now or time.time()
    for rid, entry in list(_pending.items()):
        if entry["fut"].done() or now - entry["ts"] > entry["timeout"] * STALE:
            _pending.pop(rid, None)


async def request(cmd, args=None, timeout=TIMEOUT):
    """Ask the phone to do one thing and wait for its answer.

    Returns the phone's reply dict, or {"error": <sentence>}. Never raises, and
    never leaves an entry behind.
    """
    _sweep()
    ws = hub.target_socket()
    if ws is None:
        return {"error": "no phone is connected right now, so I can't ask it to do that"}
    if len(_pending) >= MAX_PENDING:
        return {"error": "too many phone commands are already waiting - try again in a moment"}

    rid = secrets.token_hex(8)          # 128 bits: a reply id is not guessable
    fut = asyncio.get_running_loop().create_future()
    _pending[rid] = {"fut": fut, "cmd": cmd, "ts": time.time(),
                     "timeout": float(timeout)}
    frame = {"type": "cmd", "id": rid, "cmd": cmd,
             "args": args or {}, "timeout": int(timeout)}
    try:
        await hub.send(ws, json.dumps(frame))
        # hub.send swallows per-socket failures and drops the socket instead of
        # raising, so a dead phone would otherwise cost the full timeout for
        # something we already know failed.
        if ws not in hub.sockets():
            return {"error": "the phone's connection dropped before it got that"}
        log(f"cmd {cmd} id={rid} -> phone")
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        log(f"cmd {cmd} id={rid} timed out after {timeout}s")
        return {"error": "the phone didn't answer - it may be asleep, or running "
                         "an app version that doesn't know that command yet"}
    except Exception as e:              # noqa: BLE001 - relayed as a sentence
        log(f"cmd {cmd} id={rid} failed", e)
        return {"error": f"could not reach the phone: {str(e)[:120]}"}
    finally:
        _pending.pop(rid, None)


def resolve(rid, payload):
    """The phone answered. False means the id is unknown - it timed out, it was
    already answered, or the bridge restarted in between. Callers turn that into
    a 404 so the phone stops rather than retrying into a void."""
    entry = _pending.get(str(rid or ""))
    if entry is None or entry["fut"].done():
        log(f"cmd reply for unknown id={rid} - dropped")
        return False
    entry["fut"].set_result(payload)
    return True
