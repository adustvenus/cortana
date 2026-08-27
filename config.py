"""Cortana config. All tunables live here or in .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
# Per-machine overrides, loaded last so they win. .env is shared across every
# box by ./secrets.sh and gets overwritten on each pull, so anything tuned to
# THIS machine - mic index, VAD threshold, voice - belongs here instead or it
# will be wiped the next time secrets are synced. Never distributed.
load_dotenv(ROOT / ".env.local", override=True)

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

# --- Prompt caching ---
# The stable prefix (tools + system + any reference documents) is cached so it
# is not re-billed every turn. 1h suits intermittent voice use better than the
# 5-minute default: the write costs more, but re-writing it after every pause
# costs more still.
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "1") not in ("0", "false", "False")
CACHE_TTL = os.getenv("CACHE_TTL", "1h")
# Billing multipliers against normal input: a cache write is 2x at the 1h TTL
# (1.25x at 5m), a read is 0.1x. _track needs these or reported spend is wrong.
CACHE_WRITE_MULT = 2.0 if CACHE_TTL == "1h" else 1.25
CACHE_READ_MULT = 0.1

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

# --- Scheduler / proactive spine ---
def _system_tz():
    """IANA zone name for recurrence math.

    NOT time.tzname or strftime('%Z') - those give an ABBREVIATION ('PDT'),
    which zoneinfo.ZoneInfo refuses to load and which is ambiguous across
    continents anyway. Read what the system actually configured instead.
    """
    tz = os.getenv("TZ", "").strip()
    if tz:
        return tz
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except Exception:
        pass
    try:                      # /etc/localtime is a symlink into .../zoneinfo/<Zone>
        return Path("/etc/localtime").resolve().as_posix().split("zoneinfo/", 1)[1]
    except Exception:
        pass
    # Deliberately "" and NOT "UTC". The Windows dev box has neither file, and
    # naming UTC here would be a lie the code cannot detect: a naive "in 20
    # minutes" would be read as a UTC wall time and silently shift by the whole
    # local offset. Empty means "no IANA name available" and lets schedule._zone
    # fall back to the system's real offset instead.
    return ""


SCHED_TZ = os.getenv("SCHED_TZ", "") or _system_tz()
# Tick cadence. 5s, not 1s: the politest delivery path already holds up to 60s
# for a quiet moment, so a finer tick buys nothing and wakes the CPU 86,400
# extra times a day on a laptop.
SCHED_TICK = float(os.getenv("SCHED_TICK", "5"))
# How long a fired-late item is still worth speaking, per kind. A pasta timer
# four hours late is worse than useless; an overslept alarm is still news.
SCHED_CATCHUP = {"timer": 900, "alarm": 3600, "reminder": 86400, "routine": 900}
# A row stuck in 'firing' longer than this lost its owner mid-fire (crash,
# restart) and is swept back to pending. Duplicate beats lost for a reminder.
SCHED_CLAIM_STALE = float(os.getenv("SCHED_CLAIM_STALE", "60"))

# --- Presence ---
PRESENCE_STALE = float(os.getenv("PRESENCE_STALE", "120"))   # older than this = unknown
PRESENT_IDLE = float(os.getenv("PRESENT_IDLE", "300"))       # X11 idle under this = at the desk
AWAY_IDLE = float(os.getenv("AWAY_IDLE", "1800"))            # over this = asleep

# --- Workstation control (tools/desktop.py) ---
# Synthetic keystrokes go to whatever window happens to have focus - a terminal,
# a chat box, a sudo prompt - with no undo and no confirmation. Off unless
# someone deliberately turns it on.
DESKTOP_TYPE_ENABLED = os.getenv("DESKTOP_TYPE_ENABLED", "0") not in ("0", "false", "False", "")
DESKTOP_TYPE_MAX = int(os.getenv("DESKTOP_TYPE_MAX", "2000"))   # chars typed blind, per call
# Clipboard contents land in the transcript and then in the model's context on
# every following turn. Cap it so one copied logfile is not the whole prompt.
DESKTOP_CLIP_MAX = int(os.getenv("DESKTOP_CLIP_MAX", "4000"))
# Desktop calls are interactive-latency work. Anything still running after this
# has hung on an X server that is not answering, with the voice loop behind it.
DESKTOP_TIMEOUT = float(os.getenv("DESKTOP_TIMEOUT", "5"))

# --- Media (tools/media.py) ---
# Nothing of its own: it reuses the dashboard's Spotify grant and the SHARED
# cool-off file, because three processes now poll one Spotify quota.

# --- Offline wake word (wakeword.py) ---
# openwakeword | porcupine | "" (off). EMPTY IS THE SHIPPED DEFAULT and means no
# behaviour change at all: wake mode still transcribes first, exactly as before.
# Nothing is imported or loaded until this is set.
WAKEWORD_ENGINE = os.getenv("WAKEWORD_ENGINE", "")
# Relative paths resolve against ROOT. The .onnx is TRAINED on the Windows box
# with a GPU and committed; the Linux box only ever runs inference on it.
WAKEWORD_MODEL = os.getenv("WAKEWORD_MODEL", "voice/models/cortana.onnx")
# Score at which a frame counts as the wake word. Raise it if the television
# sets her off, lower it if she is missed from across the room.
WAKEWORD_THRESHOLD = float(os.getenv("WAKEWORD_THRESHOLD", "0.5"))
# Seconds of deafness after a hit. One spoken "Cortana" spans several frames and
# would otherwise queue several turns off a single word.
WAKEWORD_COOLDOWN = float(os.getenv("WAKEWORD_COOLDOWN", "2.0"))
PICOVOICE_ACCESS_KEY = os.getenv("PICOVOICE_ACCESS_KEY", "")   # porcupine only
# Porcupine has no built-in "cortana" and its .ppn files are per platform, so a
# Windows-trained keyword will not load on the Linux box.
WAKEWORD_BUILTIN = os.getenv("WAKEWORD_BUILTIN", "computer")

# How many past verdicts get quoted into the open-mode "was that for me"
# classifier. This text is billed on EVERY overheard utterance, so it is a cost
# dial: 0 restores the old zero-shot prompt.
ADDRESS_EXAMPLES = int(os.getenv("ADDRESS_EXAMPLES", "8"))

# faster-whisper instead of the Whisper API. Off, and it should STAY off on the
# runtime box: base.en at int8 holds about a gigabyte resident out of the two
# that are free, to save an API call costing well under a cent a minute. RAM is
# the binding constraint there, not money. Set per-machine in .env.local.
STT_USE_LOCAL = os.getenv("STT_USE_LOCAL", "0") not in ("0", "", "false", "False")

# --- Knowledge layer (tools/notes.py) ---
# Where to look for local documents. Widening this does NOT widen what gets
# indexed past the safety rules: notes.excluded() refuses anything
# credential-shaped or machine-generated regardless of what is listed here.
NOTES_ROOTS = [WORKSPACE, Path.home() / "Documents", Path.home() / "Downloads"]
# Per-file ceiling, then a cap on how much of an accepted file is stored. A
# 40 MB log tells the index nothing its first 300 KB did not, and tokenising it
# stalls a pass on a 5 GB box.
NOTES_MAX_FILE_BYTES = int(os.getenv("NOTES_MAX_FILE_BYTES", "300000"))
NOTES_MAX_CHARS = int(os.getenv("NOTES_MAX_CHARS", "40000"))
# Per-pass budgets. Three of them because they fail differently: FILES caps stat
# syscalls, READS caps disk and CPU, SECONDS caps a pass that has wandered onto
# a network mount. A pass resumes where the last one stopped, so this bounds
# every tick without ever leaving part of the tree unindexed.
NOTES_PASS_FILES = int(os.getenv("NOTES_PASS_FILES", "400"))
NOTES_PASS_READS = int(os.getenv("NOTES_PASS_READS", "25"))
NOTES_PASS_SECONDS = float(os.getenv("NOTES_PASS_SECONDS", "5"))
# 5 minutes between passes, deliberately slow: documents do not change fast
# enough to justify more, and recall() re-syncs the conversation log itself.
NOTES_TICK = float(os.getenv("NOTES_TICK", "300"))

# --- Routines ---
# The routines engine rides the scheduler tick rather than running its own
# thread, but SCHED_TICK is 5s for the scheduler's benefit, and re-reading three
# state files twelve times a minute forever is exactly the idle burn a previous
# version had to be walked back for. This is how often the edge evaluator is
# actually allowed to run.
ROUTINE_TICK = float(os.getenv("ROUTINE_TICK", "30"))
# US ZIP for the morning brief's weather clause, using the same two keyless
# endpoints the dashboard already calls. Empty means no weather clause AND no
# network call at all.
BRIEF_ZIP = os.getenv("BRIEF_ZIP", "")

# --- Sentinel (system / network / account watch) ---
# Cadence of the whole loop. 60s, not 5s: every check has its own longer cadence
# underneath, and idle burn on a laptop is a real cost.
SENTINEL_INTERVAL = float(os.getenv("SENTINEL_INTERVAL", "60"))
# Absolute free-RAM headroom, NOT a percentage: an allocation fails at an
# absolute number of free bytes regardless of how big the box is. 5 GB total,
# ~2 GB free with Electron + Python + aiohttp up, so 600/300 leaves real warning
# before the OOM killer runs and Restart=always hides the death as a respawn.
SENTINEL_MEM_WARN_MB = float(os.getenv("SENTINEL_MEM_WARN_MB", "600"))
SENTINEL_MEM_BAD_MB = float(os.getenv("SENTINEL_MEM_BAD_MB", "300"))
SENTINEL_DISK_WARN_GB = float(os.getenv("SENTINEL_DISK_WARN_GB", "5"))
SENTINEL_DISK_BAD_GB = float(os.getenv("SENTINEL_DISK_BAD_GB", "1.5"))
SENTINEL_TEMP_WARN_C = float(os.getenv("SENTINEL_TEMP_WARN_C", "82"))
SENTINEL_TEMP_BAD_C = float(os.getenv("SENTINEL_TEMP_BAD_C", "92"))
SENTINEL_SPEND_WARN = float(os.getenv("SENTINEL_SPEND_WARN", "0.8"))  # of BUDGET_MONTHLY_USD
# Google expires refresh tokens after SEVEN days while the OAuth consent screen
# sits in "Testing". Warn at six, so there is a day left to act in.
SENTINEL_GOOGLE_TOKEN_DAYS = float(os.getenv("SENTINEL_GOOGLE_TOKEN_DAYS", "6"))
SENTINEL_APK_STALE_DAYS = float(os.getenv("SENTINEL_APK_STALE_DAYS", "30"))
SENTINEL_UNITS = ("cortana", "cortana-dash", "cortana-bridge", "cortana-spotifyd")
# ONE `systemctl --user is-active` covering all four units, at most this often.
# The throttle IS the feature: this shape of check is what spawned a process 24
# times a minute forever and had to be walked back.
SENTINEL_UNITS_EVERY = float(os.getenv("SENTINEL_UNITS_EVERY", "300"))
SENTINEL_TAILSCALE_EVERY = float(os.getenv("SENTINEL_TAILSCALE_EVERY", "600"))
# How long a bad row stays quiet before it is worth saying again. Nagging every
# minute about a disk that is still full is how a user learns to ignore a voice.
SENTINEL_REALERT = float(os.getenv("SENTINEL_REALERT", "10800"))

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
