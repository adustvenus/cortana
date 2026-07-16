"""Microphone capture. Two modes:
- record_while(flag): PTT - records while F9 held
- listen_segment(): continuous - waits for speech, records until silence (energy VAD)
"""
import tempfile
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import MIC_DEVICE, MIC_NAME, SAMPLE_RATE, VAD_THRESHOLD


def _find_by_name(name):
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    print(f"MIC_NAME '{name}' not found, falling back to MIC_DEVICE index.")
    return None


_resolved = _find_by_name(MIC_NAME) if MIC_NAME else MIC_DEVICE
if _resolved is not None:
    sd.default.device = (_resolved, None)
MIC_DEVICE = _resolved

REC_RATE = SAMPLE_RATE
try:
    sd.check_input_settings(device=MIC_DEVICE, samplerate=SAMPLE_RATE)
except Exception:
    dev = sd.query_devices(MIC_DEVICE, "input") if MIC_DEVICE is not None else sd.query_devices(kind="input")
    REC_RATE = int(dev["default_samplerate"])
    print(f"Mic doesn't support {SAMPLE_RATE}Hz, recording at {REC_RATE}Hz instead.")


def _resample(audio, orig_rate, target_rate):
    if orig_rate == target_rate:
        return audio
    n_out = int(len(audio) * target_rate / orig_rate)
    x_old = np.linspace(0, 1, len(audio))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, audio.flatten()).astype(np.int16).reshape(-1, 1)


def _save(audio):
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    sf.write(f.name, audio, REC_RATE)
    return f.name


def record_while(flag, should_abort=None):
    """PTT capture: record while flag() is true. should_abort() is polled every
    ~0.1s; if it fires (TTS started, or exit requested) the capture is ABANDONED
    and None returned - so a fragment truncated by Cortana's own voice starting
    is never processed as a command."""
    frames = []
    aborted = False
    with sd.InputStream(samplerate=REC_RATE, channels=1, dtype="int16") as st:
        while flag():
            if should_abort and should_abort():
                aborted = True
                break
            data, _ = st.read(int(REC_RATE * 0.1))
            frames.append(data.copy())
    if aborted or not frames:
        return None
    audio = np.concatenate(frames)
    if len(audio) < REC_RATE * 0.3:
        return None
    return _save(audio)


def listen_segment(silence_s=0.8, max_s=30, wait_s=5, on_speech_start=None,
                   should_abort=None):
    """Block up to wait_s for speech; then record until silence_s of quiet.
    on_speech_start fires once when voice is first detected (lets the caller
    flag 'user is talking' so queued announcements hold politely).
    should_abort() is polled every ~0.1s: if it fires (Cortana's TTS started
    mid-capture, or an exit was requested) the capture is dropped and None
    returned - this is the half-duplex gate that stops her hearing herself,
    since TTS can begin AFTER this call is already recording."""
    frames, started, silent = [], False, 0.0
    t0 = time.time()
    with sd.InputStream(samplerate=REC_RATE, channels=1, dtype="int16") as st:
        while True:
            if should_abort and should_abort():
                return None
            data, _ = st.read(int(REC_RATE * 0.1))
            rms = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
            if not started:
                if rms > VAD_THRESHOLD:
                    started = True
                    if on_speech_start:
                        try:
                            on_speech_start()
                        except Exception:
                            pass
                    frames.append(data.copy())
                elif time.time() - t0 > wait_s:
                    return None
            else:
                frames.append(data.copy())
                silent = silent + 0.1 if rms < VAD_THRESHOLD else 0.0
                if silent >= silence_s or time.time() - t0 > max_s:
                    break
    audio = np.concatenate(frames)
    if len(audio) < REC_RATE * 0.4:
        return None
    return _save(audio)
