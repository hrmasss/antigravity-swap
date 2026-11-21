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

from antigravity_swap import agy, convstore, errors, slotlock
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
    strategy = strategy or mgr.settings.get("failover.strategy")
    resume_prompt = mgr.settings.get("failover.resume_prompt")
    fallback = float(mgr.settings.get("failover.limit_fallback_hours")) * 3600
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
    max_hops = max_hops if max_hops is not None else int(mgr.settings.get("failover.max_hops"))
    max_hops = max_hops or total
    tried: set[int] = set()
    hops = 0
    conv_id: str | None = None
    prev_dir: Path | None = None
    prev_slot: int | None = None
    args = p["args"]

    while True:
        tried.add(acct.slot)
        try:
            # The lock covers everything that touches this slot's directory: prepare_session
            # symlink/token placement, the `agy models` probe and the turn itself.
            with slotlock.held(acct.slot, wait=False, reason="exec"):
                outcome = _exec_on_slot(mgr, acct, args, conv_id, prev_dir, p, cmd0, resume_prompt,
                                        say, out, err, strategy, prev_slot=prev_slot)
        except slotlock.SlotBusy as exc:
            say(str(exc))
            nxt = _next(mgr, strategy, p["pool"], tried, acct.slot)
            if nxt is None:
                return _exhausted(say)
            if hops >= max_hops:
                say(f"stopped after {hops} account change(s) (failover.max_hops)")
                return EXHAUSTED_EXIT
            hops += 1
            prev_dir = None
            prev_slot = None
            acct = nxt
            continue

        if outcome["next"] is not None:
            nxt = outcome["next"]
            if nxt is None:
                return _exhausted(say)
            if hops >= max_hops:
                say(f"stopped after {hops} account change(s) (failover.max_hops)")
                return EXHAUSTED_EXIT
            hops += 1
            conv_id, prev_dir = outcome.get("conv_id"), outcome.get("gdir")
            prev_slot = acct.slot
            acct = nxt
            continue

        rc, stdout, stderr, streamed = outcome["rc"], outcome["stdout"], outcome["stderr"], outcome["streamed"]
        gdir = outcome["gdir"]
        result = last_json(stdout)
        conv_id = (result or {}).get("conversation_id") or streamed or agy.newest_conversation(gdir, outcome["started"]) or conv_id
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
        nxt = _next(mgr, strategy, p["pool"], tried, acct.slot)
        if nxt is None:
            _emit(p["mode"], result, stdout, out)
            return _exhausted(say)
        if hops >= max_hops:
            say(f"stopped after {hops} account change(s) (failover.max_hops)")
            _emit(p["mode"], result, stdout, out)
            return EXHAUSTED_EXIT
        prev_dir = gdir
        prev_slot = acct.slot
        acct = nxt


def _exec_on_slot(mgr: Manager, acct, args, conv_id, prev_dir, p, cmd0, resume_prompt,
                  say, out, err, strategy: str, prev_slot: int | None = None) -> dict:
    """Prepare, probe and run one account. Caller holds that slot's lock."""
    gdir = agy.prepare_session(acct.slot, default_gdir=mgr.gdir) if mgr.isolates else mgr.gdir
    if not mgr.isolates:
        try:
            res = with_registry(lambda reg: mgr.switch(reg, reg.get(acct.slot), verify=False))
        except SwitchError as e:
            say(str(e))
            return {"rc": 1, "stdout": "", "stderr": "", "streamed": None, "gdir": gdir,
                    "started": time.time(), "next": None}
        if res.switched:
            say(f"default login is now account {acct.slot} ({acct.label})")
    check = agy.check_login(cmd0, gdir)
    if not check.ok and check.kind in ("signin", "verify"):
        reason = "login rejected" if check.kind == "signin" else "account needs verification"
        _flag(acct.slot, quarantine=reason)
        say(f"account {acct.slot} ({acct.label}): {reason}; quarantined")
        nxt = _next(mgr, strategy, p["pool"], {acct.slot}, acct.slot)
        return {"next": nxt, "conv_id": conv_id, "gdir": gdir}
    capture(acct.slot, mgr.current_login() if not mgr.isolates else FileBackend(gdir).read())

    run_args = args
    if conv_id:
        if prev_dir is not None:
            if prev_slot is not None and prev_slot != acct.slot:
                # Use ordered acquisition to avoid lock-ordering inversion
                if prev_slot < acct.slot:
                    # Would grab low while holding high — skip lock (inversion risk), migrate directly
                    mig = convstore.migrate_conversation(conv_id, prev_dir, gdir)
                else:
                    # prev_slot > acct.slot — grabbing high while holding low, safe
                    try:
                        with slotlock.held(prev_slot, wait=True, wait_s=1.0, reason="migrate_src"):
                            mig = convstore.migrate_conversation(conv_id, prev_dir, gdir)
                    except slotlock.SlotBusy:
                        mig = convstore.migrate_conversation(conv_id, prev_dir, gdir)
            else:
                mig = convstore.migrate_conversation(conv_id, prev_dir, gdir)
            if not mig.ok:
                say(f"conversation {conv_id} could not be carried over ({mig.error}); starting over")
                conv_id = None
            else:
                detail = (f"{mig.steps_dst} step(s), integrity {mig.integrity}"
                          if mig.steps_dst is not None else "byte copy (no SQLite tables)")
                say(f"conversation {conv_id} carried over ({mig.method}, {detail})")
                run_args = resume_args(args, conv_id, resume_prompt)
    say(f"running on account {acct.slot} ({acct.label})" + (f", continuing {conv_id}" if conv_id else ""))
    started = time.time()

    def attempt_fn(_n: int):
        return _run(cmd0 + agy.dir_args(gdir) + run_args, p["mode"], err, out)[:3]

    def renew() -> bool:
        again = agy.check_login(cmd0, gdir)
        if again.ok:
            _capture_dir(mgr, acct.slot, gdir)
            say(f"account {acct.slot} ({acct.label}): access token renewed")
            return True
        say(f"account {acct.slot} ({acct.label}): token renewal failed ({again.message or again.kind})")
        return False

    try:
        rc, stdout, stderr, attempts, _recovery = errors.run_with_recovery(
            attempt_fn, renew_fn=renew, log=lambda m: say(f"account {acct.slot}: {m}"))
        if attempts > 1:
            say(f"account {acct.slot} ({acct.label}): {attempts} attempt(s) were needed")
        streamed = agy.newest_conversation(gdir, started) if p["mode"] == "stream-json" else None
    except KeyboardInterrupt:
        _capture_dir(mgr, acct.slot, gdir)
        raise
    _capture_dir(mgr, acct.slot, gdir)
    return {"rc": rc, "stdout": stdout, "stderr": stderr, "streamed": streamed,
            "gdir": gdir, "started": started, "next": None}


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


def _next(mgr: Manager, strategy: str, pool: str, tried: set[int], after: int):
    """The next account, counting on from the one that just failed when rotating.

    Quota readings for the candidates are refreshed first (when older than
    usage.max_age_seconds), so "most quota left" is judged on current numbers.
    """
    def fn(reg):
        if strategy != "rotate":
            active = mgr.active(reg)
            for a in reg.ordered():
                if a.slot not in tried and a.rotatable():
                    try:
                        mgr.refresh_usage(reg, a, active=active)
                    except Exception:
                        pass  # a failed read keeps the last-known numbers
        return mgr.pick(reg, strategy, pool, current=reg.get(after), exclude=tried)
    return with_registry(fn)


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
