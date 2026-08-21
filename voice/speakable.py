"""Turn written text into text a TTS engine reads correctly.

Cortana's replies are generated as prose meant to be READ, so they carry
written shorthand: "27 lbs", '27"', "$40", "72F", "w/". Sent verbatim to
ElevenLabs those come out as "el bee es", "twenty seven" with no unit, or the
symbol name. The model is not going to reliably avoid shorthand no matter what
the system prompt says, so normalise on the way out instead.

Two rules keep this from mangling ordinary prose:

1. Units are only expanded when a NUMBER precedes them. "in" and "ft" are far
   too common as words to touch otherwise - "check in", "left" - and a bare
   substitution would wreck every other sentence.
2. Genuinely ambiguous units are left alone. "5m" is five metres or five
   million, "5g" is grams or a network; guessing wrong is worse than leaving
   the engine to its own devices.
"""
import re

# (pattern following the number, singular, plural). Longest first: "mph" must
# match before "m" would, "mins" before "mi".
_UNITS = (
    (r"mph",            "mile per hour",      "miles per hour"),
    (r"kph",            "kilometre per hour", "kilometres per hour"),
    (r"lbs?\.?",        "pound",              "pounds"),
    (r"kgs?\.?",        "kilogram",           "kilograms"),
    (r"oz\.?",          "ounce",              "ounces"),
    (r"mins?\.?",       "minute",             "minutes"),
    (r"secs?\.?",       "second",             "seconds"),
    (r"hrs?\.?",        "hour",               "hours"),
    (r"yrs?\.?",        "year",               "years"),
    (r"km",             "kilometre",          "kilometres"),
    (r"cm",             "centimetre",         "centimetres"),
    (r"mm",             "millimetre",         "millimetres"),
    (r"ft\.?|'",        "foot",               "feet"),
    (r"in\.?|\"|”|″", "inch",       "inches"),
    (r"mi\.?",          "mile",               "miles"),
    (r"gb",             "gigabyte",           "gigabytes"),
    (r"mb",             "megabyte",           "megabytes"),
    (r"tb",             "terabyte",           "terabytes"),
)

_NUM = r"(\d[\d,]*(?:\.\d+)?)"


def _plural(value, singular, plural):
    try:
        return singular if abs(float(value.replace(",", ""))) == 1 else plural
    except ValueError:
        return plural


def _units(text):
    # Feet-and-inches first: 6'2" is one measurement, not two collisions.
    text = re.sub(_NUM + r"\s*'\s*" + _NUM + r'\s*(?:"|”|″)',
                  lambda m: f"{m.group(1)} {_plural(m.group(1), 'foot', 'feet')} "
                            f"{m.group(2)} {_plural(m.group(2), 'inch', 'inches')}",
                  text)
    for pat, singular, plural in _UNITS:
        text = re.sub(_NUM + r"\s*(?:" + pat + r")(?![A-Za-z])",
                      lambda m, s=singular, p=plural:
                          f"{m.group(1)} {_plural(m.group(1), s, p)}",
                      text, flags=re.IGNORECASE)
    return text


def speakable(text):
    """Normalise `text` for a speech engine. Never raises; returns a string."""
    if not text:
        return ""
    t = str(text)

    # Markdown that should never have been spoken in the first place.
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    # Headings only at line start: a bare # mid-sentence is "#42", a number.
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)
    t = re.sub(r"[`*_]{1,3}(?=\S)", "", t)
    t = re.sub(r"(?<=\S)[`*_]{1,3}", "", t)

    # URLs read terribly; say the host and move on.
    t = re.sub(r"https?://([^\s/]+)\S*", r"\1", t)

    t = _units(t)

    # Currency and degrees need the symbol moved, not just renamed.
    t = re.sub(r"\$" + _NUM,
               lambda m: f"{m.group(1)} {_plural(m.group(1), 'dollar', 'dollars')}", t)
    t = re.sub(_NUM + r"\s*°\s*F\b", r"\1 degrees Fahrenheit", t, flags=re.IGNORECASE)
    t = re.sub(_NUM + r"\s*°\s*C\b", r"\1 degrees Celsius", t, flags=re.IGNORECASE)
    t = re.sub(_NUM + r"\s*°", r"\1 degrees", t)
    t = re.sub(_NUM + r"\s*%", r"\1 percent", t)

    # Symbols that are words when spoken.
    t = re.sub(r"\bw/o\b", "without", t, flags=re.IGNORECASE)
    t = re.sub(r"\bw/", "with ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*&\s*", " and ", t)
    t = re.sub(r"(?<=\w)@(?=\w)", " at ", t)
    t = re.sub(r"#" + _NUM, r"number \1", t)
    t = re.sub(r"~" + _NUM, r"about \1", t)
    t = re.sub(r"\be\.g\.", "for example", t, flags=re.IGNORECASE)
    t = re.sub(r"\bi\.e\.", "that is", t, flags=re.IGNORECASE)
    # Lookahead, not a word boundary: "vs." would leave its full stop behind.
    t = re.sub(r"\bvs\.?(?=\s|$)", "versus", t, flags=re.IGNORECASE)
    t = re.sub(r"\betc\.", "et cetera", t, flags=re.IGNORECASE)

    return re.sub(r"[ \t]{2,}", " ", t).strip()
