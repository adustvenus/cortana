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

# --- Models (verify strings at platform.claude.com/docs if 404) ---
MODEL_LEAD = os.getenv("MODEL_LEAD", "claude-sonnet-4-6")
MODEL_FAST = os.getenv("MODEL_FAST", "claude-haiku-4-5-20251001")
MODEL_HEAVY = os.getenv("MODEL_HEAVY", "claude-opus-4-8")

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
# $/MTok (input, output). Update if Anthropic pricing changes.
PRICES = {
    MODEL_LEAD: (3.0, 15.0),
    MODEL_FAST: (1.0, 5.0),
    MODEL_HEAVY: (5.0, 25.0),
}

# --- Misc ---
# main.py exits with this code for a voice/tool shutdown; launcher.py treats it
# as "stop, do not relaunch". A plain exit 0 means restart (relaunch).
SHUTDOWN_CODE = 42
MAX_TOKENS = 2048
DB_PATH = ROOT / "state.db"
CORTANA_MD = ROOT / "CORTANA.md"
GMAIL_CREDS = ROOT / "credentials.json"   # download from Google Cloud Console
GMAIL_TOKEN = ROOT / "token.json"         # auto-created on first OAuth
