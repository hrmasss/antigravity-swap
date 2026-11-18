"""Per-slot single-writer locks.

Requirements covered here:
  * one writer per slot; a second process on the same slot is refused;
  * different slots never contend (3 slots = 3 writers at once);
  * the lock dies with the process (crash recovery: no manual cleanup needed);
  * a lock file left by a dead PID is adopted, never deleted blindly;
  * a lock held by a live process is never broken.

The child processes below are real OS processes: an flock only means something across processes,
so an in-process test would prove nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from antigravity_swap import slotlock

HOLDER = textwrap.dedent("""
    import json, os, sys, time
    from antigravity_swap import slotlock
    slot, secs, ready = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
    try:
        with slotlock.held(slot, reason="pytest-holder"):
            open(ready, "w").write("locked")
            time.sleep(secs)
        print("released")
    except slotlock.SlotBusy as exc:
        print(f"busy:{exc}")
        sys.exit(3)
""")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "aswap"
    h.mkdir()
    monkeypatch.setenv("ASWAP_HOME", str(h))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    return h


def _spawn(slot: int, secs: float, ready: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(slot), str(secs), str(ready)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for(p: Path, timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if p.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{p} never appeared")


def test_second_process_on_same_slot_is_refused(home, tmp_path):
    ready = tmp_path / "ready"
    holder = _spawn(1, 3.0, ready)
    try:
        _wait_for(ready)
        with pytest.raises(slotlock.SlotBusy) as exc:
            slotlock.acquire(1, wait=False)
        assert "slot 1 is busy" in str(exc.value)
        assert exc.value.owner and exc.value.owner.get("slot") == 1
    finally:
        holder.wait(10)


def test_three_slots_run_concurrently(home, tmp_path):
    holders = []
    try:
        for slot in (1, 2, 3):
            ready = tmp_path / f"ready{slot}"
            holders.append((slot, ready, _spawn(slot, 2.5, ready)))
        for _, ready, _ in holders:
            _wait_for(ready)
        locks = {l["slot"]: l for l in slotlock.active_locks()}
        assert set(locks) == {1, 2, 3}
        assert all(l["held"] for l in locks.values()), locks
    finally:
        for _, _, p in holders:
            p.wait(15)


def test_lock_released_when_process_exits(home, tmp_path):
    ready = tmp_path / "ready"
    holder = _spawn(4, 0.5, ready)
    _wait_for(ready)
    with pytest.raises(slotlock.SlotBusy):
        slotlock.acquire(4)
    holder.wait(10)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            lk = slotlock.acquire(4)
            lk.release()
            return
        except slotlock.SlotBusy:
            time.sleep(0.05)
    pytest.fail("lock was not released when the holder exited")


def test_lock_dies_with_crashed_process(home, tmp_path):
    ready = tmp_path / "ready"
    holder = _spawn(5, 30.0, ready)
    _wait_for(ready)
    holder.kill()          # SIGKILL: no finally block, no signal handler
    holder.wait(10)
    lk = slotlock.acquire(5)   # kernel released the flock when the fd died
    assert lk.adopted_stale is True, "a stale lock file must be adopted, not left orphaned"
    lk.release()


def test_stale_owner_record_is_adopted(home):
    """A lock file whose recorded PID is dead must not block a new writer."""
    path = slotlock.lock_path(7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 999999, "slot": 7, "started": 1.0, "host": "x"}))
    lk = slotlock.acquire(7)
    assert lk.adopted_stale is True
    assert json.loads(path.read_text())["pid"] == os.getpid()
    lk.release()


def test_live_owner_is_never_broken(home):
    lk = slotlock.acquire(8)
    try:
        owner = json.loads(slotlock.lock_path(8).read_text())
        assert owner["pid"] == os.getpid()
        with pytest.raises(slotlock.SlotBusy):
            slotlock.acquire(8)
    finally:
        lk.release()


def test_wait_mode_times_out_instead_of_hanging(home, tmp_path):
    ready = tmp_path / "ready"
    holder = _spawn(9, 5.0, ready)
    try:
        _wait_for(ready)
        start = time.monotonic()
        with pytest.raises(slotlock.SlotBusy):
            slotlock.acquire(9, wait=True, wait_s=2.0)
        elapsed = time.monotonic() - start
        assert 1.5 <= elapsed <= 12, f"waited {elapsed:.1f}s; expected a bounded wait"
    finally:
        holder.wait(15)


def test_wait_mode_acquires_after_release(home, tmp_path):
    ready = tmp_path / "ready"
    holder = _spawn(10, 1.0, ready)
    _wait_for(ready)
    lk = slotlock.acquire(10, wait=True, wait_s=15.0)
    lk.release()
    holder.wait(10)


def test_sigterm_releases_the_lock(home, tmp_path):
    """SIGTERM must unwind the holder, not leave a lock that only a crash could explain."""
    ready = tmp_path / "ready"
    holder = _spawn(11, 30.0, ready)
    _wait_for(ready)
    holder.terminate()
    holder.wait(10)
    lk = slotlock.acquire(11)
    lk.release()


def test_active_locks_reports_owner_state(home, tmp_path):
    ready = tmp_path / "ready"
    holder = _spawn(12, 2.0, ready)
    try:
        _wait_for(ready)
        rows = {r["slot"]: r for r in slotlock.active_locks()}
        assert rows[12]["held"] is True
        assert rows[12]["owner"]["slot"] == 12
        assert rows[12]["stale"] is False
    finally:
        holder.wait(10)
