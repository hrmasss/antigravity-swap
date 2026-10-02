"""aswap: switch between Antigravity CLI (agy) accounts, by hand or automatically."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from antigravity_swap import __version__, agy, auto, paths, style, views
from antigravity_swap.credentials import Credential, CredentialError, FileBackend
from antigravity_swap.failover import ExecError, run_exec
from antigravity_swap.supervise import run_interactive
from antigravity_swap.fsutil import locked, write_atomic
from antigravity_swap.manager import Manager, SwitchError, with_registry
from antigravity_swap.settings import KEYS, PICK_STRATEGIES, POOLS, STRATEGIES, Settings
from antigravity_swap.settings import strategy as strategy_name
from antigravity_swap.store import Registry, StoreError, capture, load_cred, save_cred
from antigravity_swap.usage import WINDOW_ORDER, windows

SCHEMA = 1


class CliError(Exception):
    pass


# --- formatting --------------------------------------------------------------------------


def fmt_when(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "-"
    now = now or time.time()
    lt = time.localtime(ts)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M", lt)
    if ts - now < 6 * 86400:
        return time.strftime("%a %H:%M", lt)
    return time.strftime("%b %d", lt)


def fmt_age(sec: float | None) -> str:
    if sec is None:
        return "never"
    if sec < 90:
        return "just now"
    if sec < 5400:
        return f"{sec / 60:.0f}m ago"
    if sec < 172800:
        return f"{sec / 3600:.0f}h ago"
    return f"{sec / 86400:.0f}d ago"


def parse_duration(s: str) -> float:
    secs = agy.parse_reset(f"Resets in {s}")
    if not secs:
        raise CliError(f"cannot read duration '{s}'; use forms like 90m, 5h, 1h30m")
    return secs


def _flags(a, now) -> str:
    bits = []
    if a.disabled:
        bits.append("disabled")
    if a.quarantined:
        bits.append(f"quarantined: {a.quarantine_reason}")
    if a.is_limited(now):
        bits.append(f"limited until {fmt_when(a.limited_until, now)}")
    return ", ".join(bits)


# --- views -------------------------------------------------------------------------------


def account_row(mgr: Manager, a, active, now) -> dict:
    rec = mgr.cached(a)
    reading = rec.get("reading")
    w = windows(reading)
    row = {
        "number": a.slot, "email": a.email, "active": bool(active and active.slot == a.slot),
        "usageStatus": rec.get("status") or "unavailable",
        "usage": {k: {"usedPct": v["used"],
                      "resetsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v["resets_at"]))
                      if v["resets_at"] else None} for k, v in w.items()} or None,
    }
    age = mgr.usage_age(a)
    if age is not None:
        row["usageAgeSeconds"] = round(age)
    if rec.get("error"):
        row["usageError"] = rec["error"]
    if a.alias:
        row["alias"] = a.alias
    if a.disabled:
        row["disabled"] = True
    if a.is_limited(now):
        row["limitedUntil"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(a.limited_until))
        row["limitedReason"] = a.limited_reason
    if a.quarantined:
        row["quarantined"] = True
        row["quarantineReason"] = a.quarantine_reason
    return row


def print_table(mgr: Manager, reg: Registry, active, out=sys.stdout) -> None:
    now = time.time()
    if not reg.accounts:
        out.write("No accounts yet. Sign in with agy, then run: aswap add\n")
        return
    heads = ["", "#", "account", "5h", "week", "3p 5h", "3p wk", "5h resets", "checked", ""]
    rows = []
    for a in reg.ordered():
        rec = mgr.cached(a)
        w = windows(rec.get("reading"))
        cells = ["*" if active and active.slot == a.slot else "", str(a.slot),
                 a.label + (f" ({a.alias})" if a.alias else "")]
        for b in WINDOW_ORDER:
            cells.append(f"{w[b]['used']:.0f}%" if b in w else "-")
        cells.append(fmt_when((w.get("gemini-5h") or {}).get("resets_at"), now))
        status = rec.get("status")
        age = fmt_age(mgr.usage_age(a))
        if status and status != "ok":
            age += f" ({status.replace('_', ' ')})"
        cells.append(age)
        cells.append(_flags(a, now))
        rows.append(cells)
    widths = [max(len(r[i]) for r in rows + [heads]) for i in range(len(heads))]
    for r in [heads] + rows:
        out.write("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() + "\n")
    out.write("\nused % per window. 5h/week: Gemini models. 3p: Claude and GPT models through agy.\n")
    if active is None:
        cred = mgr.current_login()
        if cred is not None:
            out.write(f"The current agy login ({cred.identity().email or 'unknown'}) is not managed: aswap add\n")


# --- commands ----------------------------------------------------------------------------


def cmd_add(mgr, args):
    def fn(reg):
        return mgr.add(reg, slot=args.slot, alias=args.alias,
                       from_dir=Path(args.from_dir).expanduser() if args.from_dir else None, force=args.force)
    acct, what = with_registry(fn)
    print(f"{'Added' if what == 'added' else 'Updated'} account {acct.slot}: {acct.label}"
          + (f" ({acct.alias})" if acct.alias else ""))
    if what == "added" and args.from_dir is None:
        print("Next: sign in to another account with agy (no need to log out), then run aswap add again.")
    return 0


def cmd_list(mgr, args):
    with locked():
        reg = Registry.load()
        active = mgr.capture_active(reg)
        if not args.cached:
            for a in reg.ordered():
                if a.disabled:
                    continue
                mgr.refresh_usage(reg, a, force=args.refresh, active=active)
        reg.save()
    instances = views.instance_rows(mgr, reg, active)
    if args.json:
        now = time.time()
        print(json.dumps({"schemaVersion": SCHEMA, "activeAccountNumber": active.slot if active else None,
                          "backend": mgr.backend.name,
                          "accounts": [account_row(mgr, a, active, now) for a in reg.ordered()],
                          "runningInstances": instances}, indent=2))
    elif args.table:
        print_table(mgr, reg, active)
    else:
        views.print_tree(mgr, reg, active, sys.stdout)
        views.print_instances(instances, sys.stdout)
    return 0


def cmd_status(mgr, args):
    reg = Registry.load()
    cred = mgr.current_login()
    active = reg.find_identity(cred.identity()) if cred else None
    if args.json:
        payload = {"schemaVersion": SCHEMA, "backend": mgr.backend.name,
                   "signedIn": cred is not None, "managed": active is not None,
                   "email": cred.identity().email if cred else None,
                   "activeAccountNumber": active.slot if active else None,
                   "runningInstances": views.instance_rows(mgr, reg, active)}
        if active:
            payload["account"] = account_row(mgr, active, active, time.time())
        print(json.dumps(payload, indent=2))
        return 0
    if cred is None:
        print("agy is not signed in.")
    elif active is None:
        print(f"Signed in as {cred.identity().email or 'unknown'}, not managed by aswap. Run: aswap add")
    else:
        views.print_tree(mgr, reg, active, sys.stdout, only=active)
    views.print_instances(views.instance_rows(mgr, reg, active), sys.stdout)
    return 0


def cmd_switch(mgr, args):
    def fn(reg):
        if args.target:
            target = reg.resolve(args.target)
        else:
            current = mgr.active(reg)
            target = mgr.pick(reg, args.strategy, args.pool or mgr.settings.get("autoswitch.pool"),
                              current=current, threshold=float(mgr.settings.get("autoswitch.threshold")))
            if target is None:
                raise CliError("no other account is available to switch to (see: aswap list)")
        return mgr.switch(reg, target, verify=False if args.no_verify else None, force=args.force)
    res = with_registry(fn)
    if args.json:
        print(json.dumps({"schemaVersion": SCHEMA, **res.as_json()}))
        return 0
    if not res.switched:
        print(f"Account {res.to.slot} ({res.to.label}) is already active.")
        return 0
    print(f"Switched to account {res.to.slot}: {res.to.label}"
          + ("" if res.verified is None else (" (login verified)" if res.verified else " (could not verify login)")))
    reg = Registry.load()
    old = [r for r in views.instance_rows(mgr, reg, res.to) if r["accountNumber"] != res.to.slot]
    if old:
        print(f"{len(old)} running agy session(s) keep the account they started on until restarted. "
              f"Resume one with: agy -c")
    return 0


def _run_plain(mgr, passthrough):
    return _spawn(mgr.agy_cmd() + passthrough)


def _spawn(cmd) -> int:
    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.call(cmd)
    finally:
        signal.signal(signal.SIGINT, prev)


def cmd_run(mgr, args, passthrough):
    reg = Registry.load()
    if args.target:
        target = reg.resolve(args.target)
    else:
        target = _mapped(reg, Path.cwd())
    if mgr.settings.get("run.failover") and not args.no_failover and reg.accounts:
        try:
            return run_interactive(mgr, passthrough, target=str(target.slot) if target else None,
                                   strategy=args.strategy, share_history=args.share_history)
        except ExecError as e:
            raise CliError(str(e)) from None
    active = mgr.active(reg)
    if target is None:
        return _run_plain(mgr, passthrough)
    if not mgr.isolates:
        if active and active.slot == target.slot:
            return _run_plain(mgr, passthrough)
        raise CliError(f"agy keeps one login for the whole machine on this platform ({mgr.backend.name}), "
                       f"so two accounts cannot run at once. Use: aswap switch {target.slot}")
    if active and active.slot == target.slot and not args.share_history and not args.no_share:
        if args.require_session:
            raise CliError(f"account {target.slot} is the default login; refusing to run a second copy of it")
        return _run_plain(mgr, passthrough)
    with locked():
        mgr._freshest(target)
        sdir = agy.prepare_session(target.slot, share_history=args.share_history, default_gdir=mgr.gdir)
    rc = _spawn(mgr.agy_cmd() + agy.dir_args(sdir) + passthrough)
    try:
        cred = FileBackend(sdir).read()
    except CredentialError:
        cred = None
    if cred is not None:
        if cred.identity().matches(target.email, target.sub):
            with locked():
                capture(target.slot, cred)
        else:
            print(f"aswap: the session signed in as {cred.identity().email}, not account {target.slot}; "
                  f"not saved. Use: aswap add --from-dir {sdir}", file=sys.stderr)
    return rc


def _mapped(reg: Registry, cwd: Path):
    best, best_len = None, -1
    cwd = cwd.resolve()
    for d, slot in reg.mappings.items():
        p = Path(d)
        if (cwd == p or p in cwd.parents) and len(str(p)) > best_len:
            best, best_len = reg.get(slot), len(str(p))
    return best


def cmd_exec(mgr, args, passthrough):
    try:
        return run_exec(mgr, passthrough, target=args.target, strategy=args.strategy, max_hops=args.max_hops)
    except ExecError as e:
        raise CliError(str(e)) from None


def cmd_auto(mgr, args):
    policy = auto.Policy.from_settings(mgr.settings, threshold=args.threshold, interval=args.interval,
                                       cooldown=args.cooldown, strategy=args.strategy, pool=args.pool)
    policy.dry_run = args.dry_run
    if not args.once and not args.json:
        print(f"aswap auto: switching at {policy.threshold:.0f}% used ({policy.pool} pool, "
              f"{policy.strategy}), checking every {policy.interval:.0f}s. Ctrl-C stops.")
    return auto.run(mgr, policy, once=args.once, as_json=args.json)


def cmd_refresh(mgr, args):
    with locked():
        reg = Registry.load()
        active = mgr.capture_active(reg)
        targets = [reg.resolve(args.target)] if args.target else reg.ordered()
        for a in targets:
            rec = mgr.refresh_usage(reg, a, force=True, active=active)
            print(f"  {a.slot}  {a.label}: {rec.get('status')}"
                  + (f" ({rec['error']})" if rec.get("error") and rec.get("status") != "ok" else ""))
        reg.save()
    return 0


def _set_flag(args, fn, msg):
    def inner(reg):
        a = reg.resolve(args.target)
        fn(a)
        return a
    a = with_registry(inner)
    print(msg.format(n=a.slot, label=a.label))
    return 0


def cmd_disable(mgr, args):
    return _set_flag(args, lambda a: setattr(a, "disabled", True),
                     "Account {n} ({label}) is out of rotation. It stays a valid explicit switch target.")


def cmd_enable(mgr, args):
    return _set_flag(args, lambda a: setattr(a, "disabled", False), "Account {n} ({label}) is back in rotation.")


def cmd_limit(mgr, args):
    if args.clear:
        def fn(a):
            a.limited_until, a.limited_reason = 0.0, None
            a.quarantined, a.quarantine_reason = False, None
        return _set_flag(args, fn, "Account {n} ({label}) is no longer held out.")
    secs = parse_duration(args.duration)

    def fn(a):
        a.limited_until, a.limited_reason = time.time() + secs, "set by hand"
    return _set_flag(args, fn, "Account {n} ({label}) is held out until " + fmt_when(time.time() + secs) + ".")


def cmd_alias(mgr, args):
    if not args.target:
        reg = Registry.load()
        for a in reg.ordered():
            if a.alias:
                print(f"  {a.alias:<12} {a.slot}  {a.label}")
        return 0

    def fn(reg):
        a = reg.resolve(args.target)
        if args.unset:
            a.alias = None
        else:
            if not args.name:
                raise CliError("give the alias: aswap alias <num|email> <name>")
            Manager._check_alias(reg, args.name, a.slot)
            a.alias = args.name
        return a
    a = with_registry(fn)
    print(f"Account {a.slot}: " + (f"alias '{a.alias}'" if a.alias else "alias removed"))
    return 0


def cmd_remove(mgr, args):
    def fn(reg):
        a = reg.resolve(args.target)
        reg.remove(a.slot)
        return a
    a = with_registry(fn)
    print(f"Removed account {a.slot} ({a.label}). The agy login itself is untouched.")
    return 0


def cmd_config(mgr, args):
    s = mgr.settings
    if args.action in (None, "list"):
        if args.json:
            print(json.dumps({k: {"value": s.get(k), "default": s.is_default(k)} for k in KEYS}, indent=2))
        else:
            for k, key in KEYS.items():
                v = s.get(k)
                print(f"  {k:<30} {json.dumps(v)}{'  (default)' if s.is_default(k) else ''}")
        return 0
    if args.action == "path":
        print(paths.settings_file())
        return 0
    if not args.key or args.key not in KEYS:
        raise CliError(f"unknown key '{args.key}'. Keys: {', '.join(KEYS)}")
    if args.action == "get":
        v = s.get(args.key)
        print(json.dumps({"key": args.key, "value": v}) if args.json else json.dumps(v))
        return 0
    if args.action == "set":
        if args.value is None:
            raise CliError("give a value: aswap config set <key> <value>")
        try:
            v = s.set(args.key, args.value)
        except ValueError as e:
            raise CliError(str(e)) from None
        s.save()
        print(f"{args.key} = {json.dumps(v)}")
        return 0
    if args.action == "unset":
        s.unset(args.key)
        s.save()
        print(f"{args.key} reset to {json.dumps(KEYS[args.key].default)}")
        return 0
    raise CliError(f"unknown config action '{args.action}'")


def cmd_export(mgr, args):
    reg = Registry.load()
    mgr.capture_active(reg)
    accts = [reg.resolve(args.account)] if args.account else reg.ordered()
    out = {"schemaVersion": SCHEMA, "tool": "antigravity-swap", "exportedAt": time.time(), "accounts": []}
    for a in accts:
        cred = load_cred(a.slot)
        if cred is None:
            continue
        out["accounts"].append({"slot": a.slot, "email": a.email, "sub": a.sub, "alias": a.alias,
                                "disabled": a.disabled, "credential": json.loads(cred.raw)})
    text = json.dumps(out, indent=2) + "\n"
    if args.file == "-":
        sys.stdout.write(text)
    else:
        write_atomic(Path(args.file), text, private=True)
        print(f"Exported {len(out['accounts'])} account(s) to {args.file}. It holds live logins: keep it private.",
              file=sys.stderr)
    return 0


def cmd_import(mgr, args):
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        raise CliError("not an aswap export file") from None
    results = []

    def fn(reg):
        for item in data.get("accounts", []):
            cred = Credential(json.dumps(item["credential"]))
            ident = cred.identity()
            existing = reg.find_identity(ident) if (ident.email or ident.sub) else None
            if existing and not (args.force or existing.quarantined):
                results.append(f"  skipped {item.get('email')} (already account {existing.slot})")
                continue
            if existing:
                save_cred(existing.slot, cred)
                existing.quarantined, existing.quarantine_reason = False, None
                results.append(f"  replaced account {existing.slot} ({existing.label})")
                continue
            from antigravity_swap.store import Account
            slot = item.get("slot") if item.get("slot") and not reg.get(item["slot"]) else reg.free_slot()
            a = Account(slot=slot, email=ident.email or item.get("email"), sub=ident.sub or item.get("sub"),
                        alias=item.get("alias"), disabled=bool(item.get("disabled")), added_at=time.time())
            if a.alias and any(x.alias == a.alias for x in reg.accounts):
                a.alias = None
            reg.accounts.append(a)
            reg.accounts.sort(key=lambda x: x.slot)
            save_cred(slot, cred)
            results.append(f"  imported {a.label} as account {slot}")
    with_registry(fn)
    print("\n".join(results) or "Nothing to import.")
    return 0


def cmd_map(mgr, args):
    if not args.target:
        reg = Registry.load()
        for d, slot in sorted(reg.mappings.items()):
            a = reg.get(slot)
            print(f"  {d}  ->  {slot} {a.label if a else '?'}")
        return 0

    def fn(reg):
        a = reg.resolve(args.target)
        d = str(Path(args.dir or os.getcwd()).expanduser().resolve())
        reg.mappings[d] = a.slot
        return a, d
    a, d = with_registry(fn)
    print(f"{d} -> account {a.slot} ({a.label}). A bare 'aswap run' there launches it.")
    return 0


def cmd_unmap(mgr, args):
    d = str(Path(args.dir or os.getcwd()).expanduser().resolve())

    def fn(reg):
        return reg.mappings.pop(d, None)
    print(f"Unmapped {d}." if with_registry(fn) is not None else f"{d} was not mapped.")
    return 0


def cmd_watch(mgr, args):
    try:
        while True:
            with locked():
                reg = Registry.load()
                active = mgr.capture_active(reg)
                for a in reg.ordered():
                    if not a.disabled:
                        mgr.refresh_usage(reg, a, active=active, max_age=args.interval)
                reg.save()
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")
            print(f"aswap watch  {time.strftime('%H:%M:%S')}  (every {args.interval:.0f}s, Ctrl-C stops)\n")
            views.print_tree(mgr, reg, active, sys.stdout)
            views.print_instances(views.instance_rows(mgr, reg, active), sys.stdout)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_upgrade(mgr, args):
    exe = sys.executable.replace("\\", "/").lower()
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            print("Standalone binary: download the latest aswap-windows-x86_64.exe from "
                  "https://github.com/hrmasss/antigravity-swap/releases/latest")
            return 0
        cmd = ["sh", "-c", "curl -fsSL https://raw.githubusercontent.com/hrmasss/antigravity-swap/main/install.sh "
               "| ASWAP_INSTALL_DIR=" + str(Path(sys.executable).parent) + " sh"]
    elif "/uv/tools/" in exe:
        cmd = ["uv", "tool", "install", "--force", "--refresh", "antigravity-swap"]
    elif "/pipx/" in exe:
        cmd = ["pipx", "upgrade", "antigravity-swap"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-U", "antigravity-swap"]
    if os.name == "nt" and cmd[0] != sys.executable:
        print("Windows keeps aswap.exe locked while it runs. Run this yourself:")
        print("  " + " ".join(cmd))
        return 0
    print("Running: " + " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd)


def cmd_purge(mgr, args):
    d = paths.data_dir()
    if not args.yes:
        if not sys.stdin.isatty():
            raise CliError("purge deletes every stored login; pass --yes to confirm")
        if input(f"Delete everything in {d}? The agy login itself is untouched. [y/N] ").strip().lower() != "y":
            return 1
    shutil.rmtree(d, ignore_errors=True)
    print(f"Removed {d}.")
    return 0


# --- parser ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aswap",
        description="Switch between Antigravity CLI (agy) accounts, by hand or automatically.",
        epilog="Accounts are referred to by number, email or alias. Run 'aswap <command> -h' for details.",
    )
    p.add_argument("--version", action="version", version=f"antigravity-swap {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    s = sub.add_parser("add", help="store the account agy is signed in with")
    s.add_argument("--slot", type=int, help="put it in this slot")
    s.add_argument("--alias", help="short name for the account")
    s.add_argument("--from-dir", metavar="DIR", help="read the login from another agy directory (--gemini_dir)")
    s.add_argument("--force", action="store_true", help="overwrite an occupied slot")

    s = sub.add_parser("list", aliases=["ls"], help="every account with its quota")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true", help="fetch fresh quota for every account")
    s.add_argument("--cached", action="store_true", help="no network; show the last readings")
    s.add_argument("--table", action="store_true", help="one compact row per account")

    s = sub.add_parser("status", help="which account agy is using")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("switch", help="change the account plain agy uses")
    s.add_argument("target", nargs="?", help="number, email or alias; omit to rotate")
    s.add_argument("--strategy", type=strategy_name, choices=PICK_STRATEGIES, default="rotate",
                   help="how to pick when no target is given (default: rotate)")
    s.add_argument("--pool", choices=POOLS, help="quota pool for best / next-available / consume-first")
    s.add_argument("--no-verify", action="store_true", help="skip the `agy models` login check")
    s.add_argument("--force", action="store_true", help="rewrite even if already active; allow quarantined")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("run", help="run agy here; when the account runs out, continue on the next one")
    s.add_argument("target", nargs="?",
                   help="number, email or alias; omit for the directory mapping or the active account")
    s.add_argument("--strategy", type=strategy_name, choices=PICK_STRATEGIES, metavar="STRATEGY",
                   help="best (most-quota), rotate (round-robin), next-available, "
                        "consume-first (soonest-reset); default: failover.strategy")
    s.add_argument("--no-failover", action="store_true",
                   help="plain session on one account, no quota watching")
    s.add_argument("--share-history", action="store_true", help="share conversations with your default agy")
    s.add_argument("--no-share", action="store_true", help="stop sharing conversations for this account")
    s.add_argument("--require-session", action="store_true",
                   help="refuse instead of running plain agy when the target is the default login")

    s = sub.add_parser("exec", help="run an agy -p task; on a quota error, continue it on the next account")
    s.add_argument("target", nargs="?", help="account to start on (default: picked by failover.strategy)")
    s.add_argument("--strategy", type=strategy_name, choices=PICK_STRATEGIES, metavar="STRATEGY",
                   help="best (most-quota), rotate (round-robin), next-available, consume-first (soonest-reset)")
    s.add_argument("--max-hops", type=int, help="most account changes (default: every account once)")

    s = sub.add_parser("auto", help="switch automatically before the active account runs out")
    s.add_argument("--threshold", type=float, help="used %% that triggers a switch (default 90)")
    s.add_argument("--interval", type=float, help="seconds between checks (default 60)")
    s.add_argument("--cooldown", type=float, help="seconds between proactive switches (default 300)")
    s.add_argument("--strategy", type=strategy_name, choices=STRATEGIES)
    s.add_argument("--pool", choices=POOLS)
    s.add_argument("--once", action="store_true",
                   help="one check; exit 0 switched, 1 error, 2 nothing to do, 3 blocked")
    s.add_argument("--dry-run", action="store_true", help="say what it would do, never switch")
    s.add_argument("--json", action="store_true", help="one JSON event per line")

    s = sub.add_parser("watch", help="live quota table")
    s.add_argument("--interval", type=float, default=60.0)

    s = sub.add_parser("refresh", help="renew tokens and fetch quota now")
    s.add_argument("target", nargs="?")

    for name, h in (("disable", "hold an account out of rotation"), ("enable", "return it to rotation")):
        s = sub.add_parser(name, help=h)
        s.add_argument("target")

    s = sub.add_parser("limit", help="hold an account out for a while, or clear a hold")
    s.add_argument("target")
    s.add_argument("duration", nargs="?", default="5h", help="e.g. 90m, 5h, 1h30m (default 5h)")
    s.add_argument("--clear", action="store_true", help="clear the hold and any quarantine")

    s = sub.add_parser("alias", help="name an account, or list names")
    s.add_argument("target", nargs="?")
    s.add_argument("name", nargs="?")
    s.add_argument("--unset", action="store_true")

    s = sub.add_parser("remove", aliases=["rm"], help="forget an account")
    s.add_argument("target")

    s = sub.add_parser("config", help="show or change settings")
    s.add_argument("action", nargs="?", choices=["list", "get", "set", "unset", "path"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("export", help="write accounts to a file (plaintext logins)")
    s.add_argument("file", help="path, or - for stdout")
    s.add_argument("--account")

    s = sub.add_parser("import", help="read accounts from an export")
    s.add_argument("file", help="path, or - for stdin")
    s.add_argument("--force", action="store_true", help="replace accounts that already exist")

    s = sub.add_parser("map", help="bind a directory to an account for 'aswap run'")
    s.add_argument("target", nargs="?")
    s.add_argument("dir", nargs="?")

    s = sub.add_parser("unmap", help="remove a directory binding")
    s.add_argument("dir", nargs="?")

    s = sub.add_parser("purge", help="delete all aswap data")
    s.add_argument("--yes", action="store_true")

    sub.add_parser("upgrade", aliases=["update"], help="upgrade aswap itself")
    return p


COMMANDS = {
    "add": cmd_add, "list": cmd_list, "ls": cmd_list, "status": cmd_status, "switch": cmd_switch,
    "auto": cmd_auto, "watch": cmd_watch, "refresh": cmd_refresh, "disable": cmd_disable,
    "enable": cmd_enable, "limit": cmd_limit, "alias": cmd_alias, "remove": cmd_remove,
    "config": cmd_config, "export": cmd_export, "import": cmd_import, "map": cmd_map,
    "unmap": cmd_unmap, "purge": cmd_purge, "rm": cmd_remove, "upgrade": cmd_upgrade,
    "update": cmd_upgrade,
}

# claude-swap's flag spellings, so muscle memory carries over: `aswap --list`, `--switch-to 2` ...
LEGACY_FLAGS = {
    "--list": "list", "--status": "status", "--add-account": "add", "--remove-account": "remove",
    "--disable-account": "disable", "--enable-account": "enable", "--switch": "switch",
    "--switch-to": "switch", "--export": "export", "--import": "import", "--watch": "watch",
    "--tui": "watch", "--upgrade": "upgrade", "--auto": "auto", "--refresh": "refresh",
}


def translate_legacy(argv: list[str]) -> list[str]:
    if argv and argv[0] in LEGACY_FLAGS:
        return [LEGACY_FLAGS[argv[0]]] + argv[1:]
    return argv
PASSTHROUGH = {"run": cmd_run, "exec": cmd_exec}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    argv = translate_legacy(argv)
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "list"
    if args.cmd is None:
        args = build_parser().parse_args(["list"])
    if passthrough and cmd not in PASSTHROUGH:
        print("aswap: arguments after -- are only for 'run' and 'exec'", file=sys.stderr)
        return 2
    try:
        mgr = Manager()
        if sys.stderr.isatty():
            mgr.on_swap = lambda a: print(style.dim(f"aswap: renewing {a.alias or a.label}'s login"),
                                          file=sys.stderr, flush=True)
        if cmd in PASSTHROUGH:
            return PASSTHROUGH[cmd](mgr, args, passthrough)
        return COMMANDS[cmd](mgr, args)
    except (CliError, StoreError, SwitchError, CredentialError, agy.AgyNotFound) as e:
        print(f"aswap: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
