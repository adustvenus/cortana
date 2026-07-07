"""Mic diagnostic. Run: ./venv/bin/python mic_test.py"""
import subprocess
import numpy as np
import sounddevice as sd
import soundfile as sf

print("\n=== Devices ===")
print(sd.query_devices())

idx = input("\nDevice index to test (blank=default): ").strip()
idx = int(idx) if idx else None

for rate in (16000, 44100, 48000):
    try:
        sd.check_input_settings(device=idx, samplerate=rate)
        print(f"OK  {rate}Hz supported")
    except Exception as e:
        print(f"FAIL {rate}Hz: {e}")

dev = sd.query_devices(idx, "input") if idx is not None else sd.query_devices(kind="input")
rate = int(dev["default_samplerate"])
print(f"\nRecording 3s at {rate}Hz, device={idx}...")

sd.default.device = (idx, None) if idx is not None else None
audio = sd.rec(int(3 * rate), samplerate=rate, channels=1, dtype="int16")
sd.wait()

peak = np.abs(audio).max()
rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
print(f"Peak: {peak} (max 32767)  RMS: {rms:.1f}")
if peak > 32000:
    print("CLIPPING - gain too high, lower mic input level")
elif rms < 50:
    print("Near silent - wrong device, muted, or not connected")
elif rms < 300:
    print("Very quiet - raise gain or VAD_THRESHOLD too high")
else:
    print("Signal level looks reasonable")

sf.write("mic_test.wav", audio, rate)
print("\nSaved mic_test.wav, playing back...")
subprocess.run(["aplay", "mic_test.wav"])