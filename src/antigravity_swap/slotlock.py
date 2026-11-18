"""One writer per account slot, many slots in parallel.

``aswap`` protects its registry with a global ``flock`` taken for the whole command, but nothing
stops two processes from running agy against the *same* session directory: two agy processes in
one directory share a SQLite store and a ``presence/`` lock, which the upstream design assumes
never happens.

This module adds a per-slot exclusive lock:

  * one writer per slot — a second process on the same slot is refused immediately
    (``acquire(wait=False)``) or after a bounded wait (``wait=True``);
  * different slots never contend, so N accounts can run at once;
  * the lock is released on normal exit, on exception, on SIGINT/SIGTERM/SIGHUP, and — because
    ``flock`` is owned by the kernel — on crash (the descriptor dies with the process);
  * a lock file left behind by a dead process is *adopted*, never deleted blindly, and a lock
    held by a live PID is never broken.

The lock file is a flock on ``$ASWAP_HOME/locks/slot-<n>.lock`` containing an advisory JSON
``{"pid","started","slot","host"}``. The JSON is diagnostics only: authority is always the
kernel lock. Its owner is verified through ``/proc/<pid>/cmdline`` so a recycled PID is not
mistaken for the original holder.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Iterator

from antigravity_swap import paths

IS_WINDOWS = os.name == "nt"
MIN_WAIT_S = 2.0
MAX_WAIT_S = 600.0


class SlotBusy(Exception):
    def __init__(self, slot: int, owner: dict | None, waited: float = 0.0):
        self.slot, self.owner, self.waited = slot, owner, waited
        who = "another process"
        if owner:
            bits = [f"pid {owner.get('pid')}"]
            if owner.get("started"):
                bits.append(f"since {time.strftime('%H:%M:%S', time.localtime(owner['started']))}")
            who = " ".join(bits)
        super().__init__(f"slot {slot} is busy ({who}); one writer per slot")


@dataclass
class SlotLock:
    slot: int
    path: Path
    fh: IO[bytes] | None
    waited: float = 0.0
    adopted_stale: bool = False

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt

                self.fh.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                self.fh.close()
            self.fh = None


def lock_path(slot: int) -> Path:
    return paths.data_dir() / "locks" / f"slot-{slot}.lock"


def _read_owner(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        owner = json.loads(raw)
    except ValueError:
        return None
    return owner if isinstance(owner, dict) else None


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        return True  # the kernel lock is authoritative; cannot cheaply probe
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmd_is_slot_worker(pid: int) -> bool:
    """True when ``pid`` still looks like an aswap/agy slot worker (guards against PID reuse)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    if not parts:
        return False
    joined = " ".join(parts).lower()
    return "aswap" in joined or "antigravity_swap" in joined or "/agy" in joined or joined.strip() == "agy"


def _try_lock(fh) -> bool:
    if IS_WINDOWS:
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def acquire(slot: int, wait: bool = False, wait_s: float = 0.0, reason: str = "") -> SlotLock:
    """Take the single-writer lock for ``slot``.

    ``wait=True`` polls up to ``wait_s`` seconds (clamped to [2, 600]); ``wait=False`` raises
    ``SlotBusy`` immediately when another process holds it.
    """
    path = lock_path(slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
    deadline = time.monotonic() + (min(max(wait_s, MIN_WAIT_S), MAX_WAIT_S) if wait else 0.0)
    started = time.monotonic()
    adopted = False
    while True:
        fh = open(path, "a+b")
        if _try_lock(fh):
            stale = _read_owner(path)
            if stale and stale.get("pid") not in (None, os.getpid()) and not _pid_alive(int(stale["pid"])):
                adopted = True  # the previous owner died; keep the file, take over the lock
            _write_owner(fh, slot, reason)
            return SlotLock(slot=slot, path=path, fh=fh,
                            waited=round(time.monotonic() - started, 3), adopted_stale=adopted)
        owner = _read_owner(path)
        fh.close()
        elapsed = time.monotonic() - started
        if not wait or time.monotonic() >= deadline:
            raise SlotBusy(slot, owner, round(elapsed, 3))
        time.sleep(0.2)


def _write_owner(fh: IO[bytes], slot: int, reason: str) -> None:
    payload = json.dumps({
        "pid": os.getpid(), "slot": slot, "started": time.time(),
        "host": os.uname().nodename if hasattr(os, "uname") else "", "reason": reason,
    })
    with contextlib.suppress(OSError):
        fh.seek(0)
        fh.truncate()
        fh.write(payload.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


@contextlib.contextmanager
def held(slot: int, wait: bool = False, wait_s: float = 0.0, reason: str = "") -> Iterator[SlotLock]:
    """Context manager that also restores the previous SIGINT/SIGTERM/SIGHUP handlers."""
    lock = acquire(slot, wait=wait, wait_s=wait_s, reason=reason)
    previous: dict[Any, Any] = {}
    if not IS_WINDOWS:
        def _bail(signum, _frame):
            lock.release()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                previous[sig] = signal.signal(sig, _bail)
            except (ValueError, OSError):
                pass
    try:
        yield lock
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, TypeError, OSError):
                signal.signal(sig, handler)
        lock.release()


def active_locks() -> list[dict]:
    """Diagnostics for ``aswap locks``: every slot lock file and its owner state."""
    out = []
    locks_dir = paths.data_dir() / "locks"
    if not locks_dir.is_dir():
        return out
    for p in sorted(locks_dir.glob("slot-*.lock")):
        slot = int(p.stem.split("-", 1)[1]) if "-" in p.stem else -1
        owner = _read_owner(p)
        held_now = False
        fh = None
        try:
            fh = open(p, "a+b")
            held_now = not _try_lock(fh)
        except OSError:
            held_now = False
        finally:
            if fh is not None:
                if held_now:
                    with contextlib.suppress(OSError):
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
        alive = bool(owner and owner.get("pid") and _pid_alive(int(owner["pid"]))
                     and _cmd_is_slot_worker(int(owner["pid"])))
        out.append({"slot": slot, "path": str(p), "held": held_now,
                    "owner": owner, "ownerAlive": alive,
                    "stale": bool(owner) and not held_now and not alive})
    return out
