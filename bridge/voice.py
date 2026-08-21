"""Cortana's real voice, streamed to the phone.

Mirrors the chain in voice/tts.py - ElevenLabs streaming, then non-streaming
ElevenLabs, then OpenAI - but writes chunks into a callback instead of a local
audio player, so the phone hears audio as it is generated rather than after.
"""
import requests

from bridge.settings import TTS_CAP, log
import config as cortana_config
from voice.speakable import speakable

_ELEVEN = "https://api.elevenlabs.io/v1/text-to-speech"


