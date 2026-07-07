"""Video editing via ffmpeg. All file paths forced inside WORKSPACE."""
import shlex
import subprocess

from config import WORKSPACE
from .files import _p

MEDIA_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".mp3", ".wav",
             ".aac", ".png", ".jpg", ".jpeg", ".gif", ".srt")


def ffmpeg_edit(args, timeout=900):
    """args: everything after 'ffmpeg', e.g.
    '-i clip.mp4 -ss 00:00:05 -to 00:00:30 -c copy out.mp4'
    Prefix 'ffprobe' in args to probe instead.
    """
    toks = shlex.split(args)
    prog = "ffmpeg"
    if toks and toks[0] in ("ffmpeg", "ffprobe"):
        prog = toks.pop(0)
    clean = []
    for t in toks:
        if "/" in t or t.lower().endswith(MEDIA_EXT):
            clean.append(str(_p(t)))  # raises if outside workspace
        else:
            clean.append(t)
    cmd = [prog, "-y", *clean] if prog == "ffmpeg" else [prog, *clean]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout, cwd=WORKSPACE)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    return out[-6000:].strip() or f"(exit {p.returncode})"
