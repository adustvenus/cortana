"""Speech-to-text. Primary: OpenAI Whisper API (~$0.006/min).

OFFLINE/FREE FALLBACK (if net down or cutting costs):
    pip install faster-whisper
then set USE_LOCAL=True below. ~2-5x slower on this laptop's CPU, still usable.
"""
from openai import OpenAI

from config import OPENAI_API_KEY

USE_LOCAL = False
_client = OpenAI(api_key=OPENAI_API_KEY)
_local = None


def transcribe(wav_path):
    import numpy as np
    import soundfile as sf
    audio, _ = sf.read(wav_path, dtype="int16")  # int16 so the RMS threshold below is on the PCM scale
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    if rms < 200:
        return ""  # too quiet, skip - avoids Whisper hallucination on near-silence
    if USE_LOCAL:
        return _transcribe_local(wav_path)
    try:
        with open(wav_path, "rb") as f:
            r = _client.audio.transcriptions.create(model="whisper-1", file=f, language="en")
        return (r.text or "").strip()
    except Exception as e:
        print("STT error:", e)
        return ""


def _transcribe_local(wav_path):
    global _local
    if _local is None:
        from faster_whisper import WhisperModel
        _local = WhisperModel("base.en", device="cpu", compute_type="int8")
    segs, _ = _local.transcribe(wav_path)
    return " ".join(s.text for s in segs).strip()
