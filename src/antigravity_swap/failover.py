"""``aswap exec``: run an agy print-mode task and carry it across accounts when one runs out.

When a run stops on a quota error, the account is held out until the reset the error
names, the conversation is copied to the next account's directory, and agy resumes it
there with ``--conversation <id>``. A conversation is a local SQLite file, so it moves
between Google accounts intact.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from antigravity_swap import agy
from antigravity_swap.credentials import FileBackend
from antigravity_swap.manager import Manager, SwitchError, with_registry
from antigravity_swap.store import Registry, capture

PROMPT_FLAGS = ("-p", "--print", "--prompt")
EXHAUSTED_EXIT = 3


class ExecError(Exception):
    pass


def _flag_value(args: list[str], names: tuple[str, ...]) -> tuple[int, str | None, int]:
    """(index, value, width) of the first flag in ``names``; width is 1 for --x=v, 2 for --x v."""
    for i, a in enumerate(args):
        for n in names:
            if a == n:
                return i, (args[i + 1] if i + 1 < len(args) else None), 2
            if a.startswith(n + "=") and n.startswith("--"):
                return i, a.split("=", 1)[1], 1
    return -1, None, 0


def _drop(args: list[str], names: tuple[str, ...], takes_value: bool = True) -> list[str]:
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in names:
            i += 2 if takes_value else 1
            continue
        if any(a.startswith(n + "=") for n in names if n.startswith("--")):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def pool_for(args: list[str]) -> str:
    _, model, _ = _flag_value(args, ("--model",))
    m = (model or "").lower()
    return "3p" if m.startswith(("claude", "gpt", "o1", "o3", "o4")) else "gemini"


def plan(args: list[str]) -> dict:
    """Validate the agy arguments ``exec`` was given and work out how to drive them."""
    if _flag_value(args, ("--gemini_dir",))[0] >= 0:
        raise ExecError("do not pass --gemini_dir; aswap picks the directory for each account")
    if _flag_value(args, ("-i", "--prompt-interactive"))[0] >= 0:
        raise ExecError("exec drives print mode only; for interactive use: aswap run")
    idx, prompt, _ = _flag_value(args, PROMPT_FLAGS)
    if idx < 0 or prompt is None:
        raise ExecError('exec needs a print-mode prompt, e.g.: aswap exec -- -p "fix the tests"')
    _, fmt, _ = _flag_value(args, ("--output-format",))
    mode = fmt or "text"
    run_args = list(args) if fmt else list(args) + ["--output-format", "json"]
    return {"args": run_args, "mode": mode, "pool": pool_for(args)}


def resume_args(args: list[str], conv_id: str, prompt: str) -> list[str]:
    base = _drop(args, PROMPT_FLAGS)
    base = _drop(base, ("--conversation",))
    base = _drop(base, ("-c", "--continue"), takes_value=False)
    return base + ["--conversation", conv_id, "-p", prompt]


def last_json(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def _run(cmd: list[str], mode: str, err: TextIO, out: TextIO) -> tuple[int, str, str, str | None]:
    """Run agy, echoing stderr live. Kills it if it stops to ask for a browser sign-in."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    err_buf: list[str] = []
    out_buf: list[str] = []
    conv: list[str] = []

    def pump_err():
        for line in proc.stderr:
            err_buf.append(line)
            err.write(line)
            err.flush()
            if agy.SIGNIN_RE.search(line):
                proc.kill()

    def pump_out():
        for line in proc.stdout:
            out_buf.append(line)
            if mode == "stream-json":
                out.write(line)
                out.flush()
                obj = last_json(line)
                if obj and obj.get("conversation_id"):
                    conv.append(obj["conversation_id"])
            elif agy.SIGNIN_RE.search(line):
                proc.kill()

    t1 = threading.Thread(target=pump_err, daemon=True)
    t2 = threading.Thread(target=pump_out, daemon=True)
    t1.start(); t2.start()
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        rc = proc.wait()
        raise
    finally:
        t1.join(5); t2.join(5)
    return rc, "".join(out_buf), "".join(err_buf), (conv[-1] if conv else None)


def run_exec(mgr: Manager, agy_args: list[str], target: str | None = None, strategy: str | None = None,
             max_hops: int | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    p = plan(agy_args)
    strategy = strategy or mgr.settings.get("exec.strategy")
    resume_prompt = mgr.settings.get("exec.resume_prompt")
    fallback = float(mgr.settings.get("exec.limit_fallback_hours")) * 3600
    cmd0 = mgr.agy_cmd()

    def say(msg: str) -> None:
        err.write(f"aswap: {msg}\n")
        err.flush()

    def first(reg: Registry):
        if target:
            return reg.resolve(target)
        return mgr.pick(reg, strategy, p["pool"])

    acct = with_registry(first)
    if acct is None:
        say("no account is available (all disabled, quarantined or limited). See: aswap list")
        return EXHAUSTED_EXIT
    total = len(Registry.load().accounts)
    max_hops = max_hops if max_hops is not None else int(mgr.settings.get("exec.max_hops"))
    max_hops = max_hops or total
    tried: set[int] = set()
    hops = 0
    conv_id: str | None = None
    prev_dir: Path | None = None
    args = p["args"]

    while True:
        tried.add(acct.slot)
        if mgr.isolates:
            gdir = agy.prepare_session(acct.slot, default_gdir=mgr.gdir)
        else:
            gdir = mgr.gdir
            try:
                res = with_registry(lambda reg: mgr.switch(reg, reg.get(acct.slot), verify=False))
            except SwitchError as e:
                say(str(e))
                return 1
            if res.switched:
                say(f"default login is now account {acct.slot} ({acct.label})")
        check = agy.check_login(cmd0, gdir)
        if not check.ok and check.kind in ("signin", "verify"):
            reason = "login rejected" if check.kind == "signin" else "account needs verification"
            _flag(acct.slot, quarantine=reason)
            say(f"account {acct.slot} ({acct.label}): {reason}; quarantined")
            nxt = _next(mgr, strategy, p["pool"], tried)
            if nxt is None:
                return _exhausted(say)
            acct = nxt
            continue
        if not mgr.isolates:
            capture(acct.slot, mgr.current_login())
        else:
            capture(acct.slot, FileBackend(gdir).read())

        run_args = args
        if conv_id:
            if prev_dir is not None and not agy.copy_conversation(conv_id, prev_dir, gdir):
                say(f"conversation {conv_id} was not found on disk; starting the prompt over")
                conv_id = None
            else:
                run_args = resume_args(args, conv_id, resume_prompt)
        say(f"running on account {acct.slot} ({acct.label})" + (f", continuing {conv_id}" if conv_id else ""))
        started = time.time()
        try:
            rc, stdout, stderr, streamed = _run(cmd0 + agy.dir_args(gdir) + run_args, p["mode"], err, out)
        except KeyboardInterrupt:
            _capture_dir(mgr, acct.slot, gdir)
            return 130
        _capture_dir(mgr, acct.slot, gdir)
        result = last_json(stdout)
        conv_id = (result or {}).get("conversation_id") or streamed or agy.newest_conversation(gdir, started) or conv_id
        status = (result or {}).get("status")
        blob = stdout + "\n" + stderr
        failed = rc != 0 or (status is not None and str(status).upper() != "SUCCESS")
        kind = agy.classify_failure(blob) if failed else "ok"
        if not failed:
            _emit(p["mode"], result, stdout, out)
            return 0
        if kind not in ("quota", "signin", "verify"):
            _emit(p["mode"], result, stdout, out)
            return rc or 1
        if kind == "quota":
            secs = agy.parse_reset(blob)
            until = time.time() + (secs or fallback)
            _flag(acct.slot, limited=(until, "quota: Individual quota reached" if secs else "quota error"))
            say(f"account {acct.slot} ({acct.label}) hit its quota; held out until "
                f"{time.strftime('%H:%M', time.localtime(until))}")
        else:
            reason = "login rejected" if kind == "signin" else "account needs verification"
            _flag(acct.slot, quarantine=reason)
            say(f"account {acct.slot} ({acct.label}): {reason}; quarantined")
        hops += 1
        nxt = _next(mgr, strategy, p["pool"], tried)
        if nxt is None:
            _emit(p["mode"], result, stdout, out)
            return _exhausted(say)
        if hops >= max_hops:
            say(f"stopped after {hops} account change(s) (exec.max_hops)")
            _emit(p["mode"], result, stdout, out)
            return EXHAUSTED_EXIT
        prev_dir = gdir
        acct = nxt


def _capture_dir(mgr: Manager, slot: int, gdir: Path) -> None:
    try:
        cred = mgr.current_login() if not mgr.isolates else FileBackend(gdir).read()
    except Exception:
        return
    if cred is not None:
        capture(slot, cred)


def _flag(slot: int, limited: tuple[float, str] | None = None, quarantine: str | None = None) -> None:
    def fn(reg: Registry):
        a = reg.get(slot)
        if a is None:
            return
        if limited:
            a.limited_until, a.limited_reason = limited
        if quarantine:
            a.quarantined, a.quarantine_reason = True, quarantine
    with_registry(fn)


def _next(mgr: Manager, strategy: str, pool: str, tried: set[int]):
    return with_registry(lambda reg: mgr.pick(reg, strategy, pool, exclude=tried))


def _exhausted(say) -> int:
    reg = Registry.load()
    now = time.time()
    limited = [a.limited_until for a in reg.accounts if a.limited_until > now]
    when = f"; the first one frees up at {time.strftime('%H:%M', time.localtime(min(limited)))}" if limited else ""
    say("every account is limited, disabled or quarantined" + when)
    return EXHAUSTED_EXIT


def _emit(mode: str, result: dict | None, stdout: str, out: TextIO) -> None:
    if mode == "stream-json":
        return  # already streamed
    if mode == "text":
        if result is not None and "response" in result:
            out.write(result.get("response") or "")
        else:
            out.write(stdout)
    else:
        out.write(stdout)
    out.flush()
