"""Everything that touches the agy binary or an agy directory."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from antigravity_swap import paths
from antigravity_swap.credentials import FileBackend
from antigravity_swap.paths import IS_MACOS, IS_WINDOWS
from antigravity_swap.store import load_cred

QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|Individual quota reached|\bcode 429\b", re.I)
RESET_RE = re.compile(r"Resets in\s+((?:\d+h)?\s*(?:\d+m)?\s*(?:\d+(?:\.\d+)?s)?)", re.I)
SIGNIN_RE = re.compile(r"Authentication required|Please visit the URL to log in|Please sign in", re.I)
VERIFY_RE = re.compile(r"Verify your account|not eligible for Antigravity", re.I)

SHARED_HISTORY = ("conversations", "brain", "annotations", "knowledge")


class AgyNotFound(Exception):
    pass


def find_agy(configured: str = "") -> list[str]:
    """The command that runs agy, as an argv prefix."""
    cand = configured or os.environ.get("ASWAP_AGY", "")
    if cand:
        if Path(cand).suffix == ".py":
            import sys

            return [sys.executable, cand]
        return [cand]
    found = shutil.which("agy")
    if found:
        return [found]
    for p in (Path.home() / ".local" / "bin" / "agy",
              Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"):
        if p.is_file():
            return [str(p)]
    raise AgyNotFound("agy not found on PATH; set it with: aswap config set agy.path <path>")


def dir_args(gdir: Path | None) -> list[str]:
    return [f"--gemini_dir={gdir}"] if gdir is not None else []


@dataclass
class Check:
    ok: bool
    kind: str  # ok | signin | verify | error | timeout
    message: str = ""


def classify_failure(text: str) -> str:
    if VERIFY_RE.search(text):
        return "verify"
    if SIGNIN_RE.search(text):
        return "signin"
    if QUOTA_RE.search(text):
        return "quota"
    return "error"


def check_login(agy: list[str], gdir: Path | None, timeout: float = 60) -> Check:
    """Run ``agy models``: proves the login works and renews an expired access token.

    It lists models and spends no quota. agy writes the renewed token back itself.
    """
    try:
        r = subprocess.run(agy + dir_args(gdir) + ["models"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return Check(False, "timeout", f"agy models did not answer in {timeout:.0f}s")
    except OSError as e:
        return Check(False, "error", str(e))
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        return Check(True, "ok")
    kind = classify_failure(out)
    msg = next((ln.strip() for ln in out.splitlines() if ln.strip() and "Fetching" not in ln), "")
    return Check(False, kind if kind != "quota" else "error", msg[:300])


def parse_reset(text: str) -> float | None:
    """Seconds until reset from 'Resets in 4h42m2s'. The last match wins."""
    matches = [m.group(1) for m in RESET_RE.finditer(text) if m.group(1).strip()]
    if not matches:
        return None
    s = matches[-1].replace(" ", "")
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", s):
        total += float(num) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total or None


# --- session directories ---------------------------------------------------------------


def prepare_session(slot: int, share_history: bool = False, default_gdir: Path | None = None) -> Path:
    """Make ``sessions/<slot>`` a ready agy directory for that account and return it."""
    default_gdir = default_gdir or paths.gemini_dir()
    sdir = paths.session_dir(slot)
    scli = paths.cli_dir(sdir)
    scli.mkdir(parents=True, exist_ok=True)

    stored = load_cred(slot)
    fb = FileBackend(sdir)
    current = fb.read() if fb.path.exists() else None
    if stored is not None and (
        current is None
        or not current.identity().matches(stored.identity().email, stored.identity().sub)
        or stored.newer_than(current)
    ):
        fb.write(stored)

    dcli = paths.cli_dir(default_gdir)
    if (dcli / "settings.json").is_file():
        shutil.copy2(dcli / "settings.json", scli / "settings.json")
    if (default_gdir / "config").is_dir():
        shutil.copytree(default_gdir / "config", sdir / "config", dirs_exist_ok=True)
    _link(dcli / "skills", scli / "skills")
    for name in SHARED_HISTORY:
        if share_history:
            _share(dcli / name, scli / name)
        else:
            _unshare(scli / name)
    return sdir


def _link(src: Path, dst: Path) -> None:
    if not src.exists() or dst.is_symlink() or dst.exists():
        return
    try:
        dst.symlink_to(src, target_is_directory=src.is_dir())
    except OSError:
        pass


def _share(src: Path, dst: Path) -> None:
    """Point ``dst`` at ``src``. Anything already in ``dst`` is moved over first, never lost."""
    src.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        if Path(os.readlink(dst)) == src:
            return
        dst.unlink()
    elif dst.is_dir():
        for item in dst.iterdir():
            target = src / item.name
            if not target.exists():
                shutil.move(str(item), str(target))
        shutil.rmtree(dst, ignore_errors=True)
    dst.symlink_to(src, target_is_directory=True)


def _unshare(dst: Path) -> None:
    if dst.is_symlink():
        dst.unlink()
        dst.mkdir()


def copy_conversation(conv_id: str, src_gdir: Path, dst_gdir: Path) -> bool:
    """Carry one conversation to another agy directory so ``--conversation`` can resume it."""
    scli, dcli = paths.cli_dir(src_gdir), paths.cli_dir(dst_gdir)
    db = scli / "conversations" / f"{conv_id}.db"
    if not db.is_file():
        return False
    if scli.resolve() == dcli.resolve() or (dcli / "conversations").resolve() == (scli / "conversations").resolve():
        return True
    (dcli / "conversations").mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, dcli / "conversations" / db.name)
    if (scli / "brain" / conv_id).is_dir():
        shutil.copytree(scli / "brain" / conv_id, dcli / "brain" / conv_id, dirs_exist_ok=True)
    ann = scli / "annotations" / f"{conv_id}.pbtxt"
    if ann.is_file():
        (dcli / "annotations").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ann, dcli / "annotations" / ann.name)
    return True


def newest_conversation(gdir: Path, since: float) -> str | None:
    d = paths.cli_dir(gdir) / "conversations"
    try:
        dbs = [p for p in d.glob("*.db") if p.stat().st_mtime >= since - 1]
    except OSError:
        return None
    if not dbs:
        return None
    return max(dbs, key=lambda p: p.stat().st_mtime).stem


# --- running agy processes ----------------------------------------------------------------


def running_agy() -> list[int]:
    """PIDs of agy processes on this machine, best effort."""
    me = os.getpid()
    pids: list[int] = []
    try:
        if IS_WINDOWS:
            r = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq agy.exe"],
                               capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) > 1 and parts[0].lower() == "agy.exe" and parts[1].isdigit():
                    pids.append(int(parts[1]))
        elif IS_MACOS:
            r = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                pid, _, comm = line.strip().partition(" ")
                if Path(comm.strip()).name == "agy" and pid.isdigit():
                    pids.append(int(pid))
        else:
            for p in Path("/proc").iterdir():
                if not p.name.isdigit():
                    continue
                try:
                    argv0 = (p / "cmdline").read_bytes().split(b"\0", 1)[0]
                except OSError:
                    continue
                if Path(argv0.decode(errors="replace")).name == "agy":
                    pids.append(int(p.name))
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in pids if p != me]
