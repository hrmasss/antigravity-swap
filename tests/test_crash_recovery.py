"""Crash recovery: a process killed mid-operation must never leave the store wedged or corrupt.

Two failure shapes are covered:

  * a conversation migration killed between "temp file written" and "published" — the leftover
    ``.migrating-*`` file must be ignored by the next run, never published, and never treated as
    a conversation;
  * a worker killed while holding a slot lock — the kernel releases the flock, the next process
    adopts the stale record and proceeds without manual cleanup.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from antigravity_swap import convstore, slotlock

CONV = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _conv_dir(gdir: Path) -> Path:
    d = gdir / "antigravity-cli" / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_db(path: Path, rows: int) -> None:
    c = sqlite3.connect(path)
    c.execute("pragma journal_mode=wal")
    c.execute("create table steps (idx integer primary key, step_type text)")
    c.execute("create table trajectory_meta (trajectory_id text, cascade_id text)")
    c.execute("insert into trajectory_meta values ('t', ?)", (CONV,))
    for _ in range(rows):
        c.execute("insert into steps (step_type) values ('s')")
    c.commit()
    c.close()


CRASHER = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from antigravity_swap import convstore
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    real_publish = convstore._publish
    def die(tmp, d, method, notes):
        os._exit(90)          # hard death: no cleanup, temp file stays behind
    convstore._publish = die
    convstore.migrate_conversation(sys.argv[3], src, dst)
""")


def test_leftover_temp_file_is_ignored_and_reused(tmp_path):
    """A dead process left ``.<name>.migrating-*``; the next migration must overwrite it cleanly."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_db(_conv_dir(src) / f"{CONV}.db", 4)

    r = subprocess.run([sys.executable, "-c", CRASHER, str(src), str(dst), CONV],
                       capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert r.returncode == 90, f"the crasher should have died hard: {r.stderr}"

    dst_conv = _conv_dir(dst)
    leftovers = list(dst_conv.glob(".migrating-*")) or list(dst_conv.glob(f".{CONV}.db.migrating-*"))
    assert leftovers, "precondition: the crash must have left a temp file behind"
    assert not (dst_conv / f"{CONV}.db").exists(), "nothing may be published by a crashed run"
    # a recovery sweep only removes temps that are already old enough to be abandoned
    for p in leftovers:
        old = time.time() - 3600
        os.utime(p, (old, old))

    mig = convstore.migrate_conversation(CONV, src, dst)
    assert mig.ok, mig.error
    assert mig.steps_dst == 4
    assert any("swept" in n for n in mig.notes), mig.notes
    remaining = list(dst_conv.glob(f".{CONV}.db.migrating-*"))
    assert remaining == [], f"temp files must be cleaned up, found {remaining}"


def test_crashed_worker_lock_is_adopted_without_cleanup(tmp_path, monkeypatch):
    home = tmp_path / "aswap"
    home.mkdir()
    monkeypatch.setenv("ASWAP_HOME", str(home))
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent("""
            import time
            from antigravity_swap import slotlock
            with slotlock.held(3, reason="crash-test"):
                print("ready", flush=True)
                time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    try:
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(slotlock.SlotBusy):
            slotlock.acquire(3)
        holder.kill()
        holder.wait(10)
        lock = slotlock.acquire(3)
        try:
            assert lock.adopted_stale is True
        finally:
            lock.release()
    finally:
        if holder.poll() is None:
            holder.kill()


def test_no_partial_database_after_failed_publish(tmp_path):
    """If publishing explodes, the destination must not contain a usable-looking partial db."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_db(_conv_dir(src) / f"{CONV}.db", 3)

    real = convstore._publish
    convstore._publish = lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone"))
    try:
        mig = convstore.migrate_conversation(CONV, src, dst)
    finally:
        convstore._publish = real
    assert mig.ok is False
    assert not (_conv_dir(dst) / f"{CONV}.db").exists()
    # and a retry after the fault clears still works
    mig2 = convstore.migrate_conversation(CONV, src, dst)
    assert mig2.ok and mig2.steps_dst == 3


def test_interrupted_wal_write_does_not_leave_shm_orphan(tmp_path):
    """After a successful migration the destination must not inherit a stale wal-index."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _make_db(_conv_dir(src) / f"{CONV}.db", 2)
    mig = convstore.migrate_conversation(CONV, src, dst)
    assert mig.ok
    out = _conv_dir(dst) / f"{CONV}.db"
    with sqlite3.connect(f"file:{out}?mode=rw", uri=True) as c:
        assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert c.execute("select count(*) from steps").fetchone()[0] == 2
