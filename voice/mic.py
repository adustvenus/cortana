"""Microphone capture. Two modes:
- record_while(flag): PTT - records while F9 held
- listen_segment(): continuous - waits for speech, records until silence (energy VAD)

Device handling is lazy and fault-tolerant ON PURPOSE: nothing here touches
audio hardware at import time. An unplugged mic (or an HDMI output being the
system default "device") used to raise during `from voice import mic`, killing
the whole process before any error handling existed - which then tripped the
launcher's crash-loop revert. Now every capture resolves the device fresh, a
missing mic raises MicUnavailable (callers SPEAK it, never crash), and
plugging in / re-picking a mic needs no restart.

Selection precedence: mic_select.json (dashboard AI-module dropdown) >
MIC_NAME (.env) > MIC_DEVICE (.env) > system default input > first device
that can capture. publish_state() writes mic_state.json - the inventory the
dashboard dropdown reads. Both files are runtime state (gitignored).
"""
import json
import os
import tempfile
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import MIC_DEVICE, MIC_NAME, ROOT, SAMPLE_RATE, VAD_THRESHOLD

SELECT_FILE = ROOT / "mic_select.json"
STATE_FILE = ROOT / "mic_state.json"


class MicUnavailable(Exception):
    """No usable input device right now. Callers report it; never a crash."""


def _selected_name():
    """Device name picked from the dashboard dropdown, '' if none."""
    try:
        return str(json.loads(SELECT_FILE.read_text()).get("name") or "")
    except Exception:
        return ""


def list_inputs():
    """[{index, name}] for every device that can capture audio. Never raises."""
    out, seen = [], set()
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0 and d["name"] not in seen:
                seen.add(d["name"])
                out.append({"index": i, "name": d["name"]})
    except Exception:
        pass
    return out


def _match(devices, name):
    """Index for a device name: exact match first, then substring (MIC_NAME style)."""
    if not name:
        return None
    for d in devices:
        if d["name"].lower() == name.lower():
            return d["index"]
    for d in devices:
        if name.lower() in d["name"].lower():
            return d["index"]
    return None


def _resolve():
    """(device_index, usable_samplerate). Raises MicUnavailable when nothing
    can capture - including the 'default device is an HDMI output' case."""
    devices = list_inputs()
    if not devices:
        raise MicUnavailable("no input devices")
    idx = _match(devices, _selected_name())
    if idx is None:
        idx = _match(devices, MIC_NAME)
    if idx is None and MIC_DEVICE is not None and any(d["index"] == MIC_DEVICE for d in devices):
        idx = MIC_DEVICE
    if idx is None:
        try:
            di = sd.default.device[0]
            if di is not None and di >= 0 and any(d["index"] == di for d in devices):
                idx = di
        except Exception:
            pass
    if idx is None:
        idx = devices[0]["index"]
    rate = SAMPLE_RATE
    try:
        sd.check_input_settings(device=idx, samplerate=SAMPLE_RATE)
    except Exception:
        try:
            rate = int(sd.query_devices(idx)["default_samplerate"])
        except Exception as e:
            raise MicUnavailable(f"device unusable: {e}")
    return idx, rate


def available():
    try:
        _resolve()
        return True
    except MicUnavailable:
        return False


def current_name():
    try:
        idx, _ = _resolve()
        return str(sd.query_devices(idx)["name"])
    except Exception:
        return ""


def publish_state():
    """Write mic_state.json for the dashboard's MIC dropdown. Never raises."""
    try:
        payload = {"devices": list_inputs(), "current": current_name(),
                   "available": available(), "ts": time.time()}
        tmp = tempfile.NamedTemporaryFile("w", dir=STATE_FILE.parent,
                                          delete=False, suffix=".tmp")
        json.dump(payload, tmp)
        tmp.close()
        os.replace(tmp.name, STATE_FILE)
    except Exception:
        pass


def _save(audio, rate):
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    sf.write(f.name, audio, rate)
    return f.name


def record_while(flag, should_abort=None):
    """PTT capture: record while flag() is true. should_abort() is polled every
    ~0.1s; if it fires (TTS started, or exit requested) the capture is ABANDONED
    and None returned - so a fragment truncated by Cortana's own voice starting
    is never processed as a command. Raises MicUnavailable when no mic works."""
    idx, rate = _resolve()
    frames = []
    aborted = False
    try:
        with sd.InputStream(device=idx, samplerate=rate, channels=1, dtype="int16") as st:
            while flag():
                if should_abort and should_abort():
                    aborted = True
                    break
                data, _ = st.read(int(rate * 0.1))
                frames.append(data.copy())
    except sd.PortAudioError as e:
        raise MicUnavailable(f"mic vanished mid-capture: {e}")
    if aborted or not frames:
        return None
    audio = np.concatenate(frames)
    if len(audio) < rate * 0.3:
        return None
    return _save(audio, rate)


def listen_segment(silence_s=0.8, max_s=30, wait_s=5, on_speech_start=None,
                   should_abort=None):
    """Block up to wait_s for speech; then record until silence_s of quiet.
    on_speech_start fires once when voice is first detected (lets the caller
    flag 'user is talking' so queued announcements hold politely).
    should_abort() is polled every ~0.1s: if it fires (Cortana's TTS started
    mid-capture, or an exit was requested) the capture is dropped and None
    returned - this is the half-duplex gate that stops her hearing herself,
    since TTS can begin AFTER this call is already recording.
    Raises MicUnavailable when no mic works."""
    idx, rate = _resolve()
    frames, started, silent = [], False, 0.0
    t0 = time.time()
    try:
        with sd.InputStream(device=idx, samplerate=rate, channels=1, dtype="int16") as st:
            while True:
                if should_abort and should_abort():
                    return None
                data, _ = st.read(int(rate * 0.1))
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
    except sd.PortAudioError as e:
        raise MicUnavailable(f"mic vanished mid-capture: {e}")
    audio = np.concatenate(frames)
    if len(audio) < rate * 0.4:
        return None
    return _save(audio, rate)
