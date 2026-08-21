"""Cortana config. All tunables live here or in .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# --- API keys (set in .env) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")          # Whisper STT + TTS fallback
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")  # primary TTS
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # "Rachel" - light female

# --- Workspace: Cortana has full autonomy INSIDE this folder ---
WORKSPACE = Path(os.getenv("WORKSPACE", str(Path.home() / "workspace"))).expanduser()
WORKSPACE.mkdir(parents=True, exist_ok=True)

# --- Models ---
# IDs are complete as-is - never append a date suffix (claude-haiku-4-5, not
# claude-haiku-4-5-20251001). Verify strings at platform.claude.com/docs if 404.
MODEL_LEAD = os.getenv("MODEL_LEAD", "claude-sonnet-5")     # voice lead - latency matters
MODEL_FAST = os.getenv("MODEL_FAST", "claude-haiku-4-5")    # wake gate + critique, no reasoning
MODEL_HEAVY = os.getenv("MODEL_HEAVY", "claude-opus-5")     # dev/subagent work worth thinking about

# Reasoning effort per tier: low | medium | high | xhigh | max. Only reaches
# models in ADAPTIVE_THINKING_MODELS; the rest reject it with a 400.
EFFORT_LEAD = os.getenv("EFFORT_LEAD", "medium")   # keeps the spoken turn snappy
EFFORT_HEAVY = os.getenv("EFFORT_HEAVY", "high")

# Claude 4.6 and newer take thinking={"type": "adaptive"} plus
# output_config.effort. Haiku 4.5 and older reject BOTH with a 400, so they get
# neither - see reasoning_kwargs(). Add new model IDs here as they ship.
ADAPTIVE_THINKING_MODELS = {
    "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
}


def reasoning_kwargs(model, effort=None):
    """Extra messages.create() kwargs that turn on adaptive thinking, or {} for
    models that don't support it.

    Omitting `thinking` is NOT the same as adaptive - on Sonnet 5 and the 4.6+
    family it means the model reasons not at all, which is what this codebase
    was silently doing before. display="summarized" is what feeds the HUD's
    live reasoning line; it costs nothing extra, thinking is billed the same
    either way.
    """
    if model not in ADAPTIVE_THINKING_MODELS:
        return {}
    kw = {"thinking": {"type": "adaptive", "display": "summarized"}}
    if effort:
        kw["output_config"] = {"effort": effort}
    return kw


# --- Voice ---
MODE = os.getenv("MODE", "ptt")  # ptt | wake | open  (F10 cycles at runtime)
WAKE_REGEX = r"^\s*(ok(ay)?[\s,]+|hey[\s,]+)?cortana[,.!?\s]*(.*)$"
SAMPLE_RATE = 16000
MIC_DEVICE = os.getenv("MIC_DEVICE")  # optional int index from sounddevice.query_devices()
MIC_DEVICE = int(MIC_DEVICE) if MIC_DEVICE not in (None, "") else None
MIC_NAME = os.getenv("MIC_NAME", "")  # substring match, e.g. "USB" - overrides MIC_DEVICE, survives index shuffling
VAD_THRESHOLD = int(os.getenv("VAD_THRESHOLD", "350"))  # raise if it triggers on room noise

# --- Audio ducking (lower Spotify while she's being talked to / talking) ---
DUCK_ENABLED = os.getenv("DUCK_ENABLED", "1") not in ("0", "false", "False")
DUCK_SINK_MATCH = os.getenv("DUCK_SINK_MATCH", "spotifyd")  # pactl application.name substring
DUCK_FACTOR = float(os.getenv("DUCK_FACTOR", "0.25"))       # fraction of current volume while ducked

# --- Budget ---
BUDGET_MONTHLY_USD = float(os.getenv("BUDGET_MONTHLY_USD", "50"))
# $/MTok (input, output). Keyed by literal model ID so a MODEL_* override in
# .env still gets priced correctly. Unknown models fall back in _track().
# Update if Anthropic pricing changes.
PRICES = {
    "claude-fable-5":    (10.0, 50.0),
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-sonnet-5":   (3.0, 15.0),   # $2/$10 intro rate through 2026-08-31
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}

# --- Misc ---
# main.py exits with this code for a voice/tool shutdown; launcher.py treats it
# as "stop, do not relaunch". A plain exit 0 means restart (relaunch).
SHUTDOWN_CODE = 42
# Adaptive thinking spends against max_tokens too, so 2048 truncated real
# answers mid-thought once reasoning was turned on. Ceiling, not a target -
# terseness is enforced by CORTANA.md, not by starving the budget.
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8192"))
DB_PATH = ROOT / "state.db"
CORTANA_MD = ROOT / "CORTANA.md"
GMAIL_CREDS = ROOT / "credentials.json"   # download from Google Cloud Console
GMAIL_TOKEN = ROOT / "token.json"         # auto-created on first OAuth
