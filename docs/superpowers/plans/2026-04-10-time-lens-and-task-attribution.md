# Time-Lens and Task Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Windows desktop-app session metadata enrichment, an America/Chicago hourly view of usage, a custom date range picker, and a peak-hour visual overlay driven by an editable config file.

**Architecture:** Scanner gains a Windows-only second data source that joins desktop session metadata to existing sessions via `cliSessionId`, adding two new columns (`title`, `original_cwd`). Dashboard backend performs UTC→Chicago bucketing in Python using `zoneinfo` and ships a new `turns_by_hour_local` structure. Frontend adds two new Chart.js charts (averaged hourly histogram + precise hourly timeline), a custom date range UI mutually exclusive with existing presets, and a reusable peak-band overlay plugin driven by `peak-hours.json`.

**Tech Stack:** Python 3.9+ stdlib only (`sqlite3`, `json`, `pathlib`, `zoneinfo`, `http.server`), Chart.js 4.4 via CDN, vanilla JavaScript with `Intl.DateTimeFormat` for client-side timezone conversion, `unittest` for testing.

**Spec:** `docs/superpowers/specs/2026-04-10-time-lens-and-task-attribution-design.md`

---

## Task ordering rationale

Tasks are ordered so each one produces a passing test suite and a meaningful commit:

1. **Schema first** (Task 1) — all downstream tasks depend on the new columns existing.
2. **Desktop metadata reader** (Task 2) — pure function, no dependencies.
3. **Enrichment function** (Task 3) — uses Task 1's schema, Task 2's reader.
4. **Scanner integration** (Task 4) — wires enrichment into `scan()`.
5. **Timezone helper** (Task 5) — pure function, foundation for all hourly work.
6. **Peak bands config loader** (Task 6) — independent; lets us ship the config file early.
7. **Hourly aggregation in dashboard API** (Task 7) — uses Task 5.
8. **Session list title fallback** (Task 8) — uses Task 1; tiny.
9. **Custom date picker UI** (Task 9) — self-contained HTML/JS.
10. **Hour-of-day histogram chart** (Task 10) — uses Task 7's API data.
11. **Hourly timeline chart** (Task 11) — uses Task 7's API data.
12. **Peak-band Chart.js plugin** (Task 12) — uses Task 6's peak bands; applies to charts from Tasks 10 and 11.
13. **Sessions table title display** (Task 13) — uses Task 8's backend data.
14. **Documentation updates** (Task 14) — README and CHANGELOG.

---

## Task 1: Add `title` and `original_cwd` columns to sessions

**Files:**
- Modify: `scanner.py` (add to `init_db`, around lines 24–74)
- Test: `tests/test_scanner.py` (new test in `TestDatabaseOperations` or new class)

**What:** Extend the sessions schema with two new TEXT columns. Use the same `try SELECT / catch OperationalError / ALTER` pattern already used for `turns.message_id`.

- [ ] **Step 1.1: Write the failing test**

Add this test method to `tests/test_scanner.py` inside a new class `TestSchemaEvolution`:

```python
class TestSchemaEvolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "test.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_title_and_original_cwd_columns_exist_after_init(self):
        from scanner import get_db, init_db
        conn = get_db(self.db_path)
        init_db(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        self.assertIn("title", cols)
        self.assertIn("original_cwd", cols)
        conn.close()

    def test_schema_evolution_from_old_sessions_table(self):
        """Simulate upgrading from a DB that predates the new columns."""
        from scanner import get_db, init_db
        conn = get_db(self.db_path)
        # Create an old-schema sessions table (no title, no original_cwd)
        conn.execute("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                project_name TEXT,
                first_timestamp TEXT,
                last_timestamp TEXT,
                git_branch TEXT,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_read INTEGER DEFAULT 0,
                total_cache_creation INTEGER DEFAULT 0,
                model TEXT,
                turn_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO sessions (session_id) VALUES ('old-session')")
        conn.commit()
        # Re-run init_db — should add new columns without losing data
        init_db(conn)
        row = conn.execute("SELECT session_id, title, original_cwd FROM sessions").fetchone()
        self.assertEqual(row[0], "old-session")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        conn.close()
```

Add these imports at the top of `tests/test_scanner.py` if not already present:

```python
import tempfile
import shutil
from pathlib import Path
import unittest
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `python -m unittest tests.test_scanner.TestSchemaEvolution -v`
Expected: FAIL with `AssertionError: 'title' not found in [...]` or similar.

- [ ] **Step 1.3: Modify `scanner.py` `init_db` to add the columns**

In `scanner.py`, locate the `init_db` function (starts around line 24). After the existing block that adds `message_id` via ALTER (around lines 65–68), add:

```python
    # Add title column if upgrading from older schema
    try:
        conn.execute("SELECT title FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
    # Add original_cwd column if upgrading from older schema
    try:
        conn.execute("SELECT original_cwd FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sessions ADD COLUMN original_cwd TEXT")
```

Also update the `CREATE TABLE IF NOT EXISTS sessions` block in `init_db` (lines 26–38) to include the new columns for fresh databases:

```python
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0,
            title           TEXT,
            original_cwd    TEXT
        );
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `python -m unittest tests.test_scanner.TestSchemaEvolution -v`
Expected: PASS (2 tests).

Also run the full scanner test suite to make sure nothing broke:

Run: `python -m unittest tests.test_scanner -v`
Expected: all existing tests pass + the 2 new ones.

- [ ] **Step 1.5: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): add title and original_cwd columns to sessions

Adds two new TEXT columns to the sessions table. Handled via the same
try-SELECT / catch OperationalError / ALTER pattern used for the
message_id column. Prepares the schema for Windows desktop metadata
enrichment in the next task.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Implement `read_desktop_metadata()`

**Files:**
- Modify: `scanner.py` (add constant + function after line 15)
- Test: `tests/test_scanner.py` (new class `TestReadDesktopMetadata`)

**What:** A pure function that walks `%APPDATA%/Claude/claude-code-sessions/**/local_*.json` and returns a dict keyed by `cliSessionId`. Silently handles missing directory, malformed JSON, and missing `cliSessionId` field.

- [ ] **Step 2.1: Write the failing tests**

Add this class to `tests/test_scanner.py`:

```python
class TestReadDesktopMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_json(self, relpath, data):
        f = self.tmp / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
        return f

    def test_reads_valid_metadata_file(self):
        from scanner import read_desktop_metadata
        self._write_json("account-a/install-b/local_abc.json", {
            "cliSessionId": "cli-session-123",
            "title": "Hourly checkin",
            "cwd": "C:\\users\\scott",
            "model": "claude-opus-4-6[1m]",
            "createdAt": 1775855438582,
            "lastActivityAt": 1775856409298,
        })
        result = read_desktop_metadata(self.tmp)
        self.assertIn("cli-session-123", result)
        self.assertEqual(result["cli-session-123"]["title"], "Hourly checkin")
        self.assertEqual(result["cli-session-123"]["original_cwd"], "C:\\users\\scott")
        self.assertEqual(result["cli-session-123"]["model"], "claude-opus-4-6[1m]")
        self.assertEqual(result["cli-session-123"]["created_at_ms"], 1775855438582)
        self.assertEqual(result["cli-session-123"]["last_activity_at_ms"], 1775856409298)

    def test_malformed_json_is_skipped(self):
        from scanner import read_desktop_metadata
        # One valid file
        self._write_json("a/b/local_good.json", {
            "cliSessionId": "good", "title": "Good", "cwd": "/home",
        })
        # One malformed file
        bad = self.tmp / "a/b/local_bad.json"
        bad.write_text("{this is not valid json", encoding="utf-8")
        result = read_desktop_metadata(self.tmp)
        self.assertIn("good", result)
        self.assertEqual(len(result), 1)  # bad file silently dropped

    def test_missing_directory_returns_empty(self):
        from scanner import read_desktop_metadata
        nonexistent = self.tmp / "does" / "not" / "exist"
        result = read_desktop_metadata(nonexistent)
        self.assertEqual(result, {})

    def test_missing_cli_session_id_is_skipped(self):
        from scanner import read_desktop_metadata
        self._write_json("a/b/local_no_id.json", {
            "title": "No ID", "cwd": "/home",
            # cliSessionId missing
        })
        result = read_desktop_metadata(self.tmp)
        self.assertEqual(result, {})
```

Add `import json` to `tests/test_scanner.py` top if not already there.

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `python -m unittest tests.test_scanner.TestReadDesktopMetadata -v`
Expected: FAIL with `ImportError: cannot import name 'read_desktop_metadata' from 'scanner'`.

- [ ] **Step 2.3: Implement `read_desktop_metadata` in `scanner.py`**

Add `import os` to the top of `scanner.py` imports if not already present (it is, in the current file). Then add this constant immediately after the existing `DEFAULT_PROJECTS_DIRS` definition (around line 15):

```python
DESKTOP_METADATA_DIR = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude-code-sessions"
```

Add this function after `project_name_from_cwd` (around line 86):

```python
def read_desktop_metadata(desktop_dir=DESKTOP_METADATA_DIR):
    """Walk the Windows Claude Desktop session metadata directory and return
    a dict keyed by cliSessionId.

    Silently returns {} if the directory doesn't exist. Individual files
    that are malformed, missing cliSessionId, or unreadable are dropped
    without raising.

    Returns:
        dict: {cli_session_id: {
            "title": str | None,
            "original_cwd": str | None,
            "model": str | None,
            "created_at_ms": int | None,
            "last_activity_at_ms": int | None,
        }}
    """
    result = {}
    desktop_dir = Path(desktop_dir)
    if not desktop_dir.exists():
        return result

    for f in desktop_dir.rglob("local_*.json"):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        cli_id = data.get("cliSessionId")
        if not cli_id:
            continue
        result[cli_id] = {
            "title": data.get("title"),
            "original_cwd": data.get("cwd"),
            "model": data.get("model"),
            "created_at_ms": data.get("createdAt"),
            "last_activity_at_ms": data.get("lastActivityAt"),
        }
    return result
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `python -m unittest tests.test_scanner.TestReadDesktopMetadata -v`
Expected: PASS (4 tests).

- [ ] **Step 2.5: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): add read_desktop_metadata for Windows Claude Desktop

Walks %APPDATA%/Claude/claude-code-sessions/**/local_*.json and returns
a dict keyed by cliSessionId. Silently handles missing directory,
malformed JSON, and missing cliSessionId. Pure function — no DB or
side effects. Wired into scan() in a later task.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Implement `enrich_sessions_with_desktop_metadata()`

**Files:**
- Modify: `scanner.py` (add function after `upsert_sessions`)
- Test: `tests/test_scanner.py` (new class `TestEnrichSessionsWithDesktopMetadata`)

**What:** Takes a connection and a metadata dict from Task 2. Runs `UPDATE sessions SET title=?, original_cwd=? WHERE session_id=?` for each matching record. Does not clear existing titles when metadata is absent. Does not touch unmatched sessions.

- [ ] **Step 3.1: Write the failing tests**

Add this class to `tests/test_scanner.py`:

```python
class TestEnrichSessionsWithDesktopMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "test.db"
        from scanner import get_db, init_db
        self.conn = get_db(self.db_path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert_session(self, session_id, title=None, original_cwd=None):
        self.conn.execute("""
            INSERT INTO sessions (session_id, project_name, title, original_cwd)
            VALUES (?, ?, ?, ?)
        """, (session_id, "some/project", title, original_cwd))
        self.conn.commit()

    def _get_session(self, session_id):
        return self.conn.execute("""
            SELECT session_id, title, original_cwd FROM sessions WHERE session_id = ?
        """, (session_id,)).fetchone()

    def test_updates_matching_sessions(self):
        from scanner import enrich_sessions_with_desktop_metadata
        self._insert_session("abc-123")
        metadata = {
            "abc-123": {
                "title": "Hourly checkin",
                "original_cwd": "C:\\users\\scott",
                "model": None, "created_at_ms": None, "last_activity_at_ms": None,
            }
        }
        enrich_sessions_with_desktop_metadata(self.conn, metadata)
        row = self._get_session("abc-123")
        self.assertEqual(row["title"], "Hourly checkin")
        self.assertEqual(row["original_cwd"], "C:\\users\\scott")

    def test_preserves_existing_title_when_metadata_absent(self):
        from scanner import enrich_sessions_with_desktop_metadata
        self._insert_session("abc-123", title="Previously set", original_cwd="/old/path")
        enrich_sessions_with_desktop_metadata(self.conn, {})  # empty metadata
        row = self._get_session("abc-123")
        self.assertEqual(row["title"], "Previously set")
        self.assertEqual(row["original_cwd"], "/old/path")

    def test_does_not_affect_unmatched_sessions(self):
        from scanner import enrich_sessions_with_desktop_metadata
        self._insert_session("session-a")
        self._insert_session("session-b")
        metadata = {
            "session-b": {
                "title": "B's title", "original_cwd": "/b/cwd",
                "model": None, "created_at_ms": None, "last_activity_at_ms": None,
            }
        }
        enrich_sessions_with_desktop_metadata(self.conn, metadata)
        row_a = self._get_session("session-a")
        row_b = self._get_session("session-b")
        self.assertIsNone(row_a["title"])
        self.assertIsNone(row_a["original_cwd"])
        self.assertEqual(row_b["title"], "B's title")
        self.assertEqual(row_b["original_cwd"], "/b/cwd")
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `python -m unittest tests.test_scanner.TestEnrichSessionsWithDesktopMetadata -v`
Expected: FAIL with `ImportError: cannot import name 'enrich_sessions_with_desktop_metadata'`.

- [ ] **Step 3.3: Implement `enrich_sessions_with_desktop_metadata` in `scanner.py`**

Add this function after `upsert_sessions` (around line 267 in the current file):

```python
def enrich_sessions_with_desktop_metadata(conn, metadata):
    """Update sessions.title and sessions.original_cwd from a desktop
    metadata dict (as returned by read_desktop_metadata).

    Only updates rows where session_id matches a key in metadata. Never
    clears existing values: if a session was previously enriched and its
    metadata is now absent, the old values are preserved (because this
    function simply doesn't touch unmatched sessions).

    Args:
        conn: sqlite3 Connection
        metadata: dict {cli_session_id: {title, original_cwd, ...}}
    """
    for cli_id, meta in metadata.items():
        conn.execute("""
            UPDATE sessions
            SET title = ?, original_cwd = ?
            WHERE session_id = ?
        """, (meta.get("title"), meta.get("original_cwd"), cli_id))
    conn.commit()
```

- [ ] **Step 3.4: Run tests to verify they pass**

Run: `python -m unittest tests.test_scanner.TestEnrichSessionsWithDesktopMetadata -v`
Expected: PASS (3 tests).

- [ ] **Step 3.5: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): add enrich_sessions_with_desktop_metadata

UPDATE-only operation that sets title and original_cwd on matching
sessions. Preserves existing values for sessions not in the metadata
dict. Idempotent. Wired into scan() in the next task.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire desktop metadata enrichment into `scan()`

**Files:**
- Modify: `scanner.py` (add enrichment call at end of `scan()`, around line 471)
- Test: `tests/test_scanner.py` (new class `TestScanIntegrationWithDesktopMetadata`)

**What:** After the existing total-recompute step in `scan()`, call `read_desktop_metadata()` and `enrich_sessions_with_desktop_metadata()`. This is the only place they're called in production code.

- [ ] **Step 4.1: Write the failing test**

Add this class to `tests/test_scanner.py`:

```python
class TestScanIntegrationWithDesktopMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.projects_dir = self.tmp / "projects"
        self.desktop_dir = self.tmp / "desktop-metadata"
        self.db_path = self.tmp / "test.db"
        self.projects_dir.mkdir()
        self.desktop_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_scan_enriches_from_desktop_dir(self):
        """After scan(), sessions should have title and original_cwd populated
        from the desktop metadata, while token totals remain correct."""
        # Write a fixture JSONL with one assistant turn
        session_id = "test-session-abc"
        jsonl_path = self.projects_dir / "test-project" / f"{session_id}.jsonl"
        jsonl_path.parent.mkdir(parents=True)
        records = [
            {
                "type": "assistant",
                "sessionId": session_id,
                "timestamp": "2026-04-10T14:00:00Z",
                "cwd": "/some/cwd",
                "gitBranch": "main",
                "message": {
                    "id": "msg-1",
                    "model": "claude-opus-4-6",
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 500,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                    "content": [],
                },
            }
        ]
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        # Write matching desktop metadata
        meta_path = self.desktop_dir / "account" / "install" / "local_xyz.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text(json.dumps({
            "cliSessionId": session_id,
            "title": "My enriched title",
            "cwd": "C:\\real\\cwd",
            "model": "claude-opus-4-6[1m]",
            "createdAt": 1775855438582,
            "lastActivityAt": 1775856409298,
        }), encoding="utf-8")

        # Patch DESKTOP_METADATA_DIR to point at our fixture dir
        import scanner
        original_dir = scanner.DESKTOP_METADATA_DIR
        try:
            scanner.DESKTOP_METADATA_DIR = self.desktop_dir
            scanner.scan(projects_dir=self.projects_dir, db_path=self.db_path, verbose=False)
        finally:
            scanner.DESKTOP_METADATA_DIR = original_dir

        # Verify title and original_cwd are populated
        from scanner import get_db
        conn = get_db(self.db_path)
        row = conn.execute(
            "SELECT title, original_cwd, total_input_tokens, total_output_tokens "
            "FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        self.assertEqual(row["title"], "My enriched title")
        self.assertEqual(row["original_cwd"], "C:\\real\\cwd")
        # Token counts must not be affected by enrichment
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertEqual(row["total_output_tokens"], 500)
        conn.close()
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `python -m unittest tests.test_scanner.TestScanIntegrationWithDesktopMetadata -v`
Expected: FAIL — `title` will be None because `scan()` doesn't call the enrichment yet.

- [ ] **Step 4.3: Wire enrichment into `scan()`**

In `scanner.py`, locate the block at the end of `scan()` that recomputes session totals (around lines 462–471, starting with `# Recompute session totals from actual turns in DB.`). Immediately after the `conn.commit()` that closes that recompute block, add:

```python
    # Enrich sessions with desktop app metadata (Windows only; no-op elsewhere)
    desktop_metadata = read_desktop_metadata()
    if desktop_metadata:
        enrich_sessions_with_desktop_metadata(conn, desktop_metadata)
        if verbose:
            print(f"  Desktop metadata: {len(desktop_metadata)} sessions enriched")
```

Place this block BEFORE the `if verbose: print(f"\nScan complete:")` summary block so the summary message appears last.

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `python -m unittest tests.test_scanner.TestScanIntegrationWithDesktopMetadata -v`
Expected: PASS (1 test).

Run the entire scanner test suite to verify nothing regressed:

Run: `python -m unittest tests.test_scanner -v`
Expected: all tests pass (original + 10 new across Tasks 1–4).

- [ ] **Step 4.5: Commit**

```bash
git add scanner.py tests/test_scanner.py
git commit -m "feat(scanner): wire desktop metadata enrichment into scan()

After the existing total-recompute step, scan() now calls
read_desktop_metadata() and enrich_sessions_with_desktop_metadata()
so each scan run populates title and original_cwd for matching
sessions. Token totals are unaffected — enrichment is additive label
data only.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Add `to_local_hour()` timezone helper

**Files:**
- Modify: `dashboard.py` (add import + helper near top, after imports)
- Test: `tests/test_dashboard.py` (new class `TestTimezoneBucketing`)

**What:** A pure function that takes a UTC ISO timestamp string and returns `(local_date_str, local_hour_int)` in `America/Chicago`. Uses `zoneinfo` for DST-aware conversion. Handles unparseable input by returning `('', 0)`.

- [ ] **Step 5.1: Write the failing tests**

Add this class to `tests/test_dashboard.py`:

```python
class TestTimezoneBucketing(unittest.TestCase):
    def test_utc_midnight_converts_to_chicago_6pm_previous_day_standard_time(self):
        """January timestamp → CST (UTC-6)."""
        from dashboard import to_local_hour
        # 2026-01-15 00:00 UTC = 2026-01-14 18:00 CST
        day, hour = to_local_hour("2026-01-15T00:00:00Z")
        self.assertEqual(day, "2026-01-14")
        self.assertEqual(hour, 18)

    def test_utc_midnight_converts_to_chicago_7pm_previous_day_dst(self):
        """July timestamp → CDT (UTC-5)."""
        from dashboard import to_local_hour
        # 2026-07-15 00:00 UTC = 2026-07-14 19:00 CDT
        day, hour = to_local_hour("2026-07-15T00:00:00Z")
        self.assertEqual(day, "2026-07-14")
        self.assertEqual(hour, 19)

    def test_dst_spring_forward_boundary(self):
        """2026-03-08 08:00 UTC = 02:00 CST (still) → 03:00 CDT immediately after.
        Spring forward is at 02:00 local; 08:00 UTC is the moment of transition.
        We verify the post-transition hour assignment."""
        from dashboard import to_local_hour
        # 2026-03-08 08:00 UTC = 2026-03-08 03:00 CDT (after spring forward)
        day, hour = to_local_hour("2026-03-08T08:00:00Z")
        self.assertEqual(day, "2026-03-08")
        self.assertEqual(hour, 3)

    def test_dst_fall_back_boundary(self):
        """2026-11-01 07:00 UTC = 01:00 CST (post-fall-back). The ambiguous
        hour (1am happens twice) is handled deterministically by zoneinfo —
        we accept whatever it returns as long as it's hour 1."""
        from dashboard import to_local_hour
        # 2026-11-01 07:00 UTC = 2026-11-01 01:00 CST (after fall back)
        day, hour = to_local_hour("2026-11-01T07:00:00Z")
        self.assertEqual(day, "2026-11-01")
        self.assertEqual(hour, 1)

    def test_unparseable_timestamp_returns_empty(self):
        from dashboard import to_local_hour
        day, hour = to_local_hour("not a timestamp")
        self.assertEqual(day, "")
        self.assertEqual(hour, 0)

    def test_none_timestamp_returns_empty(self):
        from dashboard import to_local_hour
        day, hour = to_local_hour(None)
        self.assertEqual(day, "")
        self.assertEqual(hour, 0)
```

Make sure `import unittest` is at the top of `tests/test_dashboard.py` (it is).

- [ ] **Step 5.2: Run tests to verify they fail**

Run: `python -m unittest tests.test_dashboard.TestTimezoneBucketing -v`
Expected: FAIL with `ImportError: cannot import name 'to_local_hour' from 'dashboard'`.

- [ ] **Step 5.3: Implement `to_local_hour` in `dashboard.py`**

At the top of `dashboard.py`, add to the existing imports block (around line 5):

```python
from zoneinfo import ZoneInfo
```

Then add this constant and function immediately after the `DB_PATH` constant (around line 12):

```python
CHICAGO = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


def to_local_hour(iso_utc):
    """Convert a UTC ISO timestamp string to (local_date_str, local_hour_int)
    in America/Chicago. Returns ('', 0) for unparseable input.

    Handles the 'Z' suffix and timezone-aware inputs. DST transitions are
    handled deterministically by zoneinfo.
    """
    if not iso_utc or not isinstance(iso_utc, str):
        return ("", 0)
    try:
        # Normalize trailing Z to +00:00 for fromisoformat
        normalized = iso_utc.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local = dt.astimezone(CHICAGO)
        return (local.strftime("%Y-%m-%d"), local.hour)
    except (ValueError, TypeError):
        return ("", 0)
```

- [ ] **Step 5.4: Run tests to verify they pass**

Run: `python -m unittest tests.test_dashboard.TestTimezoneBucketing -v`
Expected: PASS (6 tests).

- [ ] **Step 5.5: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add to_local_hour() for UTC → Chicago conversion

Pure function that converts a UTC ISO timestamp to a (date, hour) pair
in America/Chicago. Uses zoneinfo from the stdlib (no new dependencies)
and handles DST transitions deterministically. Foundation for the
hourly views added in later tasks.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Create `peak-hours.json` and add `load_peak_bands()`

**Files:**
- Create: `peak-hours.json` (repo root)
- Modify: `dashboard.py` (add `load_peak_bands` near top)
- Test: `tests/test_dashboard.py` (new class `TestLoadPeakBands`)

**What:** Ship the config file with the default Anthropic peak band (Mon–Fri 05:00–11:00 America/Los_Angeles). Add a loader that validates the structure and silently drops invalid bands with a warning.

- [ ] **Step 6.1: Create `peak-hours.json`**

Write this file to `E:\claude-usage\peak-hours.json`:

```json
{
  "source": "Reddit community synthesis, 2026-03-26 (Thariq Shihipar update)",
  "note": "Peak times are stored in their native Pacific timezone and converted to the viewer's timezone at render time. Edit this file and refresh the dashboard to change the overlay.",
  "bands": [
    {
      "timezone": "America/Los_Angeles",
      "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
      "start": "05:00",
      "end": "11:00",
      "label": "Anthropic peak"
    }
  ]
}
```

- [ ] **Step 6.2: Write the failing tests**

Add this class to `tests/test_dashboard.py`:

```python
class TestLoadPeakBands(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, data):
        p = self.tmp / "peak-hours.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_valid_file_loads(self):
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "America/Los_Angeles", "days": ["Mon", "Tue"],
                 "start": "05:00", "end": "11:00", "label": "Test"}
            ]
        })
        bands = load_peak_bands(p)
        self.assertEqual(len(bands), 1)
        self.assertEqual(bands[0]["timezone"], "America/Los_Angeles")
        self.assertEqual(bands[0]["start"], "05:00")

    def test_missing_file_returns_empty(self):
        from dashboard import load_peak_bands
        bands = load_peak_bands(self.tmp / "nonexistent.json")
        self.assertEqual(bands, [])

    def test_malformed_json_returns_empty(self):
        from dashboard import load_peak_bands
        p = self.tmp / "peak-hours.json"
        p.write_text("{not valid json", encoding="utf-8")
        bands = load_peak_bands(p)
        self.assertEqual(bands, [])

    def test_invalid_band_is_dropped(self):
        """One valid band + one missing required field → only the valid band returned."""
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "America/Los_Angeles", "days": ["Mon"],
                 "start": "05:00", "end": "11:00"},  # valid
                {"timezone": "America/Los_Angeles", "days": ["Tue"],
                 "start": "05:00"},  # missing 'end'
            ]
        })
        bands = load_peak_bands(p)
        self.assertEqual(len(bands), 1)
        self.assertEqual(bands[0]["days"], ["Mon"])

    def test_bad_timezone_is_dropped(self):
        from dashboard import load_peak_bands
        p = self._write_config({
            "bands": [
                {"timezone": "Not/AReal_Zone", "days": ["Mon"],
                 "start": "05:00", "end": "11:00"}
            ]
        })
        bands = load_peak_bands(p)
        self.assertEqual(bands, [])
```

Add these imports at the top of `tests/test_dashboard.py` if not present:

```python
import json
import tempfile
import shutil
from pathlib import Path
```

- [ ] **Step 6.3: Run tests to verify they fail**

Run: `python -m unittest tests.test_dashboard.TestLoadPeakBands -v`
Expected: FAIL with `ImportError: cannot import name 'load_peak_bands'`.

- [ ] **Step 6.4: Implement `load_peak_bands` in `dashboard.py`**

Add this function after `to_local_hour` (which was added in Task 5):

```python
REQUIRED_BAND_FIELDS = ("timezone", "days", "start", "end")
PEAK_HOURS_PATH = Path(__file__).parent / "peak-hours.json"


def load_peak_bands(path=PEAK_HOURS_PATH):
    """Load peak-hours.json and return a list of validated band dicts.

    Silently returns [] on missing file, malformed JSON, or missing
    top-level 'bands' key, logging a single warning to stderr. Individual
    bands that fail validation are dropped from the returned list.

    Validation rules:
      - Required fields: timezone, days, start, end
      - timezone must be a valid IANA zone (resolvable by zoneinfo)
      - days must be a list
      - start and end must be 'HH:MM' strings with start < end
    """
    import sys
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"warning: peak-hours.json not found at {path}; overlay disabled", file=sys.stderr)
        return []
    except (OSError, json.JSONDecodeError) as e:
        print(f"warning: peak-hours.json could not be parsed: {e}", file=sys.stderr)
        return []

    if not isinstance(data, dict) or not isinstance(data.get("bands"), list):
        print("warning: peak-hours.json missing 'bands' list; overlay disabled", file=sys.stderr)
        return []

    valid = []
    for i, band in enumerate(data["bands"]):
        if not isinstance(band, dict):
            continue
        if any(band.get(k) is None for k in REQUIRED_BAND_FIELDS):
            print(f"warning: peak band #{i} missing required field; dropped", file=sys.stderr)
            continue
        if not isinstance(band["days"], list):
            continue
        try:
            ZoneInfo(band["timezone"])
        except Exception:
            print(f"warning: peak band #{i} has invalid timezone {band['timezone']!r}; dropped",
                  file=sys.stderr)
            continue
        if not (isinstance(band["start"], str) and isinstance(band["end"], str)):
            continue
        if band["start"] >= band["end"]:
            print(f"warning: peak band #{i} has start >= end; dropped", file=sys.stderr)
            continue
        valid.append(band)
    return valid
```

- [ ] **Step 6.5: Run tests to verify they pass**

Run: `python -m unittest tests.test_dashboard.TestLoadPeakBands -v`
Expected: PASS (5 tests).

- [ ] **Step 6.6: Commit**

```bash
git add peak-hours.json dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add peak-hours.json config and loader

Ships the default Anthropic peak band (Mon-Fri 05:00-11:00 Pacific,
per Reddit community source). The loader validates each band and
silently drops malformed ones with a stderr warning. Returns [] if
the file is missing or unparseable so the overlay gracefully disables.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Add hourly aggregation to dashboard API

**Files:**
- Modify: `dashboard.py` (add aggregation to `get_dashboard_data`, around lines 15–95)
- Test: `tests/test_dashboard.py` (new class `TestHourlyAggregation`)

**What:** Extend `get_dashboard_data` to compute a `turns_by_hour_local` list keyed by `(day_local, hour_local, model)` using `to_local_hour`. Also add `peak_bands` (from `load_peak_bands`) and `viewer_timezone` fields to the response.

- [ ] **Step 7.1: Write the failing tests**

Add this class to `tests/test_dashboard.py`:

```python
class TestHourlyAggregation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "test.db"
        from scanner import get_db, init_db
        conn = get_db(self.db_path)
        init_db(conn)
        # Seed: two turns in the same local hour, one turn in a different hour
        conn.execute("""
            INSERT INTO sessions (session_id, project_name, first_timestamp,
                last_timestamp, total_input_tokens, total_output_tokens,
                total_cache_read, total_cache_creation, model, turn_count)
            VALUES ('s1', 'proj', '2026-04-10T14:00:00Z', '2026-04-10T14:45:00Z',
                    300, 150, 0, 0, 'claude-opus-4-6', 2)
        """)
        conn.executemany("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, tool_name, cwd, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            # 2026-04-10T14:15:00Z = 2026-04-10 09:15 Chicago (CDT, UTC-5)
            ("s1", "2026-04-10T14:15:00Z", "claude-opus-4-6", 100, 50, 0, 0, None, "/cwd", "m1"),
            # 2026-04-10T14:45:00Z = 2026-04-10 09:45 Chicago
            ("s1", "2026-04-10T14:45:00Z", "claude-opus-4-6", 200, 100, 0, 0, None, "/cwd", "m2"),
            # 2026-04-10T20:00:00Z = 2026-04-10 15:00 Chicago
            ("s1", "2026-04-10T20:00:00Z", "claude-opus-4-6",  50,  25, 0, 0, None, "/cwd", "m3"),
        ])
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_turn_lands_in_correct_hour_bucket(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        hourly = data["turns_by_hour_local"]
        # Find the 15:00 bucket (Chicago) — should have one turn, 50 input
        h15 = [h for h in hourly if h["hour_local"] == 15]
        self.assertEqual(len(h15), 1)
        self.assertEqual(h15[0]["input"], 50)
        self.assertEqual(h15[0]["turns"], 1)
        self.assertEqual(h15[0]["day_local"], "2026-04-10")

    def test_multiple_turns_same_hour_sum_correctly(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        hourly = data["turns_by_hour_local"]
        h9 = [h for h in hourly if h["hour_local"] == 9]
        self.assertEqual(len(h9), 1)
        self.assertEqual(h9[0]["input"], 300)  # 100 + 200
        self.assertEqual(h9[0]["output"], 150)  # 50 + 100
        self.assertEqual(h9[0]["turns"], 2)

    def test_response_includes_peak_bands_and_timezone(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        self.assertIn("peak_bands", data)
        self.assertIn("viewer_timezone", data)
        self.assertEqual(data["viewer_timezone"], "America/Chicago")
        self.assertIsInstance(data["peak_bands"], list)
```

- [ ] **Step 7.2: Run tests to verify they fail**

Run: `python -m unittest tests.test_dashboard.TestHourlyAggregation -v`
Expected: FAIL — `turns_by_hour_local` not in response.

- [ ] **Step 7.3: Add hourly aggregation to `get_dashboard_data`**

In `dashboard.py`, inside `get_dashboard_data` (around lines 15–95), add the following block after the existing `daily_by_model` computation (around line 55, after the list comprehension closes) and before the sessions query (around line 57):

```python
    # ── Hourly bucketing in viewer's local time (America/Chicago) ─────────────
    hourly_rows = conn.execute("""
        SELECT timestamp, COALESCE(model, 'unknown') as model,
               input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens
        FROM turns
    """).fetchall()

    hourly_map = {}  # (day_local, hour_local, model) -> counters
    for r in hourly_rows:
        day_local, hour_local = to_local_hour(r["timestamp"])
        if not day_local:
            continue
        key = (day_local, hour_local, r["model"])
        if key not in hourly_map:
            hourly_map[key] = {
                "input": 0, "output": 0,
                "cache_read": 0, "cache_creation": 0,
                "turns": 0,
            }
        bucket = hourly_map[key]
        bucket["input"] += r["input_tokens"] or 0
        bucket["output"] += r["output_tokens"] or 0
        bucket["cache_read"] += r["cache_read_tokens"] or 0
        bucket["cache_creation"] += r["cache_creation_tokens"] or 0
        bucket["turns"] += 1

    turns_by_hour_local = [
        {"day_local": k[0], "hour_local": k[1], "model": k[2], **v}
        for k, v in hourly_map.items()
    ]
```

Then update the return dict at the end of `get_dashboard_data` (around lines 90–95) to include the new fields:

```python
    return {
        "all_models":     all_models,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "turns_by_hour_local": turns_by_hour_local,
        "peak_bands":     load_peak_bands(),
        "viewer_timezone": "America/Chicago",
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
```

- [ ] **Step 7.4: Run tests to verify they pass**

Run: `python -m unittest tests.test_dashboard.TestHourlyAggregation -v`
Expected: PASS (3 tests).

Run the entire dashboard test suite:

Run: `python -m unittest tests.test_dashboard -v`
Expected: all existing tests pass + new ones from Tasks 5, 6, 7.

- [ ] **Step 7.5: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add hourly aggregation to /api/data

get_dashboard_data now ships turns_by_hour_local, a flat list of
per-hour buckets grouped by (day_local, hour_local, model) in
America/Chicago. Also adds peak_bands and viewer_timezone fields to
the response. Client-side aggregation + filtering will be added in
the frontend tasks.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Session list title fallback

**Files:**
- Modify: `dashboard.py` (update `sessions_all` builder and SQL query in `get_dashboard_data`)
- Test: `tests/test_dashboard.py` (add test to existing `TestGetDashboardData` or new method)

**What:** The session SQL query must read the new `title` and `original_cwd` columns. The `sessions_all` builder must expose `project` as `title or project_name`, and include a `project_raw` field for the original name. CSV export in the frontend (Task 13) will use this.

- [ ] **Step 8.1: Write the failing test**

Add this method to `TestGetDashboardData` in `tests/test_dashboard.py` (or create the class if it doesn't exist with this shape):

```python
class TestSessionTitleFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "test.db"
        from scanner import get_db, init_db
        conn = get_db(self.db_path)
        init_db(conn)
        # Session A: has title
        conn.execute("""
            INSERT INTO sessions (session_id, project_name, first_timestamp,
                last_timestamp, total_input_tokens, total_output_tokens,
                total_cache_read, total_cache_creation, model, turn_count,
                title, original_cwd)
            VALUES ('sess-a', 'Users/scott', '2026-04-10T14:00:00Z',
                    '2026-04-10T14:30:00Z', 100, 50, 0, 0,
                    'claude-opus-4-6', 1, 'Hourly checkin', 'C:\\users\\scott')
        """)
        # Session B: no title
        conn.execute("""
            INSERT INTO sessions (session_id, project_name, first_timestamp,
                last_timestamp, total_input_tokens, total_output_tokens,
                total_cache_read, total_cache_creation, model, turn_count)
            VALUES ('sess-b', 'my/project', '2026-04-10T15:00:00Z',
                    '2026-04-10T15:30:00Z', 200, 100, 0, 0,
                    'claude-opus-4-6', 1)
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_titled_session_shows_title_as_project(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        sess_a = next(s for s in data["sessions_all"] if s["session_id"] == "sess-a"[:8])
        self.assertEqual(sess_a["project"], "Hourly checkin")
        self.assertEqual(sess_a["project_raw"], "Users/scott")

    def test_untitled_session_falls_back_to_project_name(self):
        from dashboard import get_dashboard_data
        data = get_dashboard_data(self.db_path)
        sess_b = next(s for s in data["sessions_all"] if s["session_id"] == "sess-b"[:8])
        self.assertEqual(sess_b["project"], "my/project")
        self.assertEqual(sess_b["project_raw"], "my/project")
```

- [ ] **Step 8.2: Run test to verify it fails**

Run: `python -m unittest tests.test_dashboard.TestSessionTitleFallback -v`
Expected: FAIL — `project_raw` not in response.

- [ ] **Step 8.3: Update `sessions_all` builder in `get_dashboard_data`**

In `dashboard.py`, update the session query (around lines 58–64) to include the new columns:

```python
    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            title, original_cwd
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()
```

Then update the list comprehension that builds `sessions_all` (around lines 66–86):

```python
    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        project_name = r["project_name"] or "unknown"
        title = r["title"]
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "project":       title or project_name,
            "project_raw":   project_name,
            "title":         title,
            "original_cwd":  r["original_cwd"],
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })
```

- [ ] **Step 8.4: Run tests to verify they pass**

Run: `python -m unittest tests.test_dashboard.TestSessionTitleFallback -v`
Expected: PASS (2 tests).

- [ ] **Step 8.5: Commit**

```bash
git add dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): expose session title with project_name fallback

The sessions list in /api/data now includes title and original_cwd
from the new sessions columns. The 'project' field uses title when
present, falling back to project_name otherwise. A new 'project_raw'
field preserves the cwd-derived name for CSV export and tooltips.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Custom date range picker UI

**Files:**
- Modify: `dashboard.py` HTML template (filter bar, state, URL helpers, applyFilter cutoff logic)

**What:** Two `<input type="date">` fields after the preset buttons. When populated, they override the preset range and deactivate the preset buttons. URL persistence via `?from=&to=`, mutually exclusive with `?range=`. No tests — this is a pure frontend interaction change exercised manually.

- [ ] **Step 9.1: Add the date input HTML to the filter bar**

In `dashboard.py`, locate the filter bar HTML in `HTML_TEMPLATE` (around lines 193–206). Add the following INSIDE `#filter-bar`, immediately after the `.range-group` div:

```html
  <div class="filter-sep"></div>
  <div class="filter-label">Custom</div>
  <input type="date" id="from-date" class="date-input" onchange="onCustomDateChange()">
  <span class="muted">–</span>
  <input type="date" id="to-date" class="date-input" onchange="onCustomDateChange()">
  <button class="filter-btn" id="clear-custom" onclick="clearCustomDates()" title="Clear custom dates">×</button>
```

Add this CSS rule to the `<style>` block (anywhere among the filter-bar styles, around line 135):

```css
  .date-input { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 3px 8px; border-radius: 4px; font-size: 12px; font-family: inherit; }
  .date-input::-webkit-calendar-picker-indicator { filter: invert(0.7); cursor: pointer; }
  .range-btn.inactive { opacity: 0.4; }
  #clear-custom { padding: 3px 8px; }
```

- [ ] **Step 9.2: Add state and handlers to the JS block**

In the state block (around lines 293–305), add:

```javascript
let customFrom = null;  // 'YYYY-MM-DD' or null
let customTo   = null;
```

Add these two handler functions immediately after `setRange` (around line 391):

```javascript
function onCustomDateChange() {
  const fromEl = document.getElementById('from-date');
  const toEl   = document.getElementById('to-date');
  customFrom = fromEl.value || null;
  customTo   = toEl.value || null;
  // If either is set, deactivate preset buttons
  const anyCustom = customFrom || customTo;
  document.querySelectorAll('.range-btn').forEach(btn => {
    btn.classList.toggle('inactive', !!anyCustom);
    btn.classList.toggle('active', !anyCustom && btn.dataset.range === selectedRange);
  });
  if (anyCustom) selectedRange = 'custom';
  updateURL();
  applyFilter();
}

function clearCustomDates() {
  document.getElementById('from-date').value = '';
  document.getElementById('to-date').value = '';
  customFrom = null;
  customTo = null;
  selectedRange = '30d';
  document.querySelectorAll('.range-btn').forEach(btn => {
    btn.classList.remove('inactive');
    btn.classList.toggle('active', btn.dataset.range === '30d');
  });
  updateURL();
  applyFilter();
}
```

- [ ] **Step 9.3: Update `setRange` to clear custom dates**

Modify `setRange` (around lines 384–391) to clear custom state when a preset is clicked:

```javascript
function setRange(range) {
  selectedRange = range;
  customFrom = null;
  customTo = null;
  document.getElementById('from-date').value = '';
  document.getElementById('to-date').value = '';
  document.querySelectorAll('.range-btn').forEach(btn => {
    btn.classList.remove('inactive');
    btn.classList.toggle('active', btn.dataset.range === range);
  });
  updateURL();
  applyFilter();
}
```

- [ ] **Step 9.4: Update `applyFilter` to honor custom dates**

In `applyFilter` (around line 501), replace the `const cutoff = getRangeCutoff(selectedRange);` line and the subsequent filter condition with the following. Find these lines:

```javascript
  const cutoff = getRangeCutoff(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff)
  );
```

And replace with:

```javascript
  // Compute date range: custom overrides preset
  const isCustom = customFrom || customTo;
  const rangeFrom = isCustom ? customFrom : getRangeCutoff(selectedRange);
  const rangeTo = isCustom ? customTo : null;

  const inRange = (day) => {
    if (rangeFrom && day < rangeFrom) return false;
    if (rangeTo && day > rangeTo) return false;
    return true;
  };

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && inRange(r.day)
  );
```

Then update the sessions filter a few lines below. Find:

```javascript
  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!cutoff || s.last_date >= cutoff)
  );
```

And replace with:

```javascript
  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && inRange(s.last_date)
  );
```

- [ ] **Step 9.5: Update `updateURL` for custom date persistence**

Replace `updateURL` (around lines 454–461) with:

```javascript
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (customFrom || customTo) {
    if (customFrom) params.set('from', customFrom);
    if (customTo)   params.set('to', customTo);
  } else if (selectedRange !== '30d') {
    params.set('range', selectedRange);
  }
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}
```

- [ ] **Step 9.6: Restore custom dates from URL on first load**

Find `readURLRange` (around lines 379–382) and add a sibling function right after it:

```javascript
function readURLCustomDates() {
  const p = new URLSearchParams(window.location.search);
  return {
    from: p.get('from'),
    to:   p.get('to'),
  };
}
```

Then in `loadData` (around line 853), find the `isFirstLoad` block and add custom-date restoration. Locate:

```javascript
    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
```

And add after the `document.querySelectorAll('.range-btn').forEach...` line but before `buildFilterUI`:

```javascript
      // Restore custom date range from URL if present
      const urlCustom = readURLCustomDates();
      if (urlCustom.from || urlCustom.to) {
        customFrom = urlCustom.from;
        customTo = urlCustom.to;
        if (customFrom) document.getElementById('from-date').value = customFrom;
        if (customTo)   document.getElementById('to-date').value = customTo;
        selectedRange = 'custom';
        document.querySelectorAll('.range-btn').forEach(btn => {
          btn.classList.add('inactive');
          btn.classList.remove('active');
        });
      }
```

- [ ] **Step 9.7: Smoke test the dashboard**

Run: `python cli.py dashboard`
Expected: dashboard opens. Verify:
- Two date inputs appear after the preset buttons
- Typing a `From` date greys out the presets and filters
- Clicking `×` clears and re-enables presets
- Clicking a preset clears the custom dates
- The URL updates to `?from=...&to=...` when custom is set
- Reloading with `?from=2026-04-01&to=2026-04-10` restores the state

Stop the server with Ctrl+C.

- [ ] **Step 9.8: Commit**

```bash
git add dashboard.py
git commit -m "feat(dashboard): add custom date range picker

Two native date inputs after the preset range buttons, mutually
exclusive with the presets. Using them deactivates (greys) the
preset buttons. URL persistence via ?from= and ?to= params that
override the existing ?range= behavior. Clicking any preset clears
the custom dates.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Hour-of-day histogram chart

**Files:**
- Modify: `dashboard.py` HTML template (add chart card, data aggregation, render function)

**What:** A new chart card inserted into the charts grid showing 24 stacked bars — one per hour 0–23 in America/Chicago, averaged across the distinct days in the selected range. Uses the same TOKEN_COLORS as the daily chart.

- [ ] **Step 10.1: Add the chart card HTML**

In `HTML_TEMPLATE`, find the `.charts-grid` block (around lines 210–223) and replace it with the following. This adds two new chart cards (the second one will be used in Task 11):

```html
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide">
      <h2 id="hour-histogram-title">Usage by Hour of Day — America/Chicago (averaged)</h2>
      <div class="chart-wrap"><canvas id="chart-hour-histogram"></canvas></div>
    </div>
    <div class="chart-card wide">
      <h2 id="hour-timeline-title">Hourly Timeline — America/Chicago</h2>
      <div class="chart-wrap tall" style="overflow-x: auto;"><canvas id="chart-hour-timeline"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
```

- [ ] **Step 10.2: Add the hourly histogram aggregation to `applyFilter`**

In `applyFilter` (around line 501 onward), immediately after the existing `const byModel = Object.values(modelMap).sort(...)` line, add:

```javascript
  // ── Hour-of-day histogram: average tokens per hour across distinct days ──
  const filteredHourly = rawData.turns_by_hour_local.filter(r =>
    selectedModels.has(r.model) && inRange(r.day_local)
  );
  const hourBuckets = Array.from({length: 24}, (_, h) => ({
    hour: h, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0,
  }));
  const daysWithData = new Set();
  for (const r of filteredHourly) {
    daysWithData.add(r.day_local);
    const b = hourBuckets[r.hour_local];
    b.input          += r.input;
    b.output         += r.output;
    b.cache_read     += r.cache_read;
    b.cache_creation += r.cache_creation;
    b.turns          += r.turns;
  }
  const nDays = Math.max(daysWithData.size, 1);
  const hourHistogram = hourBuckets.map(b => ({
    hour:           b.hour,
    input:          b.input / nDays,
    output:         b.output / nDays,
    cache_read:     b.cache_read / nDays,
    cache_creation: b.cache_creation / nDays,
    turns:          b.turns / nDays,
  }));
```

Then, near the end of `applyFilter` where the renderers are called (around line 576), add a call to the new renderer:

```javascript
  renderHourHistogram(hourHistogram);
```

Place this call immediately after `renderDailyChart(daily);` so the two time charts render in sequence.

- [ ] **Step 10.3: Add the renderer function**

Add this function immediately after `renderDailyChart` (around line 631):

```javascript
function renderHourHistogram(hourly) {
  const ctx = document.getElementById('chart-hour-histogram').getContext('2d');
  if (charts.hourHistogram) charts.hourHistogram.destroy();
  charts.hourHistogram = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: hourly.map(h => String(h.hour).padStart(2, '0') + ':00'),
      datasets: [
        { label: 'Input',          data: hourly.map(h => h.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: hourly.map(h => h.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: hourly.map(h => h.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: hourly.map(h => h.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4' }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}
```

- [ ] **Step 10.4: Smoke test**

Run: `python cli.py dashboard`
Expected: dashboard opens. Below the daily chart you should see "Usage by Hour of Day" with 24 stacked bars. Verify:
- The hours labeled 00:00 to 23:00
- The bars respond to range and model filter changes
- The hours shown are Chicago local time (compare with a turn you know happened at a specific UTC time)

Stop with Ctrl+C.

- [ ] **Step 10.5: Commit**

```bash
git add dashboard.py
git commit -m "feat(dashboard): add hour-of-day histogram chart

New stacked bar chart showing averaged tokens per hour (0-23) in
America/Chicago. Averages across the distinct days in the selected
range (days with no data don't count toward the average, so sparse
ranges aren't artificially flattened). Uses the same token color
palette as the existing daily chart.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Hourly timeline chart

**Files:**
- Modify: `dashboard.py` HTML template (add aggregation + renderer; chart card was added in Task 10)

**What:** Precise per-hour timeline — one stacked bar per (day_local, hour_local) pair in the selected range, sorted chronologically. When range > 14 days, horizontal scrolling kicks in via the `overflow-x: auto` already on the wrapper.

- [ ] **Step 11.1: Add the hourly timeline aggregation to `applyFilter`**

In `applyFilter`, immediately after the `hourHistogram` computation added in Task 10, add:

```javascript
  // ── Hourly timeline: one bar per (day, hour) in chronological order ──
  const timelineMap = {};  // "YYYY-MM-DD HH" -> bucket
  for (const r of filteredHourly) {
    const key = r.day_local + ' ' + String(r.hour_local).padStart(2, '0');
    if (!timelineMap[key]) {
      timelineMap[key] = {
        key, day: r.day_local, hour: r.hour_local,
        input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0,
      };
    }
    const b = timelineMap[key];
    b.input          += r.input;
    b.output         += r.output;
    b.cache_read     += r.cache_read;
    b.cache_creation += r.cache_creation;
    b.turns          += r.turns;
  }
  const hourTimeline = Object.values(timelineMap).sort((a, b) => a.key.localeCompare(b.key));
```

Then add the renderer call immediately after `renderHourHistogram(hourHistogram);`:

```javascript
  renderHourTimeline(hourTimeline);
```

- [ ] **Step 11.2: Add the renderer function**

Add this function immediately after `renderHourHistogram`:

```javascript
function renderHourTimeline(timeline) {
  const ctx = document.getElementById('chart-hour-timeline').getContext('2d');
  if (charts.hourTimeline) charts.hourTimeline.destroy();
  if (!timeline.length) { charts.hourTimeline = null; return; }
  // Compact label: "MM-DD HH" (e.g. "04-10 15")
  const labels = timeline.map(b => b.day.slice(5) + ' ' + String(b.hour).padStart(2, '0'));
  // Scale canvas width for many bars (approx 12px per bar)
  const canvas = document.getElementById('chart-hour-timeline');
  const minWidth = Math.max(800, timeline.length * 12);
  canvas.style.width = minWidth + 'px';

  charts.hourTimeline = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Input',          data: timeline.map(b => b.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: timeline.map(b => b.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: timeline.map(b => b.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: timeline.map(b => b.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        tooltip: {
          callbacks: {
            title: items => {
              if (!items.length) return '';
              const b = timeline[items[0].dataIndex];
              return b.day + ' ' + String(b.hour).padStart(2, '0') + ':00 CT';
            }
          }
        }
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxRotation: 0, autoSkip: true, autoSkipPadding: 20 }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}
```

- [ ] **Step 11.3: Smoke test**

Run: `python cli.py dashboard`
Expected: dashboard opens. Below the hour histogram you should see "Hourly Timeline" with one bar per (day, hour) pair in the selected range. Verify:
- Narrow ranges (e.g. 7d) show a compact chart
- Wider ranges (30d, 90d, all) scroll horizontally inside the card
- Tooltip shows the full day + hour on hover
- Chart updates when filters change

Stop with Ctrl+C.

- [ ] **Step 11.4: Commit**

```bash
git add dashboard.py
git commit -m "feat(dashboard): add hourly timeline chart

New stacked bar chart showing one bar per (day, hour) in the selected
range, sorted chronologically. Canvas width scales with bar count,
and the wrapper div already has overflow-x: auto from Task 10, so
wide ranges scroll horizontally. Tooltip shows the full day+hour
in Central Time for each bar.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Peak-band Chart.js plugin

**Files:**
- Modify: `dashboard.py` HTML template (add plugin, register it, attach to both hourly charts)

**What:** A reusable Chart.js plugin that draws translucent rectangles in the chart area based on `peakBands` converted from their source timezone to America/Chicago. Applied to both the hour histogram and the hourly timeline.

- [ ] **Step 12.1: Add state for peak bands**

In the state block (around the existing `let customFrom = null;` you added in Task 9), add:

```javascript
let peakBands = [];
```

- [ ] **Step 12.2: Populate peak bands on data load**

In `loadData` (around line 853), find the `rawData = d;` line and add immediately after it:

```javascript
    peakBands = d.peak_bands || [];
```

- [ ] **Step 12.3: Add the timezone conversion helper**

Add this function in the `// ── Time range` section (around line 367), before `getRangeCutoff`:

```javascript
// Convert a peak band to viewer-local (Chicago) hour range for a given day.
// Uses Intl.DateTimeFormat to do the cross-timezone conversion without a library.
// Returns {start: float, end: float} in decimal hours (viewer time), or null if
// the band doesn't apply to the given day-of-week.
function convertBandForDay(band, dayStr) {
  // dayStr = 'YYYY-MM-DD' in viewer time. Determine its day-of-week in viewer tz.
  const [y, m, d] = dayStr.split('-').map(Number);
  // Create a date at noon viewer time to avoid DST edge cases for day-of-week
  const localNoon = new Date(Date.UTC(y, m-1, d, 18, 0, 0));  // 12:00 CDT ≈ 18:00 UTC; CST ≈ 18:00 UTC too
  const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const dowViewer = dayNames[new Date(localNoon).getUTCDay()];
  if (!band.days.map(d => d.slice(0,3)).includes(dowViewer)) return null;

  // Parse band start/end in the band's timezone, convert to viewer time.
  const [sh, sm] = band.start.split(':').map(Number);
  const [eh, em] = band.end.split(':').map(Number);

  // Build a Date representing band.start on dayStr in the band's timezone.
  // Technique: format a reference UTC time in the band's timezone, compute offset,
  // then apply offset to get the UTC moment of band.start on dayStr.
  const bandStartUTC = zonedTimeToUTC(y, m, d, sh, sm, band.timezone);
  const bandEndUTC   = zonedTimeToUTC(y, m, d, eh, em, band.timezone);

  // Convert those UTC moments to Chicago local hours (decimal)
  const startLocal = utcToViewerHour(bandStartUTC, 'America/Chicago');
  const endLocal   = utcToViewerHour(bandEndUTC,   'America/Chicago');
  return { start: startLocal, end: endLocal };
}

// Given Y/M/D and H/M in `tz`, return the corresponding UTC timestamp (Date).
// Uses a two-pass approximation: first assume the time IS UTC, then measure the
// tz offset at that instant, then correct.
function zonedTimeToUTC(y, mo, d, h, mi, tz) {
  // First guess: treat h:mi as UTC
  const guess = new Date(Date.UTC(y, mo-1, d, h, mi, 0));
  // Format the guess in the target tz, measure the delta
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(guess);
  const p = Object.fromEntries(parts.filter(x => x.type !== 'literal').map(x => [x.type, parseInt(x.value, 10)]));
  // p now represents what the guess looks like in tz. Compute the offset.
  const asUTCOfTZ = Date.UTC(p.year, p.month-1, p.day, p.hour === 24 ? 0 : p.hour, p.minute, p.second);
  const offsetMs = asUTCOfTZ - guess.getTime();
  return new Date(guess.getTime() - offsetMs);
}

// Convert a UTC Date to a decimal hour in viewer tz (e.g. 7.5 for 7:30am).
function utcToViewerHour(utcDate, tz) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour12: false,
    hour: '2-digit', minute: '2-digit',
  }).formatToParts(utcDate);
  const p = Object.fromEntries(parts.filter(x => x.type !== 'literal').map(x => [x.type, parseInt(x.value, 10)]));
  const hour = p.hour === 24 ? 0 : p.hour;
  return hour + (p.minute / 60);
}
```

- [ ] **Step 12.4: Add the Chart.js plugin**

Immediately after the `MODEL_COLORS` constant (around line 365), add:

```javascript
// Chart.js plugin that draws peak-hour bands in the chart area.
// Each chart that wants bands passes options.peakBandMode: 'histogram' or 'timeline'.
const peakBandPlugin = {
  id: 'peakBands',
  beforeDatasetsDraw(chart, args, pluginOpts) {
    if (!peakBands.length) return;
    const mode = pluginOpts.mode;
    const ctx  = chart.ctx;
    const xAxis = chart.scales.x;
    const yAxis = chart.scales.y;
    if (!xAxis || !yAxis) return;

    ctx.save();
    ctx.fillStyle = 'rgba(217,119,87,0.10)';

    if (mode === 'histogram') {
      // Histogram x-axis is 24 labels "00:00".."23:00". Draw one band for a
      // Monday (or any weekday) converted to viewer time.
      const refDay = getRecentWeekday();
      for (const band of peakBands) {
        const range = convertBandForDay(band, refDay);
        if (!range) continue;
        const xStart = xAxis.getPixelForValue(String(Math.floor(range.start)).padStart(2,'0') + ':00');
        const xEnd   = xAxis.getPixelForValue(String(Math.floor(range.end)).padStart(2,'0') + ':00');
        ctx.fillRect(xStart, yAxis.top, xEnd - xStart, yAxis.bottom - yAxis.top);
      }
    } else if (mode === 'timeline') {
      // Timeline has one label per (day, hour). For each weekday in the range,
      // shade the band hours.
      const labels = chart.data.labels;  // ["04-10 15", ...]
      // Walk label index, shading bars whose hour falls inside any band on that day
      for (let i = 0; i < labels.length; i++) {
        const b = pluginOpts.timelineData[i];
        if (!b) continue;
        for (const band of peakBands) {
          const range = convertBandForDay(band, b.day);
          if (!range) continue;
          const hour = b.hour;
          if (hour >= Math.floor(range.start) && hour < Math.ceil(range.end)) {
            const x = xAxis.getPixelForValue(labels[i]);
            const barWidth = xAxis.getPixelForValue(labels[Math.min(i+1, labels.length-1)]) - x;
            ctx.fillRect(x - barWidth/2, yAxis.top, barWidth, yAxis.bottom - yAxis.top);
            break;
          }
        }
      }
    }
    ctx.restore();
  }
};

// Return a recent weekday (Mon-Fri) in YYYY-MM-DD format for histogram peak band rendering.
function getRecentWeekday() {
  const d = new Date();
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  return d.toISOString().slice(0, 10);
}

Chart.register(peakBandPlugin);
```

- [ ] **Step 12.5: Attach the plugin to the two charts**

In `renderHourHistogram`, update the `options` object to enable the plugin:

```javascript
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        peakBands: { mode: 'histogram' },
      },
      scales: {
        x: { ticks: { color: '#8892a4' }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
```

In `renderHourTimeline`, update the `options` object similarly:

```javascript
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        peakBands: { mode: 'timeline', timelineData: timeline },
        tooltip: {
          callbacks: {
            title: items => {
              if (!items.length) return '';
              const b = timeline[items[0].dataIndex];
              return b.day + ' ' + String(b.hour).padStart(2, '0') + ':00 CT';
            }
          }
        }
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxRotation: 0, autoSkip: true, autoSkipPadding: 20 }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
```

- [ ] **Step 12.6: Smoke test**

Run: `python cli.py dashboard`
Expected: dashboard opens. The hour histogram should show a translucent orange band roughly covering 07:00–13:00 (which is 05:00–11:00 Pacific converted to Chicago). The hourly timeline should show the same band repeating every 24 hours on weekdays only.

- Verify the band is absent on weekend rows in the timeline
- Verify editing `peak-hours.json` to empty bands (`"bands": []`) and refreshing causes the overlay to disappear

Stop with Ctrl+C.

- [ ] **Step 12.7: Commit**

```bash
git add dashboard.py
git commit -m "feat(dashboard): add peak-hour overlay Chart.js plugin

Reusable plugin that draws translucent orange bands in the chart area
based on peakBands shipped from /api/data. Uses Intl.DateTimeFormat
for cross-timezone conversion with no external libraries. Applied to
the hour-of-day histogram (single band) and the hourly timeline
(repeating band, weekdays only). Plugin gracefully no-ops when
peakBands is empty.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Sessions table title display + CSV export

**Files:**
- Modify: `dashboard.py` HTML template (`renderSessionsTable`, `exportSessionsCSV`)

**What:** The Project column already shows `s.project` which (after Task 8) is `title || project_name`. Add a hover tooltip showing the raw cwd-derived name when different. Update CSV export to include both `Title` and `Project (cwd-derived)` columns.

- [ ] **Step 13.1: Update `renderSessionsTable` to show tooltip**

In `renderSessionsTable` (around line 678), find the project cell:

```javascript
      <td>${esc(s.project)}</td>
```

Replace with:

```javascript
      <td title="${esc(s.project_raw || '')}">${esc(s.project)}</td>
```

- [ ] **Step 13.2: Update `exportSessionsCSV` to include Title and raw Project**

Find `exportSessionsCSV` (around line 818). Replace the whole function with:

```javascript
function exportSessionsCSV() {
  const header = ['Session', 'Title', 'Project (cwd-derived)', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.title || '', s.project_raw || s.project, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}
```

- [ ] **Step 13.3: Smoke test**

Run: `python cli.py dashboard`
Expected:
- Hovering over a project cell shows the raw cwd-derived name as a tooltip (visible when the title differs from the raw name)
- Clicking the CSV export button downloads a file with `Session, Title, Project (cwd-derived), ...` as the header
- Sessions without a title have empty Title cells in the CSV; project column shows the raw name

Stop with Ctrl+C.

- [ ] **Step 13.4: Commit**

```bash
git add dashboard.py
git commit -m "feat(dashboard): show title in sessions table with raw cwd tooltip

The Project column renders the title when present (already set by
the API), with a title attribute showing the raw cwd-derived name
on hover. CSV export now has separate Title and Project (cwd-derived)
columns so both are preserved regardless of which is displayed.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: Documentation updates

**Files:**
- Modify: `README.md` (add Windows desktop metadata section)
- Modify: `CHANGELOG.md` (add 2026-04-10 entry)

**What:** Document the new features so users know what to expect.

- [ ] **Step 14.1: Add desktop metadata section to README**

In `README.md`, find the "What this tracks" section (around line 16–28). Add a new subsection after the existing "Not captured" block:

```markdown
### Windows: Desktop app session metadata

On Windows, the scanner additionally reads session metadata from `%APPDATA%/Claude/claude-code-sessions/` (written by the Claude Desktop app). This enriches sessions with:

- **Title** — human-readable names like "Hourly checkin" or "Kb refresh" that replace the generic cwd-derived project name in the dashboard's Project column
- **Original cwd** — the working directory the desktop app recorded explicitly, shown as a hover tooltip

This feature is Windows-only. On macOS/Linux, the enrichment silently no-ops and sessions show their cwd-derived project name as before.

Token counts are not affected by enrichment — they come entirely from the JSONL files under `~/.claude/projects/`. Desktop metadata only adds labels.
```

Then add a new section at the bottom of the README (before the Files table):

```markdown
## Time-of-day view

The dashboard includes two charts that break usage down by hour of day in your local time (currently hardcoded to America/Chicago, DST-aware):

- **Usage by Hour of Day** — 24 stacked bars averaged across the days in your selected range. Useful for spotting patterns like "I always burn tokens at 9am."
- **Hourly Timeline** — one stacked bar per (day, hour) in the selected range, sorted chronologically. Wider ranges scroll horizontally. Useful for forensic investigation like "what happened yesterday at 3pm?"

Both charts show a translucent peak-hour overlay based on `peak-hours.json` in the repo root. The default reflects Anthropic's reported peak window (Mon–Fri 05:00–11:00 Pacific, March 2026 source). Edit the file and refresh the dashboard to change it.

## Custom date range

In addition to the preset 7d/30d/90d/All buttons, you can pick a custom From/To range using the date inputs at the top of the dashboard. Using the custom range deactivates the preset buttons; clicking any preset clears the custom dates. Range is persisted in the URL as `?from=YYYY-MM-DD&to=YYYY-MM-DD`.
```

- [ ] **Step 14.2: Add CHANGELOG entry**

In `CHANGELOG.md`, add a new entry at the top (above the existing 2026-04-09 entry):

```markdown
## 2026-04-10

- Add Windows desktop app session metadata enrichment — sessions now show titles like "Hourly checkin" instead of generic "Users/scott" when available
- Add two new columns to the `sessions` table: `title` and `original_cwd`
- Add time-of-day view in America/Chicago (DST-aware): averaged hourly histogram and precise hourly timeline
- Add custom date range picker (`From`/`To` inputs), mutually exclusive with preset buttons
- Add peak-hour visual overlay on hourly charts, driven by editable `peak-hours.json` config
- Dashboard API now ships `turns_by_hour_local`, `peak_bands`, and `viewer_timezone` fields
- CSV export of sessions now includes `Title` and `Project (cwd-derived)` as separate columns

```

- [ ] **Step 14.3: Run the full test suite one last time**

Run: `python -m unittest discover tests -v`
Expected: all tests pass (original 84 + 26 new = 110 total). The plan adds 6 tests beyond the spec's promised 20: 2 schema-evolution tests in Task 1 (covering the ALTER migration path), 2 defensive timezone tests in Task 5 (unparseable/None input), and 2 session-title fallback tests in Task 8 (covering spec section 4.3 which section 5.2 didn't list).

- [ ] **Step 14.4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: update README and CHANGELOG for time-lens features

Documents the new Windows desktop metadata enrichment, time-of-day
charts, custom date range, and peak-hour overlay. Users of other
platforms are explicitly told the metadata enrichment is a Windows-
only feature.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Self-review checklist

Before executing this plan, the implementer should verify:

- [ ] All 14 tasks committed cleanly with passing tests at each commit
- [ ] The full test suite (`python -m unittest discover tests`) passes
- [ ] The dashboard starts without errors: `python cli.py dashboard`
- [ ] The hour histogram shows data in the current selected range
- [ ] The hourly timeline scrolls horizontally when range > 14 days
- [ ] The peak band appears as a translucent orange overlay around 07:00–13:00 Chicago
- [ ] Sessions titled by desktop metadata show their titles (e.g., "Hourly checkin")
- [ ] Sessions without desktop metadata fall back to the cwd-derived project name
- [ ] Custom date range updates the URL and restores on reload
- [ ] CSV export contains both Title and Project (cwd-derived) columns
- [ ] `peak-hours.json` can be edited and the overlay updates on next page load
- [ ] README and CHANGELOG are up to date
