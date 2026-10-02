"""``aswap run``: an interactive agy session that moves to the next account when one runs out.

agy keeps its login in memory, so a running session cannot change account in place. aswap
runs agy as a child in this terminal and follows its log. When the account's quota is gone
(agy logs a final "agent executor error ... Individual quota reached ... Resets in"), aswap
holds that account out until the reset, stops agy, carries the conversation to the next
account, and starts agy again on it with ``--conversation <id> -i "<continue prompt>"``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from antigravity_swap import agy, paths
from antigravity_swap.failover import (PROMPT_FLAGS, ExecError, _capture_dir, _drop, _exhausted, _flag,
                                       _flag_value, _next, pool_for, run_exec)
from antigravity_swap.manager import Manager, with_registry
from antigravity_swap.store import Registry

# agy retries a quota error a couple of times ("attempt N failed ... retrying") and then gives
# up with an executor error. Only that last line means the turn is dead, so only it triggers.
FINAL_QUOTA_RE = re.compile(
    r"agent executor error:.*(?:Individual quota reached|RESOURCE_EXHAUSTED.*Resets in)", re.I)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
INTERACTIVE_PROMPT_FLAGS = ("-i", "--prompt-interactive")


class LogWatch(threading.Thread):
    """Follow an agy log file: remember the live conversation id, flag the final quota error."""

    def __init__(self, path: Path, from_end: bool = False):
        super().__init__(daemon=True)
        self.path = path
        self.from_end = from_end
        self.conv: str | None = None
        self.quota_line: str | None = None
        self.hit = threading.Event()
        self.done = threading.Event()

    def feed(self, line: str) -> None:
        if "onversation" in line:
            m = UUID_RE.search(line)
            if m and (self.conv is None or "Created conversation" in line or "esum" in line):
                self.conv = m.group(0)
        if FINAL_QUOTA_RE.search(line):
            self.quota_line = line
            self.hit.set()

    def run(self) -> None:
        while not self.done.is_set() and not self.path.exists():
            time.sleep(0.2)
        if self.done.is_set():
            return
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            if self.from_end:
                fh.seek(0, 2)
            buf = ""
            while not self.done.is_set():
                chunk = fh.readline()
                if not chunk:
                    time.sleep(0.25)
                    continue
                buf += chunk
                if buf.endswith("\n"):
                    self.feed(buf)
                    buf = ""


def resume_args(args: list[str], conv_id: str, prompt: str) -> list[str]:
    base = _drop(args, INTERACTIVE_PROMPT_FLAGS)
    base = _drop(base, ("--conversation",))
    base = _drop(base, ("-c", "--continue"), takes_value=False)
    return base + ["--conversation", conv_id, "-i", prompt]


def _children(pid: int) -> list[int]:
    kids = []
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                if int((p / "stat").read_text().rsplit(")", 1)[1].split()[1]) == pid:
                    kids.append(int(p.name))
            except (OSError, ValueError, IndexError):
                pass
    except OSError:
        pass
    return kids


def stop(proc: subprocess.Popen) -> None:
    """End agy and the language server it spawns."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        kids = _children(proc.pid)
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        for k in kids:
            try:
                os.kill(k, signal.SIGKILL)
            except OSError:
                pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


def restore_terminal() -> None:
    """agy draws on the alternate screen in raw mode; put the terminal back before relaunching."""
    try:
        sys.stdout.write("\033[?1049l\033[?25h\033[0m\r\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    if os.name != "nt" and sys.stdin.isatty():
        subprocess.run(["stty", "sane"], stdin=sys.stdin, capture_output=True)


def run_interactive(mgr: Manager, agy_args: list[str], target: str | None = None,
                    strategy: str | None = None, share_history: bool = False,
                    err: TextIO = sys.stderr, grace: float = 1.5) -> int:
    if _flag_value(agy_args, ("--gemini_dir",))[0] >= 0:
        raise ExecError("do not pass --gemini_dir; aswap picks the directory for each account")
    if _flag_value(agy_args, PROMPT_FLAGS)[0] >= 0:
        return run_exec(mgr, agy_args, target=target, strategy=strategy, err=err)
    strategy = strategy or mgr.settings.get("failover.strategy")
    resume_prompt = mgr.settings.get("failover.resume_prompt")
    fallback = float(mgr.settings.get("failover.limit_fallback_hours")) * 3600
    pool = pool_for(agy_args)
    cmd0 = mgr.agy_cmd()
    _, user_log, _ = _flag_value(agy_args, ("--log-file",))
    logdir = paths.data_dir() / "run-logs"
    logdir.mkdir(parents=True, exist_ok=True)

    def say(msg: str) -> None:
        err.write(f"aswap: {msg}\n")
        err.flush()

    def first(reg: Registry):
        active = mgr.active(reg)
        if target:
            return reg.resolve(target), active
        if active is not None and active.rotatable():
            return active, active
        return mgr.pick(reg, strategy, pool, current=active), active

    acct, default_acct = with_registry(first)
    if acct is None:
        return _exhausted(say)
    max_hops = int(mgr.settings.get("failover.max_hops")) or len(Registry.load().accounts)
    tried: set[int] = set()
    hops = 0
    conv: str | None = None
    prev_dir: Path | None = None
    run_args = list(agy_args)

    while True:
        tried.add(acct.slot)
        if mgr.isolates:
            on_default = default_acct is not None and acct.slot == default_acct.slot and not share_history
            gdir = mgr.gdir if on_default else agy.prepare_session(
                acct.slot, share_history=share_history, default_gdir=mgr.gdir)
        else:
            slot = acct.slot
            res = with_registry(lambda reg: mgr.switch(reg, reg.get(slot), verify=False))
            if res.switched:
                say(f"default login is now account {acct.slot} ({acct.label})")
            gdir = mgr.gdir
        if conv and prev_dir is not None:
            if agy.copy_conversation(conv, prev_dir, gdir):
                run_args = resume_args(agy_args, conv, resume_prompt)
            else:
                say(f"conversation {conv} was not found on disk; starting a fresh session")
                run_args = list(agy_args)
        logpath = Path(user_log) if user_log else logdir / f"{os.getpid()}-{hops}.log"
        launch = run_args if user_log else run_args + [f"--log-file={logpath}"]
        watch = LogWatch(logpath, from_end=bool(user_log))
        watch.start()
        started = time.time()
        nxt = None
        prev_int = signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to agy
        try:
            proc = subprocess.Popen(cmd0 + agy.dir_args(gdir) + launch)
            while True:
                try:
                    rc = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if not watch.hit.is_set():
                    continue
                watch.hit.clear()
                secs = agy.parse_reset(watch.quota_line or "")
                until = time.time() + (secs or fallback)
                _flag(acct.slot, limited=(until, "quota: Individual quota reached" if secs else "quota error"))
                nxt = _next(mgr, strategy, pool, tried, acct.slot)
                if nxt is None or hops + 1 > max_hops:
                    say(f"account {acct.slot} hit its quota and no other account is free; staying on it")
                    nxt = None
                    continue
                time.sleep(grace)  # let the error show before the screen changes
                stop(proc)
                rc = None
                break
        finally:
            signal.signal(signal.SIGINT, prev_int)
            watch.done.set()
        _capture_dir(mgr, acct.slot, gdir)
        conv = watch.conv or agy.newest_conversation(gdir, started) or conv
        if not user_log:
            try:
                logpath.unlink()
            except OSError:
                pass
        if rc is not None:
            return rc
        restore_terminal()
        held = Registry.load().get(acct.slot)
        until_txt = time.strftime("%H:%M", time.localtime(held.limited_until)) if held else "?"
        say(f"account {acct.slot} ({acct.label}) hit its quota; held out until {until_txt}")
        say(f"continuing {'conversation ' + conv if conv else 'in a new session'} on account "
            f"{nxt.slot} ({nxt.label})")
        hops += 1
        prev_dir = gdir
        acct = nxt
