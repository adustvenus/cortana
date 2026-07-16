"""Cortana HUD: frameless translucent top strip.

Waveform matches the reference: dense thin vertical bars mirrored around a
horizontal midline, tallest at screen-center and tapering smoothly to nothing
at both edges (a bell envelope), with the wave travelling outward. Soft glow.

Below the wave: a live 'thinking' feed - the latest reasoning/status line, the
way visible thinking reads in a chat.

State file (hud_state.json) drives speed + amplitude:
  idle=slow/low  listening/thinking/working=medium  speaking=fast/high

Run standalone: ./venv/bin/python hud.py   (launcher normally starts it)
Quit: exits when state == 'offline', or on SIGTERM from launcher.
"""
import ctypes
import math
import sys

from PyQt5 import QtCore, QtGui, QtWidgets

from hud_state import read_state


def _empty_x11_input_region(win_id):
    """Set an empty X11 input shape on the native window so ALL pointer events
    pass through to whatever is beneath it. This is the reliable click-through
    path on X11 - Qt's WindowTransparentForInput/WA_TransparentForMouseEvents
    do not always empty the native input shape (esp. with override-redirect),
    which leaves the window swallowing clicks instead of passing them down.
    No-op / harmless if the X libs or session aren't X11 (e.g. Wayland)."""
    try:
        x11 = ctypes.CDLL("libX11.so.6")
        xext = ctypes.CDLL("libXext.so.6")
    except OSError:
        return False
    ShapeInput, ShapeSet = 2, 0  # X11 shape constants
    xext.XShapeCombineRectangles.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        return False
    try:
        # NULL rect list + count 0 => empty input region => full click-through
        xext.XShapeCombineRectangles(
            dpy, ctypes.c_ulong(int(win_id)), ShapeInput, 0, 0, None, 0,
            ShapeSet, 0)
        x11.XFlush(dpy)
    finally:
        x11.XCloseDisplay(dpy)
    return True

HEIGHT = 78          # strip height (wave + thinking line)
WAVE_H = 52          # vertical space the mirrored wave uses
BAR_GAP = 4          # px between bars (controls density)
FPS = 60

# per-state: (phase_speed, target_amp 0..1, rgb)
PROFILES = {
    "idle":      (0.5, 0.10, (150, 165, 195)),
    "listening": (1.5, 0.55, (110, 210, 175)),
    "thinking":  (1.0, 0.40, (150, 170, 255)),
    "working":   (1.0, 0.45, (150, 170, 255)),
    "speaking":  (2.6, 0.95, (170, 200, 255)),
    "offline":   (0.0, 0.0,  (80, 80, 80)),
}


class HUD(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowTransparentForInput  # empties the X11 input shape - true click-through
            | QtCore.Qt.X11BypassWindowManagerHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)  # click-through (Qt-side)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)      # never steal focus/clicks

        scr = QtWidgets.QApplication.primaryScreen().geometry()
        self.setGeometry(scr.x(), scr.y(), scr.width(), HEIGHT)

        self.phase = 0.0
        self.amp = 0.0
        self.state = "idle"
        self.agent = ""
        self.thought = ""

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(1000 / FPS))

        self.poll = QtCore.QTimer(self)
        self.poll.timeout.connect(self._poll)
        self.poll.start(120)

    def showEvent(self, e):
        super().showEvent(e)
        # Native window now exists; empty its X11 input shape for true
        # click-through. Guarded to the X11 (xcb) platform.
        if QtWidgets.QApplication.platformName() == "xcb":
            _empty_x11_input_region(self.winId())

    def _poll(self):
        s = read_state()
        self.state = s.get("state", "idle")
        self.agent = s.get("agent", "")
        th = s.get("thoughts") or []
        self.thought = th[-1] if th else ""
        if self.state == "offline":
            QtWidgets.QApplication.quit()

    def _tick(self):
        speed, target, _ = PROFILES.get(self.state, PROFILES["idle"])
        self.phase += 0.06 * speed
        self.amp += (target - self.amp) * 0.12       # smooth ease
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w = self.width()
        cx = w / 2
        mid = 6 + WAVE_H / 2                          # horizontal midline of the wave
        _, _, (r, g, b) = PROFILES.get(self.state, PROFILES["idle"])

        n = int(cx / BAR_GAP)
        for i in range(n):
            frac = i / n                             # 0 at center -> 1 at edge
            # bell envelope: tall at center, ~0 at edges (gaussian)
            env = math.exp(-(frac * 2.3) ** 2)
            # travelling wave riding on the envelope
            wave = math.sin(self.phase - frac * 9.0) * 0.5 + 0.5
            h = (WAVE_H / 2) * self.amp * env * (0.55 + 0.45 * wave)
            if h < 0.6:
                continue
            alpha = int(235 * env * (0.5 + 0.5 * self.amp))
            # glow underlay + crisp core, drawn on both sides of center
            for x in (cx + i * BAR_GAP, cx - i * BAR_GAP):
                glow = QtGui.QColor(r, g, b, int(alpha * 0.35))
                p.setPen(QtGui.QPen(glow, 2.4))
                p.drawLine(QtCore.QPointF(x, mid - h), QtCore.QPointF(x, mid + h))
                core = QtGui.QColor(min(r + 40, 255), min(g + 40, 255), 255, alpha)
                p.setPen(QtGui.QPen(core, 1.0))
                p.drawLine(QtCore.QPointF(x, mid - h), QtCore.QPointF(x, mid + h))

        # live thinking feed under the wave
        label = self.thought or self.agent
        if label:
            p.setPen(QtGui.QColor(200, 212, 240, 235))
            f = p.font(); f.setPointSize(9); p.setFont(f)
            if self.agent and self.thought:
                label = f"[{self.agent}] {self.thought}"
            p.drawText(QtCore.QRectF(0, HEIGHT - 20, w, 18),
                       QtCore.Qt.AlignCenter, label[:140])
        p.end()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    hud = HUD()
    hud.show()
    import signal
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    keep = QtCore.QTimer(); keep.start(200); keep.timeout.connect(lambda: None)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
