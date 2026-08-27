"""Knowledge layer: dictated notes, every past conversation turn, and the
user's local documents, in one sqlite full-text index.

Before this, her memory was a 12-turn window plus a flat key-value list. Nothing
she had ever said and nothing the user had ever written was findable. This module
is the searchable half.

Three deliberate choices, each of which had a tempting alternative:

  * FTS5, not embeddings. The runtime box has ~2 GB free with Electron and
    Python already resident, so a vector index would spend the single scarcest
    resource on the one thing sqlite already does well enough. There is no flag
    for it and no half-open door - if that ever changes it is a rewrite, not a
    toggle.
  * File indexing is incremental by mtime+size and bounded per pass. Re-reading
    ~/Downloads on every tick is precisely the idle burn this repo has already
    been bitten by once. A pass stats a bounded number of candidates, reads far
    fewer, and resumes where the last one stopped.
  * The exclude list is a security control, not tidiness. Indexing .env or a
    private key would make the user's secrets voice-searchable and speakable
    aloud, so anything credential-shaped is refused before it is even opened.

FTS5 is compiled into every sqlite this project has met, but it is an optional
module and a stripped build raises "no such module: fts5" at CREATE time. That
is detected once and degrades to LIKE, because a knowledge layer that is merely
worse is a far better outcome than one that takes the process down at import.
"""
import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import config
import memory


def _cfg(name, default):
    """Read a tunable from config, falling back if that key isn't there yet.

    config.py is owned elsewhere and its NOTES_* block lands in a separate
    commit; hard-importing a name this module cannot guarantee would turn a
    merge-order accident into an ImportError at startup."""
    return getattr(config, name, default)


# -- what is worth indexing -------------------------------------------------
# Directories that are all machine output or all history. node_modules alone is
# the difference between indexing a project and indexing npm.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", "venv", ".venv",
    "env", "virtualenv", "cortana_venv", "site-packages", "dist", "build",
    "target", ".gradle", ".cache", ".npm", ".tox", ".mypy_cache",
    ".pytest_cache", ".next", ".nuxt", "vendor", "Trash", "snap",
}

# Credential shapes. A false positive costs one unsearchable file; a false
# negative makes a private key readable aloud by a voice assistant, so the
# patterns are deliberately greedy - "tokenizer.py" not being searchable is an
# acceptable price for token.json never being.
_DENY_GLOBS = (
    ".env", ".env.*", "*.env", "credentials.json", "token.json", "*token*",
    "*secret*", "*credential*", "*password*", "*apikey*", "*api_key*",
    "*.pem", "*.key", "*.p12",
    "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.gpg", "*.asc", "*.ppk",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".netrc", ".pgpass", ".npmrc",
    ".htpasswd", "*.min.*", "*.lock", "*-lock.json",
)

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".org", ".tex",
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".kt", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".lua",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".r",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv",
    ".html", ".htm", ".css", ".xml",
}
_PDF_SUFFIXES = {".pdf"}


def _size_cap(path):
    """The most bytes a file may occupy on disk and still be worth opening.

    PDFs get a far larger one than text. They are compressed containers - a
    40-page report is routinely 2 MB on disk and 30 KB of words - so judging one
    by its byte size rejects nearly every real PDF and leaves the whole
    pdftotext path dead while reporting nothing but "too big". The -l 40 page
    cap and NOTES_MAX_CHARS are what actually bound the work.
    """
    if Path(path).suffix.lower() in _PDF_SUFFIXES:
        return int(_cfg("NOTES_MAX_PDF_BYTES", 20000000))
    return int(_cfg("NOTES_MAX_FILE_BYTES", 300000))

# Query words that match everything and therefore rank nothing. Dropped from a
# spoken query ("what did I say about the roof") so the real terms decide.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "is",
    "was", "were", "be", "been", "am", "are", "it", "its", "this", "that",
    "what", "when", "where", "who", "why", "how", "did", "do", "does", "i",
    "me", "my", "you", "your", "we", "us", "about", "with", "from", "any",
    "all", "some", "said", "say", "tell", "told", "again", "please",
}

_SRC_NOTE, _SRC_TURN, _SRC_FILE = "note", "turn", "file"
# Per-source rowid namespace - see _rid().
_SRC_CODE = {_SRC_NOTE: 1, _SRC_TURN: 2, _SRC_FILE: 3}
_NO_PDFTOTEXT = "pdf, no pdftotext"

# Module-level caches. _ready is keyed on the database path so a test that
# repoints memory.DB_PATH re-creates its schema instead of inheriting the
# previous file's answer.
_ready = {"db": None}
_FTS5 = None
_pdf_missing_told = False
_degraded_told = False


# -- schema -----------------------------------------------------------------
def _fts5(con):
    """Is the fts5 module compiled into this sqlite? Probed once, in temp, so
    the answer costs nothing and leaves nothing behind."""
    global _FTS5
    if _FTS5 is None:
        try:
            con.execute("CREATE VIRTUAL TABLE temp.notes_fts5_probe USING fts5(x)")
            con.execute("DROP TABLE temp.notes_fts5_probe")
            _FTS5 = True
        except Exception:
            _FTS5 = False
    return _FTS5


def _table():
    """Name of the index table. The two shapes carry identical columns so only
    the SELECT differs between full-text and degraded mode - the writer, the
    delete-by-ref and the row rendering are shared."""
    return "notes_fts" if _FTS5 else "notes_flat"


def _ensure(con):
    # Probe before the early return, not after: _table() reads _FTS5 directly,
    # so a process that finds the schema already built must still know which
    # shape it is looking at.
    _fts5(con)
    key = str(memory.DB_PATH)
    if _ready["db"] == key:
        return
    con.executescript("""
    -- user_notes, not notes. The /local/comms contract already uses "notes"
    -- for phone notifications, and CREATE TABLE IF NOT EXISTS is silent when a
    -- table of that name exists with a different shape - the first sign would
    -- be an INSERT raising "no column named kind" months later, on the box we
    -- cannot reach. Cheaper to never share the name.
    CREATE TABLE IF NOT EXISTS user_notes(
      id INTEGER PRIMARY KEY,
      ts REAL, kind TEXT, title TEXT, body TEXT, tags TEXT, src TEXT);

    -- One row per file ever considered, INCLUDING ones skipped for size. The
    -- skip has to be remembered or a 40 MB export is re-stat'd and re-rejected
    -- on every pass forever.
    CREATE TABLE IF NOT EXISTS notes_docs(
      id INTEGER PRIMARY KEY,
      path TEXT UNIQUE, mtime REAL, size INT, chars INT,
      indexed REAL, gen INT, err TEXT);
    CREATE INDEX IF NOT EXISTS notes_docs_gen ON notes_docs(gen);
    """)
    if _FTS5:
        # porter so "meeting" finds "meetings"; the metadata columns are
        # UNINDEXED so a ref number can never itself be a search hit.
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
                    "body, title, src UNINDEXED, ref UNINDEXED, at UNINDEXED,"
                    " tokenize='porter unicode61')")
    else:
        con.execute("CREATE TABLE IF NOT EXISTS notes_flat("
                    "body TEXT, title TEXT, src TEXT, ref INT, at REAL)")
        con.execute("CREATE INDEX IF NOT EXISTS notes_flat_ref "
                    "ON notes_flat(src, ref)")
    con.commit()
    _ready["db"] = key


def _con():
    con = memory.connect()
    _ensure(con)
    return con


def _rid(src, ref):
    """One stable rowid per (source, reference).

    Not cosmetic. `src` and `ref` are UNINDEXED fts5 columns, so
    `DELETE ... WHERE src=? AND ref=?` is a SCAN of the entire index - measured
    here as a full decode of every stored row, per write. Re-indexing one edited
    file, or catching up a backlog of conversation turns inside recall(), would
    pay that once per row. Deriving the rowid instead makes the delete and the
    replace an O(log n) seek. Sources get separate multiples of 8 because their
    ref spaces (user_notes.id, log.rowid, notes_docs.id) all start at 1.
    """
    return int(ref) * 8 + _SRC_CODE[src]


def _put(con, src, ref, title, body, at):
    t = _table()
    rid = _rid(src, ref)
    con.execute("DELETE FROM " + t + " WHERE rowid=?", (rid,))
    con.execute("INSERT INTO " + t + "(rowid, body, title, src, ref, at)"
                " VALUES(?,?,?,?,?,?)",
                (rid, body, title or "", src, int(ref), float(at or 0)))


def _drop(con, src, ref):
    con.execute("DELETE FROM " + _table() + " WHERE rowid=?", (_rid(src, ref),))


# -- notes the user dictates ------------------------------------------------
def add(text, title="", tags="", kind="note"):
    """Handle the `note` tool. Returns one spoken-style line."""
    body = (text or "").strip()
    if not body:
        return "A note needs something in it."
    kind = (kind or "note").strip().lower() or "note"
    title = (title or "").strip()[:200] or _title_from(body)
    ts = time.time()
    con = _con()
    cur = con.execute(
        "INSERT INTO user_notes(ts, kind, title, body, tags, src) VALUES(?,?,?,?,?,?)",
        (ts, kind, title, body, (tags or "").strip()[:200], "voice"))
    nid = cur.lastrowid
    _put(con, _SRC_NOTE, nid, title, body, ts)
    con.commit()
    con.close()
    return f"Noted: {title}"


def _title_from(body):
    first = re.sub(r"\s+", " ", body.strip().split("\n")[0]).strip()
    return (first[:57] + "...") if len(first) > 60 else first


# -- the existing conversation log ------------------------------------------
def sync_log(limit=1000):
    """Index conversation turns written since the last call.

    The cursor is a meta row, not kv: recall_all() dumps every kv row into the
    system prompt verbatim on every single turn, so a bookkeeping integer
    parked there would be read to the model forever.
    """
    con = _con()
    start = int(memory.meta_get("notes_log_rowid", "0") or 0)
    rows = con.execute(
        "SELECT rowid, ts, text FROM log WHERE rowid>? ORDER BY rowid LIMIT ?",
        (start, int(limit))).fetchall()
    cap = int(_cfg("NOTES_MAX_CHARS", 40000))
    last, n = start, 0
    for rid, ts, text in rows:
        last = rid
        body = (text or "").strip()
        if len(body) < 4:
            continue           # "ok", "yes" - nothing anyone will ever search for
        _put(con, _SRC_TURN, rid, "", body[:cap], ts or 0)
        n += 1
    con.commit()
    con.close()
    if last != start:
        memory.meta_set("notes_log_rowid", last)
    return n


# -- local documents --------------------------------------------------------
def _roots():
    override = _cfg("NOTES_ROOTS", None)
    if override:
        cand = [Path(str(p)).expanduser() for p in override]
    else:
        home = Path.home()
        cand = [Path(_cfg("WORKSPACE", home / "workspace")),
                home / "Documents", home / "Downloads"]
    seen, out = set(), []
    for p in cand:
        try:
            key = str(p.resolve())
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def excluded(path, root=None):
    """Why this path must not be indexed, or '' if it may be. Public because it
    is the security-relevant half of this module and deserves its own tests.

    Directory rules are applied only BELOW `root`. A user whose documents live
    under, say, ~/.local/share/notes has a dot-directory in every single path,
    and judging the root itself would empty the index without one error line to
    explain it - the silent kind of failure this repo keeps getting bitten by.
    """
    p = Path(path)
    below = p.parts[:-1]
    if root is not None:
        try:
            below = p.relative_to(root).parts[:-1]
        except ValueError:
            pass
    lowered = {d.lower() for d in _SKIP_DIRS}
    for part in below:
        low = part.lower()
        if low in lowered:
            return f"in {part}"
        # Every dot-directory at once: .git, .ssh, .aws, .config, .mozilla.
        # Cheaper to state as a rule than to enumerate, and nothing worth
        # indexing lives in a hidden directory.
        if low.startswith(".") and low not in (".", ".."):
            return f"in {part}"
    name = p.name.lower()
    if name.startswith("."):
        return "hidden file"
    for glob in _DENY_GLOBS:
        if fnmatch.fnmatch(name, glob):
            return "looks like a credential or generated file"
    suffix = p.suffix.lower()
    if suffix in _PDF_SUFFIXES:
        return "" if shutil.which("pdftotext") else _NO_PDFTOTEXT
    if suffix not in _TEXT_SUFFIXES:
        return "not a text type"
    return ""


def _candidates(roots):
    """Every indexable-looking file under `roots`, in a stable order.

    Yielded as (root, path) so exclusion can be judged relative to the root.
    Sorted at both levels so the resume cursor below means the same thing from
    one pass to the next. Symlinks are not followed - a link back up the tree
    turns a walk into a hang.
    """
    for root in roots:
        try:
            if not Path(root).is_dir():
                continue
        except OSError:
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                yield root, os.path.join(dirpath, fn)


def _extract(path):
    """File text, or None if this file should not be indexed after all.

    None and "" are different answers: "" is an empty file we successfully read,
    None means we could not or should not read it.
    """
    p = Path(path)
    cap = int(_cfg("NOTES_MAX_CHARS", 40000))
    if p.suffix.lower() in _PDF_SUFFIXES:
        exe = shutil.which("pdftotext")
        if not exe:
            _pdf_unavailable()
            return None
        try:
            out = subprocess.run([exe, "-q", "-l", "40", str(p), "-"],
                                 capture_output=True, timeout=20)
            if out.returncode != 0:
                return None
            return out.stdout.decode("utf-8", "replace")[:cap]
        except Exception:
            return None
    try:
        raw = p.read_bytes()[: _size_cap(p) + 1]
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None            # a .json that is really a sqlite file, etc.
    return raw.decode("utf-8", "replace")[:cap]


def _remember_doc(con, path, st, gen, chars, err):
    con.execute(
        "INSERT INTO notes_docs(path, mtime, size, chars, indexed, gen, err)"
        " VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size,"
        " chars=excluded.chars, indexed=excluded.indexed, gen=excluded.gen,"
        " err=excluded.err",
        (str(path), st.st_mtime, st.st_size, chars, time.time(), gen, err))
    row = con.execute("SELECT id FROM notes_docs WHERE path=?", (str(path),)).fetchone()
    return row[0]


def _pdf_unavailable():
    """Say once that PDFs are not searchable here, then never again.

    poppler-utils is an apt package, not a pip one - adding a PDF library to
    requirements.txt to paper over a missing system binary is how a "no heavy
    dependencies" rule dies. Printing per file would flood the journal with one
    line per PDF per lap, forever.
    """
    global _pdf_missing_told
    if not _pdf_missing_told:
        _pdf_missing_told = True
        print("[notes] pdftotext not installed - PDFs are not searchable "
              "(sudo apt install poppler-utils)")


def _gone(path):
    """True only if this file is genuinely absent.

    A permission error, a stalled mount or an unreadable parent directory must
    never be read as a deletion. os.walk() swallows exactly those errors and
    yields nothing, so without this distinction one unreadable ~/Documents would
    complete a "full" lap and silently reap every document under it.
    """
    try:
        os.stat(path)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _under(path, roots):
    """Is this indexed path below one of the roots actually walked this lap?"""
    q = os.path.normcase(os.path.abspath(str(path)))
    return any(q == r or q.startswith(r + os.sep) for r in roots)


def _flush_touch(con, touch, gen):
    """Stamp this generation onto every file the pass found unchanged, in one
    statement. Batched because it is otherwise the most common write in the
    module and the one with the least to say."""
    if not touch:
        return
    con.execute("UPDATE notes_docs SET gen=? WHERE id IN (%s)"
                % ",".join("?" * len(touch)), [gen] + list(touch))
    del touch[:]
    con.commit()


def _index_one(con, path, gen, stats, root=None, touch=None):
    why = excluded(path, root)
    if why:
        if why == _NO_PDFTOTEXT:
            _pdf_unavailable()
        stats["excluded"] += 1
        return
    try:
        st = os.stat(path)
    except OSError:
        return
    row = con.execute("SELECT id, mtime, size FROM notes_docs WHERE path=?",
                      (str(path),)).fetchone()
    if row and int(row[2]) == st.st_size and abs(float(row[1]) - st.st_mtime) < 0.002:
        # THE incremental case, and the only one that matters for idle burn:
        # one stat, no open() and no tokenizing. The gen bump is deferred to
        # _flush_touch rather than written here, because a write statement
        # opens a transaction that would then stay open across the NEXT file's
        # read - see the commit in index_pass().
        if touch is None:
            con.execute("UPDATE notes_docs SET gen=? WHERE id=?", (gen, row[0]))
        else:
            touch.append(row[0])
        stats["unchanged"] += 1
        return
    if st.st_size > _size_cap(path):
        did = _remember_doc(con, path, st, gen, 0, "too big")
        _drop(con, _SRC_FILE, did)     # it may have been small enough yesterday
        stats["too_big"] += 1
        return
    text = _extract(path)
    stats["read"] += 1
    did = _remember_doc(con, path, st, gen,
                        0 if text is None else len(text),
                        "unreadable" if text is None else "")
    if text is None or not text.strip():
        _drop(con, _SRC_FILE, did)
        return
    _put(con, _SRC_FILE, did, str(path), text, st.st_mtime)
    stats["indexed"] += 1


def index_pass(roots=None):
    """One bounded slice of the file walk. Safe to call on a slow timer.

    Three budgets, because they fail differently: files examined caps the stat
    syscalls, files read caps the disk and CPU, and the deadline caps a pass
    that has wandered onto a network mount. Whichever trips first ends the pass,
    and the cursor remembers where to resume - so no tick ever walks Downloads
    from the top and reads it end to end.
    """
    stats = {"examined": 0, "read": 0, "indexed": 0, "unchanged": 0,
             "excluded": 0, "too_big": 0, "removed": 0, "wrapped": False}
    roots = roots if roots is not None else _roots()
    max_files = int(_cfg("NOTES_PASS_FILES", 400))
    max_reads = int(_cfg("NOTES_PASS_READS", 25))
    deadline = time.monotonic() + float(_cfg("NOTES_PASS_SECONDS", 5.0))
    skip_to = int(memory.meta_get("notes_walk_pos", "0") or 0)
    gen = int(memory.meta_get("notes_gen", "1") or 1)

    # Which roots are actually reachable right now. Recorded before the walk
    # because it decides whether the reap below is allowed to believe a file is
    # missing: an external drive or a network mount that is simply not there
    # must cost coverage, never the index.
    live = []
    for r in roots:
        try:
            if Path(r).is_dir():
                live.append(os.path.normcase(os.path.abspath(str(r))))
        except OSError:
            pass

    con = _con()
    touch = []
    pos, wrapped = 0, True
    for root, path in _candidates(roots):
        pos += 1
        if pos <= skip_to:
            continue
        # The deadline may not end a pass that has examined nothing. Reaching a
        # far-along cursor is itself part of the walk, and on a big tree that
        # skip can outlast the budget on its own - at which point the pass
        # writes back the cursor it started with and stalls there for good,
        # burning the full budget every tick and indexing not one file. The
        # file and read budgets still bound the work that follows.
        if (stats["examined"] >= max_files or stats["read"] >= max_reads
                or (stats["examined"] and time.monotonic() > deadline)):
            wrapped = False
            pos -= 1           # this one was never examined; resume ON it
            break
        stats["examined"] += 1
        did_io = (stats["read"], stats["too_big"])
        try:
            _index_one(con, path, gen, stats, root, touch)
        except Exception as e:
            print(f"[notes] {path}: {e}")
        if (stats["read"], stats["too_big"]) != did_io:
            # Commit per file that actually touched disk. One transaction for
            # the whole pass would be held across the next file's read - and
            # across a 20 second pdftotext call - while schedule.py ticks this
            # same database every five seconds and log_turn() writes it on every
            # utterance. Both have a 5s busy timeout and then raise "database is
            # locked", which is a crashed voice turn, not a slow one.
            con.commit()
        if len(touch) >= 200:
            _flush_touch(con, touch, gen)
    _flush_touch(con, touch, gen)

    if wrapped and live:
        # A full lap finished, so every file still on disk under a reachable
        # root carries this generation. Anything left behind is a candidate for
        # removal - but only a candidate: os.walk() reports no error when it
        # cannot read a directory, so "the walk did not yield it" and "it is
        # deleted" are different claims and only the second one justifies
        # dropping a document.
        stale = con.execute("SELECT id, path FROM notes_docs WHERE gen<?",
                            (gen,)).fetchall()
        for n, (did, dpath) in enumerate(stale):
            if not _under(dpath, live):
                continue       # its root was not walked this lap
            if not _gone(dpath):
                con.execute("UPDATE notes_docs SET gen=? WHERE id=?", (gen, did))
            else:
                _drop(con, _SRC_FILE, did)
                con.execute("DELETE FROM notes_docs WHERE id=?", (did,))
                stats["removed"] += 1
            if n % 200 == 199:
                con.commit()   # same lock discipline as the walk above
    stats["wrapped"] = wrapped
    con.commit()
    con.close()

    memory.meta_set("notes_walk_pos", 0 if wrapped else pos)
    if wrapped:
        memory.meta_set("notes_gen", gen + 1)
    return stats


def tick():
    """One background pass: catch up on conversation turns, then a slice of the
    file walk. Called from a slow loop in main.py - see NOTES_TICK."""
    turns = 0
    try:
        turns = sync_log()
    except Exception as e:
        print("[notes] log sync failed:", e)
    try:
        stats = index_pass()
    except Exception as e:
        print("[notes] index pass failed:", e)
        # Same keys as a real pass. A caller in the tick loop that reads
        # stats["examined"] must not meet a KeyError on the one path that
        # already means something went wrong - that turns a logged failure
        # into a dead loop.
        stats = dict.fromkeys(
            ("examined", "read", "indexed", "unchanged", "excluded",
             "too_big", "removed"), 0)
        stats["wrapped"] = False
    return dict(stats, turns=turns)


# -- search -----------------------------------------------------------------
def _terms(query):
    words = [w.lower() for w in re.findall(r"\w+", str(query or ""), re.UNICODE)]
    words = [w for w in words if len(w) > 1 or w.isdigit()]
    kept = [w for w in words if w not in _STOPWORDS]
    # "what is it about" is all stopwords; searching for common words beats
    # searching for nothing.
    return (kept or words)[:8]


_URL = re.compile(r"(?<![\w.])(?:https?://|ftp://|www\.)\S+", re.I)


def _speakable(text):
    """Strip what must never reach a speaker: URLs, table pipes, markdown.

    The house rule is that spoken output is prose, and every other module can
    honour that where it writes its sentence. This one cannot: the quote is
    lifted verbatim out of a README or a dictated note, so the only place the
    rule can be enforced is here, where borrowed text enters the sentence.
    A URL read aloud character by character is the worst of it.
    """
    flat = _URL.sub("a link", text or "")
    flat = re.sub(r"[`*_#>|~\[\]]+", " ", flat)
    return re.sub(r"\s+", " ", flat).strip()


def _around(body, terms, width=180):
    """A quote centred on the first term that actually appears.

    Hand-rolled rather than fts5's snippet() so the degraded LIKE path produces
    identical output - one renderer, one thing to get right.
    """
    flat = _speakable(body)
    flat = re.sub(r"^[\s\-+]+", "", flat)
    low = flat.lower()
    at = -1
    for t in terms:
        at = low.find(t)
        if at >= 0:
            break
    if at < 0:
        return flat[:width] + ("..." if len(flat) > width else "")
    start = max(0, at - width // 3)
    if start:                              # do not start mid-word
        space = flat.find(" ", start)
        start = space + 1 if 0 <= space < start + 20 else start
    end = min(len(flat), start + width)
    quote = flat[start:end].strip()
    return ("..." if start else "") + quote + ("..." if end < len(flat) else "")


def search(query, limit=3):
    """Ranked hits across notes, conversation turns and files.

    Returns dicts, not prose - recall() does the speaking, and the dashboard or
    a future tool can use the same rows without re-parsing a sentence.
    """
    terms = _terms(query)
    if not terms:
        return []
    con = _con()
    t = _table()
    rows = []
    if _FTS5:
        sql = ("SELECT body, title, src, ref, at FROM " + t +
               " WHERE " + t + " MATCH ? ORDER BY bm25(" + t + ", 1.0, 4.0) LIMIT ?")
        # AND first for precision, OR only if that finds nothing. Prefix match
        # because speech-to-text drops plurals and word endings constantly.
        for joiner in (" AND ", " OR "):
            expr = joiner.join('"' + w + '"*' for w in terms)
            try:
                rows = con.execute(sql, (expr, int(limit))).fetchall()
            except Exception as e:
                print("[notes] fts query failed:", e)
                rows = []
            if rows:
                break
    else:
        where = " AND ".join(["(body LIKE ? OR title LIKE ?)"] * len(terms))
        params = []
        for w in terms:
            params += ["%" + w + "%", "%" + w + "%"]
        params.append(int(limit))
        rows = con.execute(
            "SELECT body, title, src, ref, at FROM " + t + " WHERE " + where +
            " ORDER BY at DESC LIMIT ?", params).fetchall()
    out = []
    for body, title, src, ref, at in rows:
        out.append({"src": src, "ref": ref, "title": title, "at": at or 0,
                    "quote": _around(body, terms),
                    "role": _role_of(con, ref) if src == _SRC_TURN else ""})
    con.close()
    return out


def _role_of(con, rowid):
    row = con.execute("SELECT role FROM log WHERE rowid=?", (int(rowid),)).fetchone()
    return row[0] if row else ""


def _ago(ts):
    """Past-tense time phrasing for speech. Prose, never an ISO string."""
    import datetime
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "some time ago"
    if ts <= 0:
        return "some time ago"
    when = datetime.datetime.fromtimestamp(ts)
    days = (datetime.date.today() - when.date()).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"on {when:%A}"
    # %-d is a glibc extension that raises on Windows, where half of this
    # repo's tests run - build the date by hand instead.
    if days < 300:
        return f"on {when.day} {when:%B}"
    return f"in {when:%B %Y}"


def recall(query, limit=3):
    """Handle the `recall` tool. Spoken prose with a couple of quotes.

    Deliberately not a table and not a list of paths: this is read aloud, and
    three sentences a person can follow beat ten rows nobody can hear.
    """
    global _degraded_told
    if not str(query or "").strip():
        return "Recall needs something to look for."
    try:
        sync_log()     # a turn from thirty seconds ago must be findable now,
                       # not after the next background tick
    except Exception as e:
        print("[notes] log sync failed:", e)

    hits = search(query, limit=int(limit or 3))
    if not hits:
        return "Nothing in your notes, our conversations, or your files matches that."

    parts = []
    for h in hits:
        when = _ago(h["at"])
        if h["src"] == _SRC_NOTE:
            lead = f"Your note from {when}"
        elif h["src"] == _SRC_TURN:
            lead = ("I said " + when) if h.get("role") == "assistant" else ("You said " + when)
        else:
            lead = "In " + Path(h["title"]).name
        parts.append(f'{lead}: "{h["quote"]}".')
    answer = " ".join(parts)

    if not _FTS5 and not _degraded_told:
        _degraded_told = True
        answer += (" One caveat: this machine's sqlite has no full text index, "
                   "so search is running degraded and may miss things.")
    return answer


def status():
    """One line for a diagnostic or the dashboard."""
    con = _con()
    n_notes = con.execute("SELECT COUNT(*) FROM user_notes").fetchone()[0]
    n_docs = con.execute("SELECT COUNT(*) FROM notes_docs WHERE err=''").fetchone()[0]
    n_rows = con.execute("SELECT COUNT(*) FROM " + _table()).fetchone()[0]
    con.close()
    pdf = "pdftotext present" if shutil.which("pdftotext") else "no pdftotext, PDFs skipped"
    mode = "FTS5" if _FTS5 else "degraded (no FTS5, LIKE search)"
    return (f"{n_notes} notes, {n_docs} files, {n_rows} indexed entries. "
            f"Search: {mode}. {pdf}.")
