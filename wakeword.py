"""Offline wake-word gate. Engine-agnostic, and dormant unless configured.

Today the wake path is transcribe-then-regex: every utterance in the room buys
a Whisper call before it can be rejected. That is the blocker on always-on
listening, in both cost and privacy. This module is the gate that goes in
front of it.

Two backends behind one interface:

  * openWakeWord - ONNX, offline, free, no key. The intended one. A custom
    "Cortana" model gets trained on the Windows box with a GPU and committed
    as a .onnx; this box only ever runs inference. A few MB of weights and
    tens of MB resident - fine here, but only because nothing is loaded until
    something actually asks. Import time stays clean.
  * Picovoice Porcupine - the documented fallback. More accurate out of the
    box, but it needs an access key and a per-platform .ppn, so it is not the
    default and never loads unless asked for by name.

The SHIPPED state is neither installed and no model on disk, and that path is
the one that matters most: available() returns False with a sentence saying
why, detect_wav() fails OPEN, and main.py behaves exactly as it does today.
A wake gate that fails closed makes Cortana deaf, which is a far worse failure
than the API call it was trying to save. Every error path here therefore ends
in "carry on as before", never in "drop the audio".

Frame size and sample rate are pinned to voice/mic.py's SAMPLE_RATE. Feeding a
model 44.1k audio it believes is 16k does not raise - it just never fires
again, which is exactly the kind of silent failure this file refuses to have.
"""
import time
from array import array
from pathlib import Path

import config
from config import SAMPLE_RATE

# openWakeWord's own framing. It will accept other chunk sizes, but 80ms at
# 16k is what its melspectrogram front end is built around, and matching it
# means the buffering below is the only place chunk size is ever decided.
_OWW_FRAME = 1280

_INT16_MIN, _INT16_MAX = -32768, 32767

# Everything mutable in one dict rather than a pile of `global` statements -
# same shape as presence._idle_probe, and it makes reload() a one-liner.
_st = {"resolved": False, "backend": None, "reason": "", "degraded": "",
       "buf": array("h"), "mute_until": 0.0}


class _Unavailable(Exception):
    """Backend cannot run, with a sentence fit to be spoken aloud."""


class _Backend:
    """What every engine reduces to: a frame length and a bool per frame.

    Deliberately not an ABC with subclasses. The whole point of this file is
    that swapping openWakeWord for Porcupine touches one loader function and
    nothing else, so the shared surface is kept as small as it can possibly be.
    """

    def __init__(self, name, frame, predict):
        self.name = name
        # max(1, ...): a frame length of zero would make detect()'s drain loop
        # spin forever inside the capture thread, which is the one failure here
        # that is worse than being deaf. Neither loader can produce it today;
        # the guard is for the day a library changes its mind.
        self.frame = max(1, int(frame or 0))
        self.predict = predict


def _cfg(name, default):
    """Read a config key that may not exist yet.

    The config additions for this feature land as a separate change to a file
    this module does not own. Until they do, every key must fall back to
    "dormant" rather than raise at import and take the voice loop with it.
    """
    return getattr(config, name, default)


def _model_path(rel):
    """Absolute path to the model file, or None if no path is configured.

    Relative paths resolve against the repo root so a committed model can be
    named in config the same way on both boxes.
    """
    raw = str(rel or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (config.ROOT / p)


# ── backends ───────────────────────────────────────────────────────────────
def _load_oww():
    path = _model_path(_cfg("WAKEWORD_MODEL", "voice/models/cortana.onnx"))
    if path is None:
        raise _Unavailable("No wake word model file is configured.")
    # Checked before the import: a missing model is the expected state on a
    # fresh clone, and it deserves a better sentence than an import error.
    if not path.exists():
        raise _Unavailable(f"I have no wake word model at {path.name}.")
    try:
        from openwakeword.model import Model
    except Exception:
        raise _Unavailable("The openwakeword library is not installed here.")
    threshold = _float(_cfg("WAKEWORD_THRESHOLD", 0.5), 0.5)
    # onnx, not tflite: the tflite runtime is the larger dependency and this
    # box is RAM bound, not CPU bound.
    model = Model(wakeword_models=[str(path)], inference_framework="onnx")

    def predict(samples):
        import numpy as np
        scores = model.predict(np.asarray(samples, dtype=np.int16))
        vals = [float(v) for v in getattr(scores, "values", lambda: [])()]
        return bool(vals) and max(vals) >= threshold

    return _Backend("openwakeword", _OWW_FRAME, predict)


def _load_porcupine():
    key = str(_cfg("PICOVOICE_ACCESS_KEY", "")).strip()
    if not key:
        raise _Unavailable("Porcupine needs a Picovoice access key and none is set.")
    try:
        import pvporcupine
    except Exception:
        raise _Unavailable("The pvporcupine library is not installed here.")
    path = _model_path(_cfg("WAKEWORD_MODEL", ""))
    kw = {"access_key": key,
          "sensitivities": [_float(_cfg("WAKEWORD_THRESHOLD", 0.5), 0.5)]}
    if path is not None and path.suffix == ".ppn" and path.exists():
        kw["keyword_paths"] = [str(path)]
    else:
        # Porcupine has no built-in "cortana", and its .ppn files are built
        # per platform, so a Windows-trained keyword will not load on the
        # Linux box. Falling back to a stock keyword keeps the path runnable
        # for a smoke test instead of failing in a way nobody can reproduce.
        kw["keywords"] = [str(_cfg("WAKEWORD_BUILTIN", "computer"))]
    p = pvporcupine.create(**kw)
    if int(p.sample_rate) != int(SAMPLE_RATE):
        p.delete()
        raise _Unavailable(f"Porcupine wants {p.sample_rate} hertz and the "
                           f"microphone gives {SAMPLE_RATE}.")
    return _Backend("porcupine", p.frame_length, lambda s: p.process(s) >= 0)


def _float(v, default):
    try:
        return float(v)
    except Exception:
        return default


def _redact(msg):
    """Strip the Picovoice access key out of anything we are about to say.

    Porcupine quotes the key back in several of its own error messages, and
    reason() is both spoken aloud and published by status() to the dashboard.
    A secret that only leaks on the error path still leaks.
    """
    key = str(_cfg("PICOVOICE_ACCESS_KEY", "")).strip()
    return msg.replace(key, "<access key>") if len(key) > 6 else msg


def _degrade(msg):
    """Record - once - that a loaded engine is passing everything through.

    Failing open is the right direction, but silence about it is not: the
    saving vanishes and nothing anywhere says why. reason() reports this in
    preference to the load-time sentence, so status() and "is wake word on"
    both tell the truth rather than "running on openwakeword".
    """
    if _st["degraded"] != msg:
        _st["degraded"] = msg
        print("[wakeword]", msg)


def _load():
    """Resolve the backend once. Never raises; sets a spoken reason every time.

    Resolution is cached because the failure case is the common one and it
    involves a disk stat and a failed import - not something to repeat on
    every audio chunk. reload() is the way back out.
    """
    if _st["resolved"]:
        return _st["backend"]
    _st["resolved"] = True
    engine = str(_cfg("WAKEWORD_ENGINE", "")).strip().lower()
    if engine in ("", "off", "none", "0", "false"):
        _st["reason"] = ("Offline wake word is switched off. Nothing changes "
                         "until a wake word engine is named in config.")
        return None
    try:
        if engine == "openwakeword":
            _st["backend"] = _load_oww()
        elif engine == "porcupine":
            _st["backend"] = _load_porcupine()
        else:
            raise _Unavailable(f"I do not know a wake word engine called {engine}.")
    except _Unavailable as e:
        _st["reason"] = str(e)
    except Exception as e:
        # Anything the library itself throws: a corrupt .onnx, a missing
        # melspectrogram download, a bad access key. Report it, stay dormant.
        _st["reason"] = _redact(f"The wake word engine failed to start. {e}")
    if _st["backend"] is not None:
        _st["reason"] = f"Offline wake word is running on {engine}."
    return _st["backend"]


# ── the interface the voice loop uses ──────────────────────────────────────
def available():
    """True only when a backend is loaded and ready. False is the default."""
    return _load() is not None


def reason():
    """One sentence, speakable, on why the gate is or is not running."""
    _load()
    return _st["degraded"] or _st["reason"]


def engine():
    b = _load()
    return b.name if b else ""


def frame_samples():
    """Samples per inference at SAMPLE_RATE, or 0 when dormant."""
    b = _load()
    return b.frame if b else 0


def reload():
    """Forget the resolved backend so the next call re-reads config and disk.

    Exists so a model file dropped in after the fact takes effect without a
    restart - the training run happens on another machine, and the file
    arrives by git pull while Cortana is already up.
    """
    _st.update(resolved=False, backend=None, reason="", degraded="")
    reset()


def reset():
    """Drop buffered audio and any cooldown. Call between captures so a frame
    straddling two unrelated recordings can never be assembled."""
    _st["buf"] = array("h")
    _st["mute_until"] = 0.0


def detect(chunk):
    """True when the wake word completes inside this chunk of PCM.

    Accepts bytes, an int16 numpy block from sounddevice, or a plain list.
    NEVER raises: this runs inside the capture loop, and a malformed chunk or
    a backend that throws mid-stream must not stop Cortana from listening.
    """
    b = _load()
    if b is None:
        return False
    try:
        samples = _to_samples(chunk)
        if not samples:
            return False
        buf = _st["buf"]
        buf.extend(samples)
        hit = False
        # Walk with an offset and delete once at the end. `del buf[:frame]` per
        # frame is O(remaining), and detect_wav() hands this a whole 30-second
        # capture in one call - 375 frames each shifting the rest of the array
        # is quadratic work in front of every wake-mode turn.
        pos, end = 0, len(buf)
        while end - pos >= b.frame:
            frame = buf[pos:pos + b.frame].tolist()
            pos += b.frame
            # Cooldown still consumes frames rather than skipping the loop, so
            # the buffer cannot grow without bound while muted.
            if time.time() < _st["mute_until"]:
                continue
            if b.predict(frame):
                # One spoken "Cortana" spans several frames and would fire on
                # each of them; the loop stops here and the mute window covers
                # the rest of the word.
                _st["mute_until"] = time.time() + _float(_cfg("WAKEWORD_COOLDOWN", 2.0), 2.0)
                hit = True
                break
        if pos:
            del buf[:pos]
        return hit
    except Exception as e:
        # A backend throwing per frame would spam the journal and add latency
        # to every chunk forever. Take it out of service and say why, once.
        # The buffer goes too: it may hold a whole capture, and with no backend
        # left nothing will ever drain it.
        _st["backend"] = None
        _st["buf"] = array("h")
        _st["reason"] = _redact(f"The wake word engine stopped working. {e}")
        return False


def detect_wav(path):
    """True when the wake word appears anywhere in a finished capture.

    This is the cheap way to put the gate in front of stt.transcribe() without
    restructuring mic.py: the capture already happened, but the Whisper call
    has not. It FAILS OPEN - dormant engine, no soundfile, unreadable file,
    wrong sample rate all return True, meaning "carry on and transcribe it",
    which is exactly today's behaviour.
    """
    if not available():
        return True
    try:
        import soundfile as sf
        audio, rate = sf.read(str(path), dtype="int16")
    except Exception:
        return True
    # _float, not int(): detect_wav is documented as failing open on EVERY
    # path, and a rate of None from an exotic container would otherwise raise
    # out of here into the voice loop.
    rate = int(_float(rate, 0))
    if rate != int(SAMPLE_RATE):
        # Resampling belongs in capture, not here. Feeding the model audio at
        # the wrong pitch does not raise, it just never fires - so let the
        # utterance through instead. This is NOT hypothetical: mic._resolve()
        # drops to a device's native rate whenever 16k is unsupported and
        # writes the WAV at that rate, at which point the gate quietly stops
        # gating. _degrade() is what stops that being invisible.
        heard = f"at {rate} hertz" if rate > 0 else "at a rate I cannot read"
        _degrade(f"The wake word gate is passing everything through: the "
                 f"microphone recorded {heard} and the model needs "
                 f"{int(SAMPLE_RATE)}.")
        return True
    _st["degraded"] = ""
    try:
        if getattr(audio, "ndim", 1) > 1:
            audio = audio[:, 0]
        reset()
        return detect(audio)
    except Exception:
        return True
    finally:
        reset()


def status():
    """Small dict for the dashboard and for answering "is wake word on"."""
    b = _load()
    return {"engine": b.name if b else "", "available": b is not None,
            "reason": reason(), "frame": b.frame if b else 0,
            "sample_rate": SAMPLE_RATE}


def _scale(v):
    """One float sample (or a one-element frame of them) to the int16 scale."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else 0
    try:
        return float(v) * _INT16_MAX
    except Exception:
        return 0


def _to_samples(chunk):
    """Anything a caller might hand us -> array('h') of int16. Never raises.

    All three shapes really occur: raw bytes from a stream read, an int16
    numpy block from sounddevice, and a plain list from a test. A fourth,
    wrong, shape must be ignored rather than crash a listening loop.
    """
    if chunk is None:
        return array("h")
    if isinstance(chunk, array):
        # Our own buffer type, and what a caller gets back from frombytes.
        # Copied rather than aliased: detect() mutates the buffer it holds.
        return array("h", chunk) if chunk.typecode == "h" else _to_samples(chunk.tolist())
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        raw = bytes(chunk)
        if len(raw) % 2:          # a half sample means a truncated read
            raw = raw[:-1]
        out = array("h")
        try:
            out.frombytes(raw)
        except Exception:
            return array("h")
        return out
    tobytes = getattr(chunk, "tobytes", None)
    dtype = getattr(chunk, "dtype", None)
    if tobytes is not None and dtype is not None:
        # int16 is the only dtype mic.py produces, and its bytes are already
        # exactly what array('h') wants - no numpy import needed on this path.
        if str(dtype) == "int16":
            return _to_samples(tobytes())
        tolist = getattr(chunk, "tolist", None)
        try:
            # list(chunk) rather than giving up when there is no .tolist: an
            # object with .tobytes and .dtype and nothing else used to fall
            # through to an empty result, which reads as "the room was silent".
            chunk = tolist() if tolist else list(chunk)
        except Exception:
            chunk = []
        if str(dtype).startswith("float"):
            # A float block is -1.0..1.0, and the int() below would truncate
            # every sample to zero - perfect silence, which the model happily
            # never fires on and never complains about. mic.py asks for int16
            # today; this is so a change there cannot switch the gate off
            # without anyone noticing.
            chunk = [_scale(v) for v in chunk]
    if isinstance(chunk, (list, tuple)):
        out = array("h")
        for v in chunk:
            if isinstance(v, (list, tuple)):    # (frames, channels) - take mono
                v = v[0] if v else 0
            try:
                i = int(v)
            except Exception:
                continue
            out.append(max(_INT16_MIN, min(_INT16_MAX, i)))
        return out
    return array("h")
