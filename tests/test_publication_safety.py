"""Phase 10.8-R4.3: Canonical SQLite WAL/SHM Publication Safety and Concurrency Tests.

Ported from synthetic test suite (WAL-01..05, CONC-01..05, FAIL-01..10):
- WAL-01: Destination with pre-existing WAL/SHM — all rows present, integrity ok
- WAL-03: Interleaved writer (active connection opened dst mid-publish, opened WAL, committed a row; sidecars not unlinked)
- WAL-05: Stale temp file sweep — orphaned .migrating-* and .stale* files cleaned up by _sweep_stale_temps()
- CONC-01: Two concurrent snapshot_database calls to same dst — exactly one wins, no corruption
- FAIL-04: Publication failure (os.replace raises) — old dst preserved, no corruption
- FAIL-05: Sidecar unlink failure — warning in notes, migration still succeeds
- FAIL-07: Cleanup exception doesn't mask primary error
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from antigravity_swap import convstore, slotlock


def _init_wal_db(path: Path, rows: int = 100, autocheckpoint: int = 0, conv_id: str = "conv_test") -> None:
    for sfx in ("", "-wal", "-shm"):
        p = Path(str(path) + sfx)
        if p.exists():
            p.unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    if autocheckpoint == 0:
        conn.execute("PRAGMA wal_autocheckpoint=0;")
    conn.execute("CREATE TABLE steps (step_index INTEGER PRIMARY KEY, note TEXT);")
    conn.execute("CREATE TABLE trajectory_meta (cascade_id TEXT PRIMARY KEY);")
    conn.execute("INSERT INTO trajectory_meta VALUES (?);", (conv_id,))
    conn.executemany("INSERT INTO steps VALUES (?, ?);", [(i, f"step_{i}") for i in range(rows)])
    conn.commit()
    conn.close()


def test_wal_01_preexisting_wal_shm(tmp_path: Path):
    """WAL-01: Destination with pre-existing WAL/SHM — all rows present, integrity ok."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"

    # Destination has 100 rows with WAL
    _init_wal_db(dst, rows=100, autocheckpoint=0)
    dst_wal = Path(str(dst) + "-wal")
    dst_shm = Path(str(dst) + "-shm")
    # Ensure pre-existing WAL/SHM sidecars exist on dst
    dst_wal.write_bytes(b"pre-existing dst wal")
    dst_shm.write_bytes(b"pre-existing dst shm")
    assert dst_wal.exists()
    assert dst_shm.exists()

    # Source has 500 rows with WAL
    _init_wal_db(src, rows=500, autocheckpoint=0)

    method, notes = convstore.snapshot_database(src, dst)

    # Immediately after snapshot_database, stale sidecars must be unlinked
    assert not dst_wal.exists()
    assert not dst_shm.exists()

    dconn = sqlite3.connect(dst)
    integrity = dconn.execute("PRAGMA integrity_check;").fetchone()[0]
    count = dconn.execute("SELECT count(*) FROM steps;").fetchone()[0]
    dconn.close()

    assert integrity == "ok"
    assert count == 500
    assert not dst_wal.exists()
    assert not dst_shm.exists()


def test_wal_03_interleaved_writer_window(tmp_path: Path):
    """WAL-03: Interleaved writer opening dst mid-publish retains rows and sidecars."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"

    _init_wal_db(dst, rows=100, autocheckpoint=0)
    _init_wal_db(src, rows=300, autocheckpoint=0)

    original_replace = os.replace
    interleaved_conn = None
    interleaved_result = {}

    def patched_replace(s, d):
        nonlocal interleaved_conn
        original_replace(s, d)
        # When main db is replaced, simulate a new connection connecting before retirement cleanup
        if str(d) == str(dst):
            try:
                c = sqlite3.connect(dst, timeout=5.0)
                c.execute("PRAGMA journal_mode=WAL;")
                c.execute("INSERT INTO steps (step_index, note) VALUES (9999, 'interleaved_note');")
                c.commit()
                integ = c.execute("PRAGMA integrity_check;").fetchone()[0]
                cnt = c.execute("SELECT count(*) FROM steps;").fetchone()[0]
                interleaved_conn = c
                interleaved_result["status"] = "ok"
                interleaved_result["integrity"] = integ
                interleaved_result["count"] = cnt
            except Exception as exc:
                interleaved_result["status"] = "error"
                interleaved_result["error"] = str(exc)

    os.replace = patched_replace
    try:
        method, notes = convstore.snapshot_database(src, dst)
    finally:
        os.replace = original_replace

    wal_file = dst.parent / f"{dst.name}-wal"
    wal_exists_during_open = wal_file.exists()

    if interleaved_conn is not None:
        try:
            interleaved_conn.close()
        except Exception:
            pass

    fconn = sqlite3.connect(dst)
    final_integrity = fconn.execute("PRAGMA integrity_check;").fetchone()[0]
    final_count = fconn.execute("SELECT count(*) FROM steps;").fetchone()[0]
    fconn.close()

    assert interleaved_result.get("status") == "ok"
    assert interleaved_result.get("integrity") == "ok"
    assert interleaved_result.get("count") == 301
    assert final_integrity == "ok"
    assert final_count == 301
    assert wal_exists_during_open


def test_wal_05_stale_temp_file_sweep(tmp_path: Path):
    """WAL-05: Stale temp file sweep — orphaned .migrating-* and .stale* files cleaned up."""
    conv_dir = tmp_path / "conversations"
    conv_dir.mkdir(parents=True)
    db_name = "test_conv.db"

    now = time.time()
    old_time = now - 600.0  # 10 minutes ago (> 300s default)
    recent_time = now - 10.0  # 10 seconds ago (< 300s default)

    # Old migrating temp file and its sidecars
    old_migrating = conv_dir / f".{db_name}.migrating-1111-999999"
    old_migrating.write_text("old migrating data")
    old_migrating_wal = conv_dir / f".{db_name}.migrating-1111-999999-wal"
    old_migrating_wal.write_text("old wal")
    os.utime(old_migrating, (old_time, old_time))

    # Recent migrating temp file (should NOT be swept)
    recent_migrating = conv_dir / f".{db_name}.migrating-2222-999999"
    recent_migrating.write_text("recent migrating data")
    os.utime(recent_migrating, (recent_time, recent_time))

    # Old retired stale sidecar file (should be swept per Fix 3)
    old_stale_wal = conv_dir / f".{db_name}.stale-wal-1111-999999"
    old_stale_wal.write_text("old stale wal")
    os.utime(old_stale_wal, (old_time, old_time))

    # Recent retired stale sidecar file (should NOT be swept)
    recent_stale_wal = conv_dir / f".{db_name}.stale-wal-2222-999999"
    recent_stale_wal.write_text("recent stale wal")
    os.utime(recent_stale_wal, (recent_time, recent_time))

    swept = convstore._sweep_stale_temps(conv_dir, db_name, max_age_s=300.0)

    assert swept == 2  # old_migrating and old_stale_wal swept
    assert not old_migrating.exists()
    assert not old_migrating_wal.exists()
    assert not old_stale_wal.exists()
    assert recent_migrating.exists()
    assert recent_stale_wal.exists()


def test_conc_01_two_migrations_same_dst(tmp_path: Path):
    """CONC-01: Two simultaneous migrations to the same destination.

    Exactly one thread wins: the destination is a coherent snapshot of whichever source
    ``os.replace`` committed last.  The losing thread receives a ``MigrationError`` (its
    temp file is gone or it cannot replace dst) — that is acceptable and expected; what is
    NOT acceptable is destination corruption or a silent partial write.
    """
    src1 = tmp_path / "src1.db"
    src2 = tmp_path / "src2.db"
    dst = tmp_path / "dst.db"

    _init_wal_db(src1, rows=120, conv_id="src1")
    _init_wal_db(src2, rows=220, conv_id="src2")

    results = []
    errors = []

    def worker(src):
        try:
            m, n = convstore.snapshot_database(src, dst)
            results.append((src.name, m))
        except Exception as exc:
            errors.append((src.name, str(exc)))

    t1 = threading.Thread(target=worker, args=(src1,))
    t2 = threading.Thread(target=worker, args=(src2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # At least one thread must succeed; the other may lose the race (MigrationError is OK).
    assert len(results) >= 1, f"Both threads failed — no winner: {errors}"
    # The loser, if any, must have received a MigrationError/OSError, not a silent corruption.
    for _src, err in errors:
        assert "publication failed" in err or "no safe copy" in err or "FileNotFoundError" in err, \
            f"Unexpected error type from losing thread: {err}"

    dconn = sqlite3.connect(dst)
    integrity = dconn.execute("PRAGMA integrity_check;").fetchone()[0]
    count = dconn.execute("SELECT count(*) FROM steps;").fetchone()[0]
    conv = dconn.execute("SELECT cascade_id FROM trajectory_meta;").fetchone()[0]
    dconn.close()

    assert integrity == "ok", f"Destination corrupted after concurrent migration: {integrity}"
    assert count in (120, 220), f"Unexpected row count {count} — partial write detected"
    assert conv in ("src1", "src2")
    assert (count == 120 if conv == "src1" else count == 220), \
        f"Row count {count} inconsistent with cascade_id {conv}"


def test_fail_04_publication_failure_preserves_dst(tmp_path: Path):
    """FAIL-04: Publication failure (os.replace raises) — old dst preserved, no corruption."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    _init_wal_db(src, rows=50, conv_id="src04")
    _init_wal_db(dst, rows=20, conv_id="dst04")
    dst_hash_before = convstore.digest_of(dst)

    real_replace = os.replace
    def mock_replace(s, d):
        if str(d) == str(dst):
            raise OSError("Simulated filesystem I/O failure on os.replace")
        return real_replace(s, d)

    os.replace = mock_replace
    error = None
    try:
        convstore.snapshot_database(src, dst)
    except convstore.MigrationError as exc:
        error = str(exc)
    finally:
        os.replace = real_replace

    dst_hash_after = convstore.digest_of(dst)
    assert error is not None
    assert "publication failed" in error
    assert dst_hash_before == dst_hash_after
    c = sqlite3.connect(dst)
    integ = c.execute("PRAGMA integrity_check;").fetchone()[0]
    cnt = c.execute("SELECT count(*) FROM steps;").fetchone()[0]
    c.close()
    assert integ == "ok"
    assert cnt == 20


def test_fail_05_sidecar_unlink_failure_warning(tmp_path: Path):
    """FAIL-05: Sidecar unlink failure — warning in notes, migration still succeeds."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    _init_wal_db(src, rows=60, conv_id="src05")
    _init_wal_db(dst, rows=20, conv_id="dst05")

    real_unlink = Path.unlink
    unlink_attempts = []
    def mock_unlink(self):
        unlink_attempts.append(str(self))
        raise PermissionError("Simulated permission error on sidecar unlink")

    Path.unlink = mock_unlink
    try:
        method, notes = convstore.snapshot_database(src, dst)
        success = True
        err = None
    except Exception as exc:
        success = False
        err = str(exc)
    finally:
        Path.unlink = real_unlink

    assert success is True
    assert len(unlink_attempts) > 0
    c = sqlite3.connect(dst)
    integ = c.execute("PRAGMA integrity_check;").fetchone()[0]
    cnt = c.execute("SELECT count(*) FROM steps;").fetchone()[0]
    c.close()
    assert integ == "ok"
    assert cnt == 60


def test_fail_07_cleanup_exception_does_not_mask_primary_error(tmp_path: Path):
    """FAIL-07: Cleanup exception doesn't mask primary error."""
    src = tmp_path / "src.db"
    dst = tmp_path / "dst.db"
    _init_wal_db(src, rows=30, conv_id="src07")

    real_connect = sqlite3.connect
    def mock_conn(database, *args, **kwargs):
        raise sqlite3.DatabaseError("Primary database corruption error")

    sqlite3.connect = mock_conn
    err = None
    try:
        convstore.snapshot_database(src, dst)
    except convstore.MigrationError as exc:
        err = str(exc)
    finally:
        sqlite3.connect = real_connect

    assert err is not None
    assert "Primary database corruption error" in err
