"""Cortana's real voice, streamed to the phone.

Mirrors the chain in voice/tts.py - ElevenLabs streaming, then non-streaming
ElevenLabs, then OpenAI - but writes chunks into a callback instead of a local
audio player, so the phone hears audio as it is generated rather than after.
"""
import requests

from bridge.settings import TTS_CAP, log
import config as cortana_config

_ELEVEN = "https://api.elevenlabs.io/v1/text-to-speech"


def stream_blocking(text, put):
    """Generate speech for `text`, feeding chunks to put().

    Sentinels: put(None) = finished, put(False) = every backend failed, and the
    phone should fall back to its own text-to-speech. Runs on a worker thread.
    """
    text = text[:TTS_CAP]
    if _stream_eleven(text, put):
        return
    if _whole_eleven(text, put):
        return
    if _whole_openai(text, put):
        return
    put(False)


def _stream_eleven(text, put):
    try:
        r = requests.post(
            f"{_ELEVEN}/{cortana_config.ELEVEN_VOICE_ID}/stream",
            headers={"xi-api-key": cortana_config.ELEVENLABS_API_KEY,
                     "Content-Type": "application/json"},
            json={"text": text, "model_id": "eleven_flash_v2_5",
                  "optimize_streaming_latency": 4},
            stream=True, timeout=60)
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=4096):
            if chunk:
                put(chunk)
        put(None)
        return True
    except Exception as e:
        log("TTS stream failed, falling back", e)
        return False


def _whole_eleven(text, put):
    try:
        r = requests.post(
            f"{_ELEVEN}/{cortana_config.ELEVEN_VOICE_ID}",
            headers={"xi-api-key": cortana_config.ELEVENLABS_API_KEY,
                     "Content-Type": "application/json"},
            json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=60)
        r.raise_for_status()
        put(r.content)
        put(None)
        return True
    except Exception as e:
        log("TTS ElevenLabs fallback failed", e)
        return False


def _whole_openai(text, put):
    try:
        from openai import OpenAI
        r = OpenAI(api_key=cortana_config.OPENAI_API_KEY).audio.speech.create(
            model="tts-1", voice="nova", input=text)
        put(r.content)
        put(None)
        return True
    except Exception as e:
        log("TTS OpenAI fallback failed", e)
        return False
