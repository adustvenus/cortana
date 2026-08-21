"""Text-to-speech. Chain: ElevenLabs (streamed) -> OpenAI tts-1 -> espeak-ng.
Streaming: audio plays as it arrives, not after full generation - cuts perceived latency.
"""
import subprocess
import tempfile

import requests

from config import ELEVENLABS_API_KEY, ELEVEN_VOICE_ID, OPENAI_API_KEY
from voice.speakable import speakable


def _play(path):
    subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                   check=False)


def _eleven_stream(text):
    """True streaming: pipe mp3 chunks straight into ffplay's stdin as they arrive."""
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream",
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_flash_v2_5",
              "optimize_streaming_latency": 4},
        stream=True, timeout=60)
    r.raise_for_status()
    proc = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", "pipe:0"],
        stdin=subprocess.PIPE)
    for chunk in r.iter_content(chunk_size=2048):
        if chunk:
            proc.stdin.write(chunk)
    proc.stdin.close()
    proc.wait()


def _eleven(text):
    """Non-streaming fallback if streaming request itself fails."""
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}",
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_flash_v2_5"}, timeout=60)
    r.raise_for_status()
    return r.content


def _openai(text):
    from openai import OpenAI
    r = OpenAI(api_key=OPENAI_API_KEY).audio.speech.create(
        model="tts-1", voice="nova", input=text)
    return r.content


def speak(text):
    # Normalise BEFORE the cap and before any backend sees it, so ElevenLabs,
    # OpenAI and the espeak fallback all read the same corrected text.
    text = speakable(text).strip()[:1500]
    if not text:
        return
    try:
        _eleven_stream(text)
        return
    except Exception as e:
        print("TTS streaming failed, falling back:", e)
    for fn in (_eleven, _openai):
        try:
            audio = fn(text)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(audio)
                p = f.name
            _play(p)
            return
        except Exception as e:
            print(f"TTS fallback ({fn.__name__}):", e)
    subprocess.run(["espeak-ng", text], check=False)
