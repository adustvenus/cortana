"""Knowledge-layer tests.

Every test here is one of the four ways this module can fail without anyone
noticing:

  * it quietly indexes a secret and then reads it aloud,
  * it re-reads the whole of ~/Downloads on every tick and cooks the laptop,
  * it explodes at import on a sqlite with no FTS5, taking the process with it,
  * or it just cannot find the thing the user asked for.

None of those raise anywhere near the code that caused them, so they are pinned
here rather than discovered on the runtime box.
"""
import time

import pytest

import memory
from tools import notes


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Throwaway database plus a clean module cache.

    notes caches which database it has built (_ready) and whether this sqlite
    has FTS5 (_FTS5) at module level. Both must be reset per test or the second
    test in a file inherits the first one's answers and passes for the wrong
    reason."""
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(notes, "_ready", {"db": None})
    monkeypatch.setattr(notes, "_FTS5", None)
    monkeypatch.setattr(notes, "_degraded_told", False)
    memory.init()
    return tmp_path


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake home to index, with no NOTES_* config needed."""
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setattr(notes, "_roots", lambda: [root])
    return root


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# -- incremental indexing ---------------------------------------------------
def test_an_unchanged_file_is_never_read_twice(db, tree, monkeypatch):
    """The whole idle-burn defence. If mtime+size skipping breaks, every tick
    reads every file forever and nothing about the symptom points here."""
    _write(tree, "plan.md", "the greenhouse roof needs new glass")
    reads = []
    real = notes._extract
    monkeypatch.setattr(notes, "_extract", lambda p: reads.append(p) or real(p))

    first = notes.index_pass()
    assert first["indexed"] == 1 and len(reads) == 1

    second = notes.index_pass()
    assert second["read"] == 0, second
    assert second["unchanged"] == 1, second
    assert len(reads) == 1, "an unchanged file was opened again"


def test_an_edited_file_is_re_read_and_re_indexed(db, tree):
    """The other half of the same guard: skipping must be by mtime+size, not by
    'have I seen this path before'."""
    p = _write(tree, "plan.md", "the greenhouse roof needs new glass")
    notes.index_pass()
    assert notes.search("greenhouse")

    time.sleep(0.01)
    p.write_text("the greenhouse roof is fixed, the fence is next", encoding="utf-8")
    stats = notes.index_pass()
    assert stats["read"] == 1, stats
    assert notes.search("fence"), "an edited file kept its old contents"


def test_a_deleted_file_leaves_the_index(db, tree):
    p = _write(tree, "gone.md", "unobtainium delivery on Thursday")
    notes.index_pass()
    assert notes.search("unobtainium")
    p.unlink()
    notes.index_pass()
    assert notes.search("unobtainium") == [], "deleted file still searchable"


def test_a_pass_is_bounded_and_resumes_instead_of_restarting(db, tree, monkeypatch):
    """A tick must not walk the whole tree. It must also not walk the same first
    N files every time, which is the bug that 'bounded' invites."""
    for i in range(12):
        _write(tree, f"f{i:02d}.md", f"paragraph number {i} about sailing")
    monkeypatch.setattr(notes, "_cfg",
                        lambda n, d: 4 if n in ("NOTES_PASS_FILES", "NOTES_PASS_READS") else d)

    first = notes.index_pass()
    assert first["examined"] == 4 and first["wrapped"] is False
    assert int(memory.meta_get("notes_walk_pos", "0")) == 4

    notes.index_pass()
    assert int(memory.meta_get("notes_walk_pos", "0")) == 8, "the pass restarted at the top"


def test_a_completed_lap_resets_the_cursor(db, tree):
    _write(tree, "one.md", "alpha")
    stats = notes.index_pass()
    assert stats["wrapped"] is True
    assert int(memory.meta_get("notes_walk_pos", "0")) == 0


# -- the exclude list, which is a security control --------------------------
def test_a_dotenv_is_never_indexed(db, tree):
    """Indexing .env makes API keys voice-searchable and speakable aloud. This
    is the single most damaging thing this module could do."""
    _write(tree, ".env", "ANTHROPIC_API_KEY=sk-ant-notarealkey-hunter2")
    _write(tree, "notes.md", "hunter2 is a terrible password")
    notes.index_pass()

    hits = notes.search("hunter2", limit=5)
    assert hits, "sanity: the harmless file should be found"
    assert all(h["src"] != "file" or ".env" not in h["title"] for h in hits)
    assert notes.excluded(str(tree / ".env"))


def test_credential_shaped_names_are_refused(db, tree):
    for name in ("token.json", "credentials.json", "server.pem", "deploy.key",
                 "id_rsa", "my_secret_stuff.txt", "app.min.js"):
        assert notes.excluded(str(tree / name)), name


def test_node_modules_is_excluded_by_path_not_by_name(db, tree):
    """The file itself looks perfectly indexable - index.js. Only its directory
    says otherwise, so the check has to run over the whole path."""
    p = _write(tree, "proj/node_modules/left-pad/index.js", "module.exports = x")
    assert notes.excluded(str(p))
    notes.index_pass()
    assert notes.search("left pad exports") == []


def test_noise_directories_are_skipped_wholesale(db, tree):
    for rel in ("proj/.git/config", "proj/__pycache__/x.py", "proj/venv/lib/y.py",
                "proj/dist/bundle.js", "proj/build/out.js", "proj/.ssh/known_hosts"):
        assert notes.excluded(str(tree / rel)), rel


def test_a_root_inside_a_dot_directory_still_indexes(db, tmp_path, monkeypatch):
    """Judging the root's own path components would empty the index for anyone
    whose documents live under ~/.local or similar - and it would do it in
    total silence, with every file 'excluded' and no error anywhere."""
    root = tmp_path / ".local" / "share" / "papers"
    root.mkdir(parents=True)
    (root / "thesis.md").write_text("the manticore hypothesis", encoding="utf-8")
    monkeypatch.setattr(notes, "_roots", lambda: [root])

    stats = notes.index_pass()
    assert stats["indexed"] == 1, stats
    assert notes.search("manticore")


def test_ordinary_project_files_are_not_excluded(db, tree):
    """The exclude list is greedy on purpose; this is the guard that it did not
    become 'exclude everything'."""
    for rel in ("proj/README.md", "proj/main.py", "diary/2026-08-24.txt",
                "proj/data/sales.csv"):
        assert notes.excluded(str(tree / rel)) == "", rel


# -- size cap ---------------------------------------------------------------
def test_a_huge_file_is_skipped_and_not_re_read(db, tree, monkeypatch):
    """A 40 MB log or database export must not be tokenised into the index, and
    it must be remembered as skipped or every pass re-rejects it forever."""
    monkeypatch.setattr(notes, "_cfg",
                        lambda n, d: 500 if n == "NOTES_MAX_FILE_BYTES" else d)
    _write(tree, "huge.txt", "leviathan " * 400)
    _write(tree, "small.txt", "leviathan in the small one")

    stats = notes.index_pass()
    assert stats["too_big"] == 1, stats
    hits = notes.search("leviathan", limit=5)
    assert hits and all("huge.txt" not in (h["title"] or "") for h in hits)

    again = notes.index_pass()
    assert again["read"] == 0 and again["too_big"] == 0, again


def test_a_binary_file_with_a_text_extension_is_dropped(db, tree):
    (tree / "sneaky.json").write_bytes(b"SQLite format 3\x00\x00\x00 kraken")
    notes.index_pass()
    assert notes.search("kraken") == []


# -- PDFs -------------------------------------------------------------------
def test_pdfs_are_skipped_silently_when_pdftotext_is_missing(db, tree, monkeypatch):
    """No pip dependency is allowed for this, so a box without poppler-utils
    simply has no PDF search - it must not error and must not retry loudly."""
    monkeypatch.setattr(notes.shutil, "which", lambda name: None)
    _write(tree, "manual.pdf", "%PDF-1.4 whatever")
    stats = notes.index_pass()
    assert stats["excluded"] == 1, stats
    assert "no pdftotext" in notes.status()


def test_the_missing_pdftotext_notice_is_printed_once(db, tree, monkeypatch, capsys):
    """One line per PDF per lap, forever, is how a journal becomes unreadable -
    and this walk laps for as long as the machine is up."""
    monkeypatch.setattr(notes.shutil, "which", lambda name: None)
    monkeypatch.setattr(notes, "_pdf_missing_told", False)
    for i in range(3):
        _write(tree, f"doc{i}.pdf", "%PDF-1.4")
    notes.index_pass()
    notes.index_pass()
    assert capsys.readouterr().out.count("pdftotext") == 1


# -- search across all three sources ----------------------------------------
def test_search_finds_a_note_and_a_conversation_turn(db, tree):
    """The point of the whole module: what she said months ago and what the user
    dictated are both findable, in one query."""
    notes.add("the boat is called Perihelion and berths at slip 14")
    memory.log_turn("user", "remind me the boat is named Perihelion")
    memory.log_turn("assistant", "Perihelion, slip fourteen. Noted.")
    _write(tree, "marina.txt", "Perihelion mooring fees are due in March")
    notes.index_pass()
    notes.sync_log()

    hits = notes.search("Perihelion", limit=6)
    kinds = {h["src"] for h in hits}
    assert "note" in kinds, hits
    assert "turn" in kinds, hits
    assert "file" in kinds, hits


def test_recall_speaks_prose_with_a_quote_not_a_table(db, tree):
    notes.add("the greenhouse roof needs new glass before winter")
    answer = notes.recall("greenhouse roof")
    assert "greenhouse" in answer.lower()
    assert '"' in answer, "no quote to read back"
    for bad in ("|", "\n-", "* ", "```", "http"):
        assert bad not in answer, f"{bad!r} would be read aloud"


def test_recall_indexes_the_turn_you_just_had(db):
    """A user who says something and immediately asks about it must not have to
    wait for the background tick."""
    memory.log_turn("user", "the accountant's name is Priya Raman")
    answer = notes.recall("accountant name")
    assert "Priya" in answer


def test_recall_says_so_when_nothing_matches(db):
    answer = notes.recall("quokka insurance")
    assert "nothing" in answer.lower()
    assert '"' not in answer


def test_recall_attributes_her_own_words_to_her(db):
    memory.log_turn("assistant", "The zeppelin lands at eleven on Tuesday.")
    answer = notes.recall("zeppelin")
    assert answer.startswith("I said"), answer


def test_stopword_only_queries_still_search_something(db):
    notes.add("what about the thing")
    assert notes.search("what about the") != []


def test_a_blank_note_is_refused(db):
    assert "needs" in notes.add("   ")


# -- degraded mode ----------------------------------------------------------
def test_no_fts5_degrades_instead_of_raising(db, tree, monkeypatch):
    """FTS5 is an optional sqlite module. A stripped build must cost the user
    ranking quality, not the whole assistant."""
    monkeypatch.setattr(notes, "_FTS5", False)
    monkeypatch.setattr(notes, "_ready", {"db": None})

    notes.add("the kiln fires at cone six on Sunday")
    memory.log_turn("user", "book the kiln for Sunday")
    _write(tree, "pottery.md", "kiln maintenance is overdue")
    notes.index_pass()
    notes.sync_log()

    hits = notes.search("kiln", limit=6)
    assert {h["src"] for h in hits} >= {"note", "turn", "file"}, hits
    assert "degraded" in notes.status()


def test_degraded_mode_warns_the_user_once(db, monkeypatch):
    monkeypatch.setattr(notes, "_FTS5", False)
    monkeypatch.setattr(notes, "_ready", {"db": None})
    notes.add("the kiln fires at cone six")

    first = notes.recall("kiln")
    assert "degraded" in first
    second = notes.recall("kiln")
    assert "degraded" not in second, "the caveat is repeated on every answer"


def test_fts5_probe_leaves_nothing_behind(db):
    """The probe runs against the live database. If it left a table there it
    would show up in every schema dump and in selfedit's diffs forever."""
    con = memory.connect()
    notes._ensure(con)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE '%probe%'").fetchall()]
    con.close()
    assert names == []


# -- the background tick ----------------------------------------------------
def test_tick_survives_a_broken_root(db, monkeypatch):
    """A root that has been unmounted or deleted must not kill the loop that
    calls this every few minutes."""
    monkeypatch.setattr(notes, "_roots", lambda: [notes.Path("/definitely/not/here")])
    memory.log_turn("user", "hello there")
    out = notes.tick()
    assert out["turns"] == 1
    assert out["examined"] == 0


# -- the walk must always move, and must never mistake absence for deletion --
def test_a_slow_walk_still_makes_progress(db, tree, monkeypatch):
    """Reaching a far-along resume cursor is itself part of the walk. If the
    deadline is allowed to end a pass that has examined nothing, the pass writes
    back the cursor it started with and stalls there permanently - burning the
    whole budget every tick and indexing not one file, with nothing in the
    journal to say so."""
    for i in range(6):
        _write(tree, f"f{i}.md", f"tortoise paragraph {i}")
    calls = {"n": 0}

    def already_late():
        # First call sets the deadline; every check after it is over budget.
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1000.0

    monkeypatch.setattr(notes.time, "monotonic", already_late)
    first = notes.index_pass()
    assert first["examined"] >= 1, first
    assert int(memory.meta_get("notes_walk_pos", "0")) >= 1, "the walk stalled"


def test_an_unreachable_root_does_not_empty_the_index(db, tree, monkeypatch):
    """An unmounted drive yields no files and therefore looks exactly like a
    completed lap. Reaping on that basis deletes every document under it in
    silence, and the next pass re-reads the lot once the drive is back."""
    _write(tree, "log.md", "narwhal sightings in March")
    notes.index_pass()
    assert notes.search("narwhal")

    monkeypatch.setattr(notes, "_roots", lambda: [tree.parent / "not-mounted"])
    stats = notes.index_pass()
    assert stats["removed"] == 0, stats
    assert notes.search("narwhal"), "an unmounted root wiped the file index"


def test_a_walk_that_yields_nothing_keeps_files_that_still_exist(db, tree, monkeypatch):
    """os.walk() reports no error when it cannot read a directory - it just
    yields nothing. 'The walk did not reach it' and 'it was deleted' are
    different claims and only the second one may drop a document."""
    _write(tree, "log.md", "narwhal sightings in March")
    notes.index_pass()

    monkeypatch.setattr(notes, "_candidates", lambda roots: iter(()))
    stats = notes.index_pass()
    assert stats["removed"] == 0, stats
    assert notes.search("narwhal"), "an unreadable directory wiped the index"


# -- one writer at a time, but never for long ------------------------------
def test_a_pass_does_not_hold_the_database_locked(db, tree, monkeypatch):
    """schedule.py ticks this database every five seconds and log_turn() writes
    it on every utterance, both with a 5s busy timeout. A single transaction
    held across the whole pass - and therefore across a 20s pdftotext call -
    turns those writes into 'database is locked', which is a crashed voice turn
    rather than a slow one."""
    for i in range(3):
        _write(tree, f"f{i}.md", f"albatross paragraph {i}")
    seen = []
    real = notes._extract

    def probe(p):
        # Exactly where a pdftotext subprocess would be: mid-pass, after
        # earlier files have already been written.
        try:
            memory.log_turn("user", "spoken while the indexer is working")
            seen.append(True)
        except Exception as e:                       # pragma: no cover
            seen.append(repr(e))
        return real(p)

    monkeypatch.setattr(notes, "_extract", probe)
    notes.index_pass()
    assert len(seen) == 3 and all(x is True for x in seen), seen


def test_re_indexing_replaces_an_entry_instead_of_duplicating_it(db, tree):
    """Each write deletes the previous row first. That delete is keyed on a
    derived rowid because src and ref are UNINDEXED fts5 columns - matching on
    them is a full scan of the index and was measurably so. If the derivation
    ever stops agreeing between _put and _drop, nothing raises: the index just
    grows a copy per pass and recall reads the same sentence back twice."""
    p = _write(tree, "plan.md", "the greenhouse roof needs new glass")
    notes.index_pass()
    for _ in range(3):
        time.sleep(0.01)
        p.write_text("the greenhouse roof needs new glass", encoding="utf-8")
        notes.index_pass()
    assert len(notes.search("greenhouse", limit=10)) == 1


# -- PDFs -------------------------------------------------------------------
def test_a_large_pdf_is_still_extracted(db, tree, monkeypatch):
    """A PDF is a compressed container: a 40-page report is megabytes on disk
    and a few pages of words. Judging one by the text-file byte cap rejects
    nearly every real PDF, leaving the pdftotext path dead while reporting
    nothing but 'too big'."""
    monkeypatch.setattr(notes.shutil, "which", lambda name: "/usr/bin/pdftotext")
    monkeypatch.setattr(notes, "_extract", lambda p: "quarterly report on basilisk sales")
    (tree / "report.pdf").write_bytes(b"%PDF-1.4" + b"\x00" * 400000)

    stats = notes.index_pass()
    assert stats["too_big"] == 0, stats
    assert notes.search("basilisk"), "a normal-sized PDF was rejected on file size"


def test_an_apikey_file_is_refused(db, tree):
    """*token* and *secret* were listed but *key* only ever as a suffix, so
    apikey.json - a real shape, gcloud and a dozen CLIs write it - was
    indexable and speakable."""
    for name in ("apikey.json", "api_key.txt"):
        assert notes.excluded(str(tree / name)), name


def test_a_quote_from_a_file_never_reads_a_url_aloud(db, tree):
    """Quotes are lifted verbatim out of READMEs and dictated notes, so the
    prose rule cannot be enforced where the sentence is written - only where the
    borrowed text enters it. A URL read out character by character is the worst
    thing this module can say."""
    _write(tree, "readme.md",
           "## Setup\n\nSee https://example.com/docs/waypoint for the "
           "**waypoint** table | column | value |")
    notes.index_pass()

    answer = notes.recall("waypoint")
    assert "waypoint" in answer.lower(), answer
    for bad in ("http", "|", "**", "##"):
        assert bad not in answer, f"{bad!r} would be read aloud"


def test_tick_returns_the_same_shape_when_a_pass_fails(db, monkeypatch):
    """The tick loop reads stats out of this dict. Returning a bare {} on the
    failure path means a KeyError in the caller on exactly the run that already
    went wrong, which kills the loop instead of logging one line."""
    def boom(*a, **kw):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(notes, "index_pass", boom)
    out = notes.tick()
    assert out["examined"] == 0 and out["wrapped"] is False, out


def test_the_note_table_is_not_named_notes(db):
    """The /local/comms contract already uses 'notes' for phone notifications.
    CREATE TABLE IF NOT EXISTS is silent against a table of the same name and a
    different shape, so sharing it would surface as an INSERT failing at runtime
    on the box we cannot reach."""
    notes.add("the ferry leaves at six")
    con = memory.connect()
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    assert "user_notes" in names
    assert "notes" not in names
