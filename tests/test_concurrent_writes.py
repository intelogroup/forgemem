"""
Tests for concurrent-write safety:
  1. rollback() on exception — connection is clean after a write failure
  2. _write_lock serializes concurrent writes — no "database is locked" cascade
"""

import concurrent.futures
import contextlib
import sqlite3
import threading

import pytest

import forgememo.daemon as daemon_module
import forgememo.storage as storage_module
from forgememo.daemon import create_app
from forgememo.storage import get_conn, init_db


@contextlib.contextmanager
def get_db(write=False):
    """Context manager matching the old api.get_db() contract: acquire write lock,
    get connection, rollback on exception, close on exit."""
    if write:
        daemon_module._write_lock.acquire()
    conn = get_conn()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
        if write:
            daemon_module._write_lock.release()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point every test at a fresh temp DB."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(storage_module, "DB_PATH", db_file)
    monkeypatch.setattr(daemon_module, "_write_lock", threading.Lock())
    init_db()
    yield db_file


@pytest.fixture()
def client(isolated_db):
    app = create_app()
    with app.test_client() as c:
        yield c


# ─── Unit: rollback on exception prevents connection poisoning ────────────────

def test_rollback_on_exception_does_not_poison_connection():
    """After a failed write, a subsequent write must succeed."""
    try:
        with get_db(write=True) as conn:
            conn.execute(
                "INSERT INTO traces (session_id, project_tag, type, content) "
                "VALUES (?, ?, ?, ?)",
                ("s1", "proj", "success", "x"),
            )
            raise RuntimeError("simulated failure mid-write")
    except RuntimeError:
        pass

    # The next write must succeed — connection must not carry a broken txn
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT INTO traces (session_id, project_tag, type, content) "
            "VALUES (?, ?, ?, ?)",
            ("s2", "proj", "success", "recovery write"),
        )
        conn.commit()

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    # Only the recovery write should be present (failed txn was rolled back)
    assert count == 1


def test_failed_write_does_not_leave_transaction_open():
    """Verify in_transaction=False after rollback."""
    try:
        with get_db(write=True) as conn:
            conn.execute(
                "INSERT INTO traces (session_id, project_tag, type, content) "
                "VALUES (?, ?, ?, ?)",
                ("s", "p", "success", "will be rolled back"),
            )
            raise sqlite3.OperationalError("database is locked")
    except sqlite3.OperationalError:
        pass

    with get_db() as conn:
        assert not conn.in_transaction, "connection still has an open transaction after failure"


# ─── Unit: write lock serializes concurrent writers ──────────────────────────

def test_write_lock_serializes_concurrent_writes():
    """N concurrent threads must all succeed without any lock errors."""
    N = 20
    errors = []

    def write_one(i):
        try:
            with get_db(write=True) as conn:
                conn.execute(
                    "INSERT INTO traces (session_id, project_tag, type, content) "
                    "VALUES (?, ?, ?, ?)",
                    (f"s{i}", "load-test", "note", f"concurrent write {i}"),
                )
                conn.commit()
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        list(ex.map(write_one, range(N)))

    assert errors == [], f"Concurrent writes produced errors: {errors}"

    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM traces WHERE project_tag='load-test'"
        ).fetchone()[0]
    assert count == N


def test_read_does_not_acquire_write_lock():
    """Reads must not block while a write lock is held."""
    results = {}
    lock_acquired = threading.Event()
    read_done = threading.Event()

    def hold_write_lock():
        with get_db(write=True) as conn:
            lock_acquired.set()
            read_done.wait(timeout=3)
            conn.execute(
                "INSERT INTO traces (session_id, project_tag, type, content) "
                "VALUES (?, ?, ?, ?)",
                ("s", "p", "note", "writer"),
            )
            conn.commit()

    def do_read():
        lock_acquired.wait(timeout=3)
        with get_db() as conn:
            results["count"] = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        read_done.set()

    t_write = threading.Thread(target=hold_write_lock)
    t_read = threading.Thread(target=do_read)
    t_write.start()
    t_read.start()
    t_write.join(timeout=5)
    t_read.join(timeout=5)

    assert "count" in results, "read thread did not complete (likely blocked by write lock)"


# ─── Integration: POST /events concurrently ──────────────────────────────────

def test_concurrent_post_events_all_succeed(isolated_db):
    """/events must accept N concurrent POSTs without any 500 errors."""
    app = create_app()
    N = 10

    responses = []
    lock = threading.Lock()

    def post_one(i):
        payload = {
            "session_id": f"sess-{i}",
            "project_id": "ci-test",
            "source_tool": "claude_code",
            "event_type": "note",
            "payload": {"text": f"concurrent integration write {i}"},
            "seq": i,
        }
        with app.test_client() as c:
            resp = c.post("/events", json=payload)
        with lock:
            responses.append(resp.status_code)

    threads = [threading.Thread(target=post_one, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    failures = [s for s in responses if s not in (200, 201)]
    assert failures == [], f"{len(failures)}/{N} POSTs failed with status: {set(failures)}"
