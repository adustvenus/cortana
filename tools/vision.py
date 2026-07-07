"""Screen capture -> base64 image block for Claude vision.
Requires X11 (Xorg session). On Wayland, mss fails -> see SETUP.md failure matrix.
"""
import base64
import io

import mss
from PIL import Image


def screenshot():
    with mss.mss() as s:
        raw = s.grab(s.monitors[1])  # primary monitor
    img = Image.frombytes("RGB", raw.size, raw.rgb)
    img.thumbnail((1568, 1568))  # Claude vision sweet spot, keeps tokens down
    buf = io.BytesIO()
    img.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": b64}},
        {"type": "text", "text": "Screenshot of the user's primary monitor."},
    ]
