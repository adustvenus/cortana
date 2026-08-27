"""Persistent memory: conversation log, key-value memory, usage/cost tracking."""
import sqlite3
import time
import datetime
from config import DB_PATH, CORTANA_MD

DEFAULT_MD = """# CORTANA - Persistent Context

## Identity
You are Cortana, a personal AI assistant. Voice-first. You are the caliber of a
billionaire's executive assistant: fast, discreet, confident, zero fluff.

## User preferences
- Terse replies. Spoken aloud, so: short sentences, no markdown, no lists read out.
- Give the answer first. One critical caveat max, at the end, only if it matters.
- Confident recommendations, not hedged essays.

## Standing rules
- NEVER execute trades. Recommendations only.
- Gmail: drafts only. Never send.
- Full autonomy inside the workspace folder. Just do it, then report done.

## Future expansion hooks (do not build unless asked)
- Real-time market data provider (see tools/trading.py DataProvider)
- UI / mobile app development via the 'dev' agent
- Wake-word learning / knowing when user is addressing Cortana vs others
"""


def _c():
    # timeout: background task threads write concurrently with the main loop;
    # a brief writer overlap should wait, not raise "database is locked".
    return sqlite3.connect(DB_PATH, timeout=5)


def connect():
    """Connection factory for modules that own their own tables (schedule.py).
    Same timeout rationale as _c(); public so those modules don't reach for a
    private name or hand-roll a second connect with different settings."""
    return _c()


def init():
    con = _c()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS log(ts REAL, role TEXT, text TEXT);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS usage(ts REAL, model TEXT, tin INT, tout INT, cost REAL);
    CREATE TABLE IF NOT EXISTS address_log(ts REAL, text TEXT, decision TEXT);
    CREATE TABLE IF NOT EXISTS tasks(id INT, ts REAL, agent TEXT, description TEXT,
                                     status TEXT, result TEXT);

    -- Internal bookkeeping. Deliberately NOT the kv table: recall_all() dumps
    -- every kv row into the system prompt as "Saved memory", so a cursor like
    -- inbox_seq stored there would be read aloud to the model every turn.
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

    -- Proactive spine (scheduler + routines). Rows are never DELETEd - state
    -- goes to 'cancelled' - so an INTEGER PRIMARY KEY can never be reused for
    -- an id the user just heard spoken aloud.
    CREATE TABLE IF NOT EXISTS schedules(
      id INTEGER PRIMARY KEY,
      created REAL, kind TEXT, title TEXT,
      action TEXT, payload TEXT,       -- say|turn|delegate  +  JSON args
      rrule TEXT, tz TEXT,             -- '' rrule = one-shot; tz is an IANA name
      next_ts REAL,                    -- epoch of next occurrence; NULL = exhausted
      state TEXT,                      -- pending|firing|delivered|acked|missed|done|cancelled
      urgency TEXT,                    -- ambient|normal|urgent|critical
      catchup INT,                     -- grace seconds on a missed window; 0 = drop
      require_ack INT, nag_after INT, nag_count INT, nag_max INT,
      fired_ts REAL, ack_ts REAL, owner TEXT, last_error TEXT);
    CREATE INDEX IF NOT EXISTS schedules_due ON schedules(state, next_ts);

    CREATE TABLE IF NOT EXISTS routines(
      id INTEGER PRIMARY KEY,
      created REAL, name TEXT UNIQUE,
      trigger TEXT,                    -- calendar|health|presence (clock lives in schedules)
      cond TEXT,                       -- JSON predicate, shape per trigger
      action TEXT, payload TEXT, urgency TEXT,
      enabled INT,
      edge TEXT,                       -- last evaluated value; fires on CHANGE only
      last_fired REAL, min_gap INT, fires INT);

    CREATE TABLE IF NOT EXISTS deliveries(
      ts REAL, src TEXT, ref INT, urgency TEXT,
      surfaces TEXT, presence TEXT, text TEXT);
    """)
    # Two processes now write this file on a timer. The default rollback journal
    # takes a whole-database exclusive lock per write, which turns the 5s
    # scheduler tick into routine contention instead of the occasional overlap
    # _c()'s timeout was sized for. Persistent on the file, not per-connection -
    # hence state.db-wal / state.db-shm in .gitignore.
    con.execute("PRAGMA journal_mode=WAL")
    con.commit()
    con.close()
    if not CORTANA_MD.exists():
        CORTANA_MD.write_text(DEFAULT_MD)


def meta_get(k, default=""):
    con = _c()
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    con.close()
    return row[0] if row else default


def meta_set(k, v):
    con = _c()
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    con.commit()
    con.close()


def log_turn(role, text):
    con = _c()
    con.execute("INSERT INTO log VALUES(?,?,?)", (time.time(), role, text))
    con.commit()
    con.close()


def recent_messages(n=12):
    """Last n turns as Anthropic messages (starts with user, roles alternate)."""
    con = _c()
    rows = con.execute(
        "SELECT role, text FROM (SELECT ts, role, text FROM log ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC",
        (n,),
    ).fetchall()
    con.close()
    msgs = [{"role": r, "content": t} for r, t in rows]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    out = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n" + m["content"]
        else:
            out.append(dict(m))
    return out


def recent_text(n=6):
    con = _c()
    rows = con.execute(
        "SELECT role, text FROM (SELECT ts, role, text FROM log ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC",
        (n,),
    ).fetchall()
    con.close()
    return "\n".join(f"{r}: {t[:200]}" for r, t in rows)


def remember(key, value):
    con = _c()
    con.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, value))
    con.commit()
    con.close()
    return f"Remembered {key}."


def recall_all():
    con = _c()
    rows = con.execute("SELECT k, v FROM kv").fetchall()
    con.close()
    return "\n".join(f"- {k}: {v}" for k, v in rows) or "(nothing saved yet)"


def add_usage(model, tin, tout, cost):
    con = _c()
    con.execute("INSERT INTO usage VALUES(?,?,?,?,?)", (time.time(), model, tin, tout, cost))
    con.commit()
    con.close()


def month_spend():
    start = datetime.datetime.now().replace(day=1, hour=0, minute=0, second=0).timestamp()
    con = _c()
    (total,) = con.execute("SELECT COALESCE(SUM(cost),0) FROM usage WHERE ts>=?", (start,)).fetchone()
    con.close()
    return float(total)


def log_task(tid, agent, description, status, result):
    """Background-task audit trail (one row per state change)."""
    con = _c()
    con.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)",
                (tid, time.time(), agent, description[:500], status, result))
    con.commit()
    con.close()


def log_address_decision(text, decision):
    """Future learning hook: training data for 'was user talking to Cortana?'"""
    con = _c()
    con.execute("INSERT INTO address_log VALUES(?,?,?)", (time.time(), text, decision))
    con.commit()
    con.close()
