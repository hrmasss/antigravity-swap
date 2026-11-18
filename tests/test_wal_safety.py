"""WAL safety: a conversation that still has uncheckpointed turns in its ``-wal`` must arrive
intact on the destination account.

The upstream implementation copied only ``<id>.db``, so a turn committed to the WAL but not yet
checkpointed was silently dropped. These tests build a real SQLite database in WAL mode, keep a
writer's connection open so the WAL stays live, and assert that every committed row reaches the
destination.

Fixture semantics: ``checkpointed`` rows are written, committed and folded into the main database
with ``wal_checkpoint(FULL)``; ``in_wal`` rows are committed afterwards and left in the ``-wal``.
That is exactly the shape a running agy leaves behind.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from antigravity_swap import agy, convstore

CONV = "11111111-2222-3333-4444-555555555555"


def _cli(gdir: Path) -> Path:
    d = gdir / "antigravity-cli"
    (d / "conversations").mkdir(parents=True, exist_ok=True)
    return d


def _make_conversation(gdir: Path, checkpointed: int = 2, in_wal: int = 3):
    """Create a WAL-mode conversation db; leave ``in_wal`` committed rows inside the WAL.

    Returns ``(db_path, writer_connection)``. Keep the connection open: closing it would make
    SQLite checkpoint and erase the very condition under test.
    """
    cli = _cli(gdir)
    db = cli / "conversations" / f"{CONV}.db"
    w = sqlite3.connect(db)
    w.execute("pragma journal_mode=wal")
    w.execute("pragma wal_autocheckpoint=0")
    w.execute("create table steps (idx integer primary key, step_type text, status text)")
    w.execute("create table trajectory_meta (trajectory_id text, cascade_id text, trajectory_type int)")
    w.execute("insert into trajectory_meta values (?,?,?)", ("traj-inner", CONV, 4))
    for _ in range(checkpointed):
        w.execute("insert into steps (step_type, status) values ('checkpointed','DONE')")
    w.commit()
    w.execute("pragma wal_checkpoint(FULL)")
    for _ in range(in_wal):
        w.execute("insert into steps (step_type, status) values ('wal-turn','DONE')")
    w.commit()
    return db, w


def _count(db: Path) -> int:
    with sqlite3.connect(f"file:{db}?mode=rw", uri=True) as c:
        return int(c.execute("select count(*) from steps").fetchone()[0])


def test_committed_wal_rows_survive_migration(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    db, w = _make_conversation(src, checkpointed=2, in_wal=3)
    assert _count(db) == 5, "the source must see checkpointed + WAL rows"
    # the main file alone only holds the checkpointed rows — this is what upstream copied
    main_only = tmp_path / "main-only.db"
    main_only.write_bytes(db.read_bytes())
    assert _count(main_only) == 2, "precondition: 3 rows live only in the -wal"

    mig = convstore.migrate_conversation(CONV, src, dst)
    assert mig.ok, mig.error
    assert mig.integrity == "ok"
    assert mig.steps_src == 5
    assert mig.steps_dst == 5, "no committed transaction may be lost"
    assert mig.conv_verified is True

    out = _cli(dst) / "conversations" / f"{CONV}.db"
    assert _count(out) == 5
    with sqlite3.connect(f"file:{out}?mode=rw", uri=True) as c:
        assert c.execute("select count(*) from steps where step_type='wal-turn'").fetchone()[0] == 3
    w.close()


def test_stale_destination_family_is_replaced_not_merged(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    db, w = _make_conversation(src, checkpointed=1, in_wal=2)
    dcli = _cli(dst)
    (dcli / "conversations" / f"{CONV}.db").write_bytes(b"stale")
    (dcli / "conversations" / f"{CONV}.db-wal").write_bytes(b"stale-wal")

    mig = convstore.migrate_conversation(CONV, src, dst)
    assert mig.ok, mig.error
    out = dcli / "conversations" / f"{CONV}.db"
    assert convstore._is_sqlite(out)
    assert _count(out) == 3
    with sqlite3.connect(f"file:{out}?mode=rw", uri=True) as c:
        assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
    w.close()


def test_brain_and_annotations_are_preserved(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    db, w = _make_conversation(src, checkpointed=1, in_wal=1)
    scli = _cli(src)
    (scli / "brain" / CONV).mkdir(parents=True)
    (scli / "brain" / CONV / "artifact.txt").write_text("brain")
    (scli / "annotations").mkdir(parents=True, exist_ok=True)
    (scli / "annotations" / f"{CONV}.pbtxt").write_text("ann")

    mig = convstore.migrate_conversation(CONV, src, dst)
    assert mig.ok and mig.brain_copied and mig.annotations_copied
    dcli = _cli(dst)
    assert (dcli / "brain" / CONV / "artifact.txt").read_text() == "brain"
    assert (dcli / "annotations" / f"{CONV}.pbtxt").read_text() == "ann"
    w.close()


def test_conversation_id_verified_via_cascade_id(tmp_path):
    """agy names the file after the cascade, not the trajectory — both must count as a match."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    db, w = _make_conversation(src, checkpointed=1, in_wal=1)
    mig = convstore.migrate_conversation(CONV, src, dst)
    assert mig.conv_verified is True, "cascade_id must satisfy the id check"
    w.close()


def test_missing_conversation_is_reported_not_invented(tmp_path):
    mig = convstore.migrate_conversation(CONV, tmp_path / "src", tmp_path / "dst")
    assert mig.ok is False
    assert "not found" in (mig.error or "")


def test_corrupt_source_refuses_rather_than_copying_garbage(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    scli = _cli(src)
    (scli / "conversations" / f"{CONV}.db").write_bytes(b"SQLite format 3\x00" + b"\xff" * 400)
    mig = convstore.migrate_conversation(CONV, src, dst)
    assert mig.ok is False, "a corrupt database must never be published"
    assert not (_cli(dst) / "conversations" / f"{CONV}.db").exists()


def test_agy_copy_conversation_delegates(tmp_path):
    """The public entry point keeps its bool contract."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    db, w = _make_conversation(src, checkpointed=1, in_wal=1)
    assert agy.copy_conversation(CONV, src, dst) is True
    assert agy.copy_conversation("00000000-0000-0000-0000-000000000000", src, dst) is False
    w.close()


def test_held_write_transaction_never_truncates(tmp_path):
    """An in-flight uncommitted write on the source must not yield a partial copy.

    Either SQLite hands over a consistent snapshot containing every *committed* row, or the
    migration refuses. Publishing a database missing committed rows is never acceptable.
    """
    src, dst = tmp_path / "src", tmp_path / "dst"
    db, w = _make_conversation(src, checkpointed=0, in_wal=2)
    w.execute("begin immediate")
    w.execute("insert into steps (step_type, status) values ('in-flight','RUNNING')")
    try:
        mig = convstore.migrate_conversation(CONV, src, dst)
        out = _cli(dst) / "conversations" / f"{CONV}.db"
        if mig.ok:
            assert mig.steps_dst >= 2, f"committed rows were lost: {mig.as_dict()}"
            assert mig.integrity == "ok"
            assert _count(out) >= 2
        else:
            assert not out.exists(), "refusing is fine; leaving a partial db is not"
    finally:
        w.rollback()
        w.close()
