"""Wake-word matching + the "is the user talking to me?" classifier.

Two entry points, with very different costs:

  * wake_match() - a regex over an already-transcribed utterance. Free, but
    the transcription that fed it was not. wakeword.py is the offline gate
    that removes that cost; this stays as the fallback and as the
    "ok cortana ..." prefix stripper.
  * addressed() - the OPEN-mode classifier, billed on EVERY utterance in the
    room. Which is why the prompt below is kept deliberately small and the
    few-shot set is bounded rather than "all of it".

memory.address_log has been recording a verdict per utterance since open mode
shipped, and nothing has ever read it. It is now the few-shot source: the
user's own history, with spoken corrections beating the model's own guesses.
That is what makes this classifier get better at THIS room over time instead
of re-deciding from zero forever.
"""
import re
import time

from config import ANTHROPIC_API_KEY, MODEL_FAST, WAKE_REGEX
import memory

_SYSTEM = ("You decide if an overheard utterance is directed at a voice "
           "assistant named Cortana, versus other people, phone calls, TV, "
           "or background speech. Reply with exactly YES or NO.")

# decision value in address_log -> label the classifier should have given.
# 'user-' rows are spoken corrections: the user told us we got it wrong, so
# they are ground truth rather than the model marking its own homework.
# 'error' rows exist so an API failure is auditable WITHOUT being taught as a
# NO - which is what the old code did, harmlessly while nothing read the
# table and not harmlessly now that these rows are the examples.
_LABELS = {"yes": "YES", "no": "NO", "user-yes": "YES", "user-no": "NO"}

_SCAN = 300         # rows read from the tail of address_log per rebuild
_EX_CHARS = 90      # per-example truncation; a long utterance teaches nothing extra
_CACHE_TTL = 120.0  # seconds; the examples change on human timescales, not per turn

_client = None
_cache = {"ts": 0.0, "text": ""}


def _get_client():
    """Lazy, per agents.py convention. Also what keeps this module importable
    on a box with no anthropic SDK, which is where its tests run."""
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def wake_match(text):
    """Returns command with wake word stripped, or None if no wake word."""
    m = re.match(WAKE_REGEX, text, re.I)
    if not m:
        return None
    rest = (m.group(3) or "").strip()
    return rest or "Yes?"


def address_examples(limit=None):
    """Bounded few-shot set from address_log as [(utterance, "YES"|"NO")].

    Three properties this owes the caller, all of them cost or accuracy:

      * BOUNDED. This text is billed on every open-mode utterance, so it is
        capped by count and by per-example length, not by how much history
        exists. Ten thousand rows and ten rows produce the same prompt size.
      * BALANCED. An evening of television puts a run of twenty NOs at the
        head of the log; handing those over unbalanced teaches the classifier
        to answer NO to everything, which is the exact failure mode that makes
        open mode feel broken.
      * NEWEST FIRST, deduped by text. A correction row is written after the
        verdict it corrects and carries the same utterance, so the dedupe key
        makes the correction win automatically - no precedence logic needed.

    Returns [] on an empty or missing table. Never raises.
    """
    limit = _limit() if limit is None else int(limit)
    if limit <= 0:
        return []
    rows = _read("SELECT text, decision FROM address_log ORDER BY ts DESC"
                 " LIMIT ?", (_SCAN,))
    seen, yes, no = set(), [], []
    for text, decision in rows:
        label = _LABELS.get((decision or "").strip().lower())
        if label is None:
            continue
        t = " ".join((text or "").split())[:_EX_CHARS]
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        (yes if label == "YES" else no).append((t, label))
    half = max(1, limit // 2)
    out = yes[:half] + no[:half]
    # Backfill from whichever side is deeper, so a limit of eight still ships
    # eight examples once the log is lopsided - which it always is at first.
    for pool in (yes, no):
        for ex in pool[half:]:
            if len(out) >= limit:
                break
            out.append(ex)
    return out[:limit]


def _read(sql, args=()):
    """Read-only query against state.db. [] on any failure, connection always
    closed - the try/finally matters because address_log is read on the latency
    path of a spoken turn, and a leaked handle there is a leaked handle per
    utterance."""
    con = None
    try:
        con = memory.connect()
        return con.execute(sql, args).fetchall()
    except Exception:
        return []
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def _write_decision(text, decision):
    """Record a verdict without letting the recording lose the turn.

    address_log is training data, not the answer. A locked or full database
    must not turn "the classifier said no" into an exception climbing out of
    addressed() and past the whole utterance.
    """
    try:
        memory.log_address_decision(text, decision)
    except Exception as e:
        print("[wake] address_log write failed:", e)


def _limit():
    # Read late and defensively: the config key lands in a separate change to
    # a file this module does not own, and its absence must mean "no examples"
    # rather than an ImportError inside the voice loop.
    import config
    try:
        return int(getattr(config, "ADDRESS_EXAMPLES", 8))
    except Exception:
        return 8


def _examples_block(now=None):
    """The few-shot text, cached for _CACHE_TTL.

    Cached because addressed() is on the latency path of a spoken turn and the
    log only changes when a human says something. Not cached forever, because
    a correction should start mattering within a couple of minutes without a
    restart - correct_last() clears it outright for the impatient case.
    """
    now = time.time() if now is None else now
    # Keyed on ts, not on text. Keying on text meant an empty result was never
    # cached, so a fresh install - no history at all, which is every install on
    # day one - opened state.db and sorted three hundred rows on EVERY
    # open-mode utterance, which is the exact latency this cache exists to
    # avoid. Invalidation sets ts to 0.
    if _cache["ts"] and now - _cache["ts"] < _CACHE_TTL:
        return _cache["text"]
    ex = address_examples()
    if ex:
        lines = "\n".join(f"- {t} -> {label}" for t, label in ex)
        text = ("Past verdicts for this user and this room. YES means they "
                "were talking to Cortana.\n" + lines)
    else:
        text = ""
    _cache.update(ts=now, text=text)
    return text


def addressed(text, recent_context=""):
    """Haiku-based classifier: was this utterance directed at Cortana?"""
    system = _SYSTEM
    block = _examples_block()
    if block:
        system = system + "\n\n" + block
    try:
        r = _get_client().messages.create(
            model=MODEL_FAST, max_tokens=3,
            system=system,
            messages=[{"role": "user",
                       "content": f"Recent conversation:\n{recent_context}\n\nUtterance: {text}"}])
        yes = "YES" in r.content[0].text.upper()
    except Exception as e:
        # Logged as 'error', not as 'no'. A network blip is not evidence about
        # who the user was talking to, and address_log is training data now.
        print("addressed error:", e)
        _write_decision(text, "error")
        return False
    _write_decision(text, "yes" if yes else "no")
    return yes


def last_decision():
    """(text, decision) of the most recent real verdict, or None.

    'error' rows are skipped: correcting one would teach the classifier about
    an utterance it never actually judged.
    """
    rows = _read("SELECT text, decision FROM address_log WHERE decision IN"
                 " ('yes','no','user-yes','user-no') ORDER BY ts DESC LIMIT 1")
    return (rows[0][0], rows[0][1]) if rows else None


def correct_last(was_addressed):
    """Record a spoken correction of the latest verdict. Returns one speakable
    sentence.

    Writes a NEW row rather than updating the old one: address_log is an
    append-only record of what was decided when, and rewriting history would
    lose the fact that the classifier got it wrong - which is the single most
    useful thing in the table.
    """
    last = last_decision()
    if not last:
        return "I have no recent decision to correct."
    text, prior = last
    label = "user-yes" if was_addressed else "user-no"
    if prior == label:
        return "That is already how I have it."
    _write_decision(text, label)
    _cache.update(ts=0.0, text="")   # take effect on the next utterance, not in two minutes
    return ("Noted. I should have answered that one." if was_addressed
            else "Noted. I will let that kind of remark pass.")
