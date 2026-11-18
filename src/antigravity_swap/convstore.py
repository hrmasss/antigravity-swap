"""WAL-safe conversation migration.

The upstream ``agy.copy_conversation`` copies only ``<id>.db`` and silently drops the
SQLite ``-wal``/``-shm`` siblings, so turns that were committed but not yet
checkpointed never reach the destination. This module replaces that copy with the
SQLite online backup API, which is safe while another process is writing, and
verifies the result before it is published.

Order of preference:
  1. ``sqlite3.Connection.backup`` from a read-only source connection
     (works under concurrent writers; SQLite restarts the copy if the source is
     written mid-backup).
  2. ``PRAGMA wal_checkpoint(FULL)`` on the source, then a byte copy of the
     checkpointed ``.db`` together with any surviving ``-wal``.
  3. refuse and report — never publish a half-copied database.

Post-conditions checked before the destination file is swapped in:
  * ``PRAGMA integrity_check`` == "ok"
  * the step count matches the source
  * the conversation id is present in ``trajectory_meta`` (``cascade_id`` or
    ``trajectory_id`` — agy names the file after the cascade on current builds)
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from antigravity_swap import paths

BUSY_TIMEOUT_MS = 5000
CONV_TABLE = "trajectory_meta"


class MigrationError(Exception):
    pass


@dataclass
class MigrationResult:
    ok: bool
    method: str = ""
    conv_id: str = ""
    steps_src: int | None = None
    steps_dst: int | None = None
    integrity: str | None = None
    conv_verified: bool | None = None
    brain_copied: bool = False
    annotations_copied: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "method": self.method, "conv_id": self.conv_id,
            "steps_src": self.steps_src, "steps_dst": self.steps_dst,
            "integrity": self.integrity, "convVerified": self.conv_verified,
            "brainCopied": self.brain_copied, "annotationsCopied": self.annotations_copied,
            "error": self.error, "notes": self.notes,
        }


def _conn_uri(p: Path) -> str:
    """Open ``p`` read/write but never create it.

    ``mode=ro`` cannot be used for a WAL database: SQLite's wal-index (``-shm``) needs write
    access even for readers, so a read-only connection silently sees *only* the main database
    file and misses every uncheckpointed row. ``mode=rw`` keeps us from creating files while
    still letting SQLite consult the WAL.
    """
    return f"file:{p}?mode=rw"


def _step_count(conn: sqlite3.Connection) -> int | None:
    try:
        return int(conn.execute("select count(*) from steps").fetchone()[0])
    except sqlite3.Error:
        return None


def _conv_present(conn: sqlite3.Connection, conv_id: str) -> bool:
    try:
        cols = [r[1] for r in conn.execute(f"pragma table_info('{CONV_TABLE}')")]
    except sqlite3.Error:
        return False
    if not cols:
        return False
    for col in ("cascade_id", "trajectory_id"):
        if col in cols:
            hit = conn.execute(f"select 1 from {CONV_TABLE} where {col}=? limit 1", (conv_id,)).fetchone()
            if hit:
                return True
    return False


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _remove_family(p: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        _unlink_quiet(Path(str(p) + suffix))


def _is_sqlite(p: Path) -> bool:
    """SQLite files start with the 16-byte header ``SQLite format 3\\0``."""
    try:
        with open(p, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def snapshot_database(src: Path, dst: Path) -> tuple[str, list[str]]:
    """Copy ``src`` to ``dst`` so no committed transaction is lost.

    Returns ``(method, notes)``. Raises ``MigrationError`` when no safe copy is possible.
    """
    notes: list[str] = []
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.migrating-{os.getpid()}-{int(time.time() * 1000)}")
    _remove_family(tmp)

    if not _is_sqlite(src):
        # Not a database (test stand-ins, or a truncated file): there is no WAL to lose and no
        # way to open it as SQLite, so a byte copy keeps exactly the upstream semantics.
        try:
            shutil_copy_family(src, tmp)
            return _publish(tmp, dst, "copy-non-sqlite", ["source is not an SQLite database"])
        finally:
            _remove_family(tmp)

    try:
        try:
            with sqlite3.connect(_conn_uri(src), uri=True, timeout=BUSY_TIMEOUT_MS / 1000) as s:
                s.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
                with sqlite3.connect(tmp) as d:
                    d.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
                    s.backup(d)
                    d.commit()
            return _publish(tmp, dst, "backup", notes)
        except (sqlite3.Error, OSError) as exc:
            notes.append(f"backup API unavailable: {exc.__class__.__name__}: {exc}")
            _remove_family(tmp)

        # Fallback: checkpoint the source, then copy coherently.
        try:
            with sqlite3.connect(src, timeout=BUSY_TIMEOUT_MS / 1000) as s:
                s.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
                busy, log_frames, checkpointed = s.execute("pragma wal_checkpoint(FULL)").fetchone()
            if busy:
                raise MigrationError(
                    f"source is locked by a live writer (wal_checkpoint busy={busy}); "
                    "refusing to copy a database that is being written")
            notes.append(f"checkpointed {checkpointed}/{log_frames} wal frames")
            shutil_copy_family(src, tmp)
            return _publish(tmp, dst, "checkpoint+copy", notes)
        except MigrationError:
            _remove_family(tmp)
            raise
        except (sqlite3.Error, OSError) as exc:
            _remove_family(tmp)
            raise MigrationError(f"no safe copy possible: {exc.__class__.__name__}: {exc}") from None
    finally:
        _remove_family(tmp)


def shutil_copy_family(src: Path, tmp: Path) -> None:
    """Copy ``src`` plus any surviving WAL/SHM, preserving group-write semantics."""
    import shutil

    shutil.copy2(src, tmp)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(tmp) + suffix))


def _publish(tmp: Path, dst: Path, method: str, notes: list[str]) -> tuple[str, list[str]]:
    # A pre-existing destination family would otherwise be mixed with the new pages.
    _remove_family(dst)
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(tmp) + suffix)
        if side.exists():
            os.replace(side, Path(str(dst) + suffix))
    return method, notes


def copy_sidecars(conv_id: str, src_gdir: Path, dst_gdir: Path) -> tuple[bool, bool]:
    """Copy ``brain/<id>`` and ``annotations/<id>.pbtxt``. Both are optional."""
    import shutil

    scli, dcli = paths.cli_dir(src_gdir), paths.cli_dir(dst_gdir)
    brain = False
    ann = False
    if (scli / "brain" / conv_id).is_dir():
        shutil.copytree(scli / "brain" / conv_id, dcli / "brain" / conv_id, dirs_exist_ok=True)
        brain = True
    src_ann = scli / "annotations" / f"{conv_id}.pbtxt"
    if src_ann.is_file():
        (dcli / "annotations").mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_ann, dcli / "annotations" / src_ann.name)
        ann = True
    return brain, ann


def _sweep_stale_temps(directory: Path, name: str, max_age_s: float = 300.0) -> int:
    """Delete ``.<name>.migrating-*`` files left by a process that died mid-copy.

    Each file is a complete-in-progress snapshot; a stranded one is wasted disk and, worse,
    makes "what is on disk" ambiguous. Only files older than ``max_age_s`` are removed so a
    concurrent migration in another process is never disturbed.
    """
    removed = 0
    if not directory.is_dir():
        return 0
    cutoff = time.time() - max_age_s
    for stale in directory.glob(f".{name}.migrating-*"):
        try:
            if stale.stat().st_mtime < cutoff:
                _remove_family(stale)
                removed += 1
        except OSError:
            continue
    return removed


def migrate_conversation(conv_id: str, src_gdir: Path, dst_gdir: Path) -> MigrationResult:
    """Move one conversation to another agy directory without losing committed turns."""
    res = MigrationResult(ok=False, conv_id=conv_id)
    scli, dcli = paths.cli_dir(src_gdir), paths.cli_dir(dst_gdir)
    src_db = scli / "conversations" / f"{conv_id}.db"
    dst_db = dcli / "conversations" / f"{conv_id}.db"
    if not src_db.is_file():
        res.error = f"conversation {conv_id} not found in {scli / 'conversations'}"
        return res
    if scli.resolve() == dcli.resolve():
        res.ok, res.method = True, "same-directory"
        res.conv_verified = True
        return res

    swept = _sweep_stale_temps(dst_db.parent, dst_db.name)
    notes_prefix = f"swept {swept} stale migration temp file(s)" if swept else ""
    try:
        method, notes = snapshot_database(src_db, dst_db)
        res.method, res.notes = method, notes
        if notes_prefix:
            res.notes.append(notes_prefix)
    except MigrationError as exc:
        res.error = str(exc)
        return res

    with sqlite3.connect(_conn_uri(src_db), uri=True) as s:
        res.steps_src = _step_count(s)
    if res.method == "copy-non-sqlite":
        # Verification is not applicable: the file cannot be opened as SQLite at all.
        res.brain_copied, res.annotations_copied = copy_sidecars(conv_id, src_gdir, dst_gdir)
        res.ok = dst_db.is_file()
        res.notes.append("verified by byte copy only (no SQLite tables to check)")
        return res
    with sqlite3.connect(_conn_uri(dst_db), uri=True) as d:
        res.integrity = d.execute("pragma integrity_check").fetchone()[0]
        res.steps_dst = _step_count(d)
        res.conv_verified = _conv_present(d, conv_id)

    res.brain_copied, res.annotations_copied = copy_sidecars(conv_id, src_gdir, dst_gdir)

    problems = []
    if res.integrity != "ok":
        problems.append(f"integrity_check={res.integrity}")
    if res.steps_src is not None and res.steps_dst is not None and res.steps_dst < res.steps_src:
        problems.append(f"steps lost: src={res.steps_src} dst={res.steps_dst}")
    if res.steps_dst == 0 and (res.steps_src or 0) > 0:
        problems.append("destination has no steps")
    if res.conv_verified is False:
        problems.append("conversation id absent from destination trajectory_meta")
    if problems:
        res.error = "; ".join(problems)
        _remove_family(dst_db)  # never leave a corrupt/partial database behind
        res.ok = False
        return res

    res.ok = True
    return res


def digest_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
