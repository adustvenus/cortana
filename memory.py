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
    return sqlite3.connect(DB_PATH)


def init():
    con = _c()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS log(ts REAL, role TEXT, text TEXT);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS usage(ts REAL, model TEXT, tin INT, tout INT, cost REAL);
    CREATE TABLE IF NOT EXISTS address_log(ts REAL, text TEXT, decision TEXT);
    """)
    con.commit()
    con.close()
    if not CORTANA_MD.exists():
        CORTANA_MD.write_text(DEFAULT_MD)


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


def log_address_decision(text, decision):
    """Future learning hook: training data for 'was user talking to Cortana?'"""
    con = _c()
    con.execute("INSERT INTO address_log VALUES(?,?,?)", (time.time(), text, decision))
    con.commit()
    con.close()
