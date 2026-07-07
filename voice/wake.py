"""Wake-word detection + 'is the user talking to me?' classifier.

Current wake detection = transcribe-then-match (costs one Whisper call per
utterance). FUTURE FREE PATH: openWakeWord running locally to gate the mic
before any API call - drop-in replacement for the gating in main.py.

OPEN mode classifier logs every decision to memory.address_log - that table is
the future training set for learning your speech patterns.
"""
import re

import anthropic

from config import ANTHROPIC_API_KEY, MODEL_FAST, WAKE_REGEX
import memory

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def wake_match(text):
    """Returns command with wake word stripped, or None if no wake word."""
    m = re.match(WAKE_REGEX, text, re.I)
    if not m:
        return None
    rest = (m.group(3) or "").strip()
    return rest or "Yes?"


def addressed(text, recent_context=""):
    """Haiku-based classifier: was this utterance directed at Cortana?"""
    try:
        r = _client.messages.create(
            model=MODEL_FAST, max_tokens=3,
            system=("You decide if an overheard utterance is directed at a voice "
                    "assistant named Cortana, versus other people, phone calls, TV, "
                    "or background speech. Reply with exactly YES or NO."),
            messages=[{"role": "user",
                       "content": f"Recent conversation:\n{recent_context}\n\nUtterance: {text}"}])
        yes = "YES" in r.content[0].text.upper()
    except Exception:
        yes = False
    memory.log_address_decision(text, "yes" if yes else "no")
    return yes
