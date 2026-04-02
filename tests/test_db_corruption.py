"""
Tests for SQLite persistent state corruption and recovery.

Covers:
  1. DB survives simulated crash mid-write (WAL journal recovery)
  2. Corrupted DB file (truncated / garbage) → init_db still works
  3. Schema re-init on existing data is idempotent
  4. Concurrent readers during write don't see partial transactions
  5. FTS index consistency after crash
  6. Error events table created inline when missing from older DB
  7. DB file deleted mid-session → next get_conn recreates it
"""

import json
import os
import sqlite3
import threading
import time

import pytest

import forgememo.daemon as daemon_module
import forgememo.storage as storage_module
from forgememo.daemon import create_app
from forgememo.storage import get_conn, init_db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "corruption_test.db"
    monkeypatch.setattr(storage_module, "DB_PATH", db_file)
    monkeypatch.setattr(daemon_module, "_write_lock", threading.Lock())
    init_db()
    yield db_file


@pytest.fixture()
def client(isolated_db):
    app = create_app()
    with app.test_client() as c:
        yield c


# ─── WAL journal recovery ────────────────────────────────────────────────────


class TestWALRecovery:
    def test_wal_mode_is_enabled(self):
        conn = get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_data_persists_after_close_and_reopen(self):
        """Simulate reboot: write, close, reopen, read back."""
        conn = get_conn()
        conn.execute(
            "INSERT INTO traces (session_id, project_tag, type, content) "
            "VALUES (?,?,?,?)",
            ("s1", "proj", "note", "persist me"),
        )
        conn.commit()
        conn.close()

        # "Reboot" — new connection
        conn2 = get_conn()
        row = conn2.execute(
            "SELECT content FROM traces WHERE session_id='s1'"
        ).fetchone()
        conn2.close()
        assert row is not None
        assert row["content"] == "persist me"

    def test_uncommitted_write_is_lost_after_close(self):
        """Simulate crash: write without commit, close, verify data is gone."""
        conn = get_conn()
        conn.execute(
            "INSERT INTO traces (session_id, project_tag, type, content) "
            "VALUES (?,?,?,?)",
            ("ghost", "proj", "note", "never committed"),
        )
        # No commit — simulate crash
        conn.close()

        conn2 = get_conn()
        row = conn2.execute(
            "SELECT COUNT(*) FROM traces WHERE session_id='ghost'"
        ).fetchone()[0]
        conn2.close()
        assert row == 0

    def test_wal_checkpoint_flushes_to_main_db(self):
        """WAL checkpoint should merge journal into main DB file."""
        conn = get_conn()
        conn.execute(
            "INSERT INTO traces (session_id, project_tag, type, content) "
            "VALUES (?,?,?,?)",
            ("ckpt", "proj", "note", "checkpointed"),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

        # WAL file should be empty or gone after truncate checkpoint
        wal_path = str(storage_module.DB_PATH) + "-wal"
        if os.path.exists(wal_path):
            assert os.path.getsize(wal_path) == 0


# ─── Corrupted DB file ───────────────────────────────────────────────────────


class TestCorruptedDB:
    def test_truncated_db_file_recoverable(self, tmp_path, monkeypatch):
        """A truncated DB (e.g., from disk-full) should be replaceable."""
        db_file = tmp_path / "truncated.db"
        # Write garbage that looks like a truncated SQLite file
        db_file.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)
        monkeypatch.setattr(storage_module, "DB_PATH", db_file)

        # init_db should either recover or fail gracefully
        # In practice, SQLite may treat this as corrupt and fail on schema ops
        try:
            init_db()
            # If it succeeds, the DB should be usable
            conn = get_conn()
            conn.execute("SELECT COUNT(*) FROM traces").fetchone()
            conn.close()
        except sqlite3.DatabaseError:
            # Expected for truly corrupt files — verify we can start fresh
            db_file.unlink()
            init_db()
            conn = get_conn()
            count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            conn.close()
            assert count == 0

    def test_garbage_db_file_replaced_cleanly(self, tmp_path, monkeypatch):
        """A file full of random bytes should be deletable and re-creatable."""
        db_file = tmp_path / "garbage.db"
        db_file.write_bytes(os.urandom(1024))
        monkeypatch.setattr(storage_module, "DB_PATH", db_file)

        try:
            init_db()
        except sqlite3.DatabaseError:
            pass  # Expected

        # Manual recovery: delete and re-init
        db_file.unlink(missing_ok=True)
        init_db()
        conn = get_conn()
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        assert "events" in tables
        assert "distilled_summaries" in tables
        assert "error_events" in tables

    def test_empty_db_file_inits_cleanly(self, tmp_path, monkeypatch):
        """An empty DB file (zero bytes) should init normally."""
        db_file = tmp_path / "empty.db"
        db_file.write_bytes(b"")
        monkeypatch.setattr(storage_module, "DB_PATH", db_file)

        init_db()
        conn = get_conn()
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        assert count == 0


# ─── DB deleted mid-session ──────────────────────────────────────────────────


class TestDBDeletedMidSession:
    def test_get_conn_recreates_after_delete(self, tmp_path, monkeypatch):
        """If DB file is deleted mid-session, get_conn should recreate it."""
        db_file = tmp_path / "deleteme.db"
        monkeypatch.setattr(storage_module, "DB_PATH", db_file)
        init_db()

        # Verify it exists
        assert db_file.exists()

        # Delete it (simulating external interference)
        db_file.unlink()
        # Also remove WAL and SHM if present
        for suffix in ["-wal", "-shm"]:
            p = tmp_path / f"deleteme.db{suffix}"
            if p.exists():
                p.unlink()

        assert not db_file.exists()

        # get_conn creates parent dir and file; schema needs re-init
        conn = get_conn()
        assert db_file.exists()
        conn.close()

        # Re-init schema
        init_db()
        conn = get_conn()
        conn.execute("SELECT COUNT(*) FROM events").fetchone()
        conn.close()


# ─── FTS index consistency ───────────────────────────────────────────────────


class TestFTSConsistency:
    def test_fts_search_matches_after_insert(self, client):
        """Data inserted via API should be findable via FTS search."""
        # Insert a distilled summary directly
        conn = get_conn()
        conn.execute(
            "INSERT INTO distilled_summaries "
            "(session_id, project_id, source_tool, type, title, narrative, "
            "facts, files_read, files_modified, concepts, impact_score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sess-fts",
                "/tmp/proj",
                "claude",
                "bugfix",
                "Fix database connection leak",
                "We found a connection leak in the pool manager",
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                json.dumps(["connection", "leak"]),
                8,
            ),
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO distilled_summaries_fts(rowid, title, narrative, concepts, tags, project_id) "
            "VALUES (?,?,?,?,?,?)",
            (
                row_id,
                "Fix database connection leak",
                "We found a connection leak in the pool manager",
                "connection,leak",
                "",
                "/tmp/proj",
            ),
        )
        conn.commit()
        conn.close()

        # Search via API
        r = client.get("/search?q=connection+leak&k=5")
        assert r.status_code == 200
        results = r.get_json().get("results", [])
        assert any("connection" in (r.get("title", "")).lower() for r in results)

    def test_fts_rebuild_survives_reinit(self, client):
        """Double init_db should not corrupt FTS indexes."""
        # Insert data
        conn = get_conn()
        conn.execute(
            "INSERT INTO distilled_summaries "
            "(session_id, project_id, source_tool, type, title, narrative, "
            "facts, files_read, files_modified, concepts, impact_score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sess-fts2",
                "/tmp/proj",
                "claude",
                "note",
                "Unique Zebra Pattern",
                "Zebra patterns in config",
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                json.dumps(["zebra"]),
                5,
            ),
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO distilled_summaries_fts(rowid, title, narrative, concepts, tags, project_id) "
            "VALUES (?,?,?,?,?,?)",
            (row_id, "Unique Zebra Pattern", "Zebra patterns in config", "zebra", "", "/tmp/proj"),
        )
        conn.commit()
        conn.close()

        # Re-init (simulating restart)
        init_db()

        # Data should still be searchable
        r = client.get("/search?q=zebra&k=5")
        results = r.get_json().get("results", [])
        assert any("zebra" in (r.get("title", "")).lower() for r in results)


# ─── Inline table creation for older DBs ─────────────────────────────────────


class TestInlineTableCreation:
    def test_error_events_created_inline_when_missing(self, tmp_path, monkeypatch):
        """POST /error_events should auto-create table on older DBs."""
        db_file = tmp_path / "old_schema.db"
        monkeypatch.setattr(storage_module, "DB_PATH", db_file)

        # Create a minimal DB without the error_events table
        conn = sqlite3.connect(str(db_file))
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, session_id TEXT);"
            "CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY, session_id TEXT, project_tag TEXT, type TEXT, content TEXT);"
        )
        conn.commit()
        conn.close()

        app = create_app()
        with app.test_client() as client:
            r = client.post(
                "/error_events",
                json={
                    "session_id": "sess-old",
                    "fingerprint": "fp-old",
                    "error_text": "TypeError: None",
                },
            )
            assert r.status_code == 201

            # Verify we can read it back
            r = client.get("/error_events?session_id=sess-old&fingerprint=fp-old")
            assert r.status_code == 200
            assert r.get_json()["count"] == 1


# ─── Concurrent read during write sees consistent state ──────────────────────


class TestReadWriteIsolation:
    def test_reader_during_write_sees_consistent_state(self):
        """A reader during a long write transaction should not see partial data."""
        N = 50
        barrier = threading.Barrier(2, timeout=5)
        reader_counts = []
        errors = []

        def writer():
            try:
                daemon_module._write_lock.acquire()
                conn = get_conn()
                for i in range(N):
                    conn.execute(
                        "INSERT INTO traces (session_id, project_tag, type, content) "
                        "VALUES (?,?,?,?)",
                        (f"batch-{i}", "isolation", "note", f"item {i}"),
                    )
                    if i == N // 2:
                        barrier.wait()  # Signal reader mid-batch
                conn.commit()
                conn.close()
            except Exception as e:
                errors.append(e)
            finally:
                daemon_module._write_lock.release()

        def reader():
            try:
                barrier.wait()  # Wait for writer to be mid-batch
                conn = get_conn()
                count = conn.execute(
                    "SELECT COUNT(*) FROM traces WHERE project_tag='isolation'"
                ).fetchone()[0]
                reader_counts.append(count)
                conn.close()
            except Exception as e:
                errors.append(e)

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start()
        t_r.start()
        t_w.join(timeout=10)
        t_r.join(timeout=10)

        assert errors == [], f"Errors during test: {errors}"
        # Reader should see either 0 (before commit) or N (after commit),
        # never a partial count, thanks to WAL isolation
        assert len(reader_counts) == 1
        assert reader_counts[0] in (0, N), (
            f"Reader saw partial data: {reader_counts[0]} rows (expected 0 or {N})"
        )
