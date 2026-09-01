"""Speech-to-text. Primary: OpenAI Whisper API (~$0.006/min).

OFFLINE/FREE FALLBACK:
    pip install faster-whisper
then set STT_USE_LOCAL=1 in .env.local (per-machine, so one box can run local
without dragging the others with it).

It DEFAULTS OFF and should stay off on the runtime box. base.en at int8 holds
roughly a gigabyte resident out of the two that are free there, so turning it
on trades the binding constraint on this machine - RAM - for an API call that
costs well under a cent a minute. It is worth having for a network outage or a
privacy-sensitive stretch, not as the everyday path. It is also 2-5x slower on
this CPU, which is felt directly in how long Cortana takes to answer.
"""
import config

def _flag(name, default=False):
    """Read an on/off config key that may not exist yet, and may be a STRING.

    getattr, not a from-import: the key lands in a separate change to a file
    this module does not own, and a missing key must leave the fallback OFF
    rather than break the voice loop at import.

    bool() alone was not enough. Every other switch in config.py is built from
    os.getenv, so the value that arrives here is far more likely to be the
    string "0" than the boolean False - and bool("0") is True. That would have
    claimed about half the free RAM on the runtime box on the strength of a
    setting the user wrote to turn the feature OFF. Same "0"/"false" idiom as
    config.DUCK_ENABLED.
    """
    v = getattr(config, name, default)
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


USE_LOCAL = _flag("STT_USE_LOCAL")

_client = None
_local = None


def _get_client():
    """Lazy, per agents.py convention: the openai SDK is not installed in CI,
    and constructing a client at import made this module untestable there."""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def transcribe(wav_path):
    import numpy as np
    import soundfile as sf
    audio, _ = sf.read(wav_path, dtype="int16")  # int16 so the RMS threshold below is on the PCM scale
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    if rms < 200:
        return ""  # too quiet, skip - avoids Whisper hallucination on near-silence
    if USE_LOCAL:
        try:
            return _transcribe_local(wav_path)
        except Exception as e:
            # faster-whisper is a manual, opt-in install, so "flag on, library
            # absent" is the likely first run - and an ImportError raised here
            # goes up into the voice loop as a traceback per utterance. Same
            # shape of failure as the API path below, so same handling.
            print("STT local error:", e)
            return ""
    try:
        with open(wav_path, "rb") as f:
            r = _get_client().audio.transcriptions.create(model="whisper-1", file=f, language="en")
        return (r.text or "").strip()
    except Exception as e:
        print("STT error:", e)
        return ""


def _transcribe_local(wav_path):
    """Raises when faster-whisper is missing or the model cannot load. The
    caller turns that into an empty transcript; nothing here crashes the loop."""
    global _local
    if _local is None:
        from faster_whisper import WhisperModel
        _local = WhisperModel("base.en", device="cpu", compute_type="int8")
    segs, _ = _local.transcribe(wav_path)
    return " ".join(s.text for s in segs).strip()
