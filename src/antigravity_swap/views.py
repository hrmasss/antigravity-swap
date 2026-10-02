"""Human views: the account tree (cswap style) and the running-instances list."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TextIO

from antigravity_swap import paths, style
from antigravity_swap.credentials import CredentialError, FileBackend
from antigravity_swap.manager import Manager, default_account_at
from antigravity_swap.processes import Instance, list_instances
from antigravity_swap.store import Account, Registry
from antigravity_swap.usage import WINDOW_ORDER, windows

LABELS = {
    "gemini-5h": "Gemini 5h",
    "gemini-weekly": "Gemini 7d",
    "3p-5h": "Claude/GPT 5h",
    "3p-weekly": "Claude/GPT 7d",
}


def fmt_reset(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "-"
    now = now or time.time()
    lt = time.localtime(ts)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M", lt)
    return f"{time.strftime('%b', lt)} {lt.tm_mday} {time.strftime('%H:%M', lt)}"


def fmt_in(sec: float) -> str:
    if sec <= 60:
        return "now"
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


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


def account_name(a: Account) -> str:
    return a.label + (f" [{a.alias}]" if a.alias else "")


def _header_bits(mgr: Manager, a: Account, is_active: bool, now: float) -> list[str]:
    bits = []
    if is_active:
        bits.append(style.active("(active)"))
    if a.disabled:
        bits.append(style.dim("(disabled)"))
    if a.quarantined:
        bits.append(style.bad(f"(quarantined: {a.quarantine_reason})"))
    if a.is_limited(now):
        bits.append(style.warn(f"(limited until {fmt_reset(a.limited_until, now)}, in {fmt_in(a.limited_until - now)})"))
    rec = mgr.cached(a)
    status = rec.get("status")
    age = mgr.usage_age(a)
    stale = age is not None and age > float(mgr.settings.get("usage.max_age_seconds"))
    if status and status != "ok":
        bits.append(style.dim(f"· {status.replace('_', ' ')}" + (f", last read {fmt_age(age)}" if age else "")))
    elif stale:
        bits.append(style.dim(f"· {fmt_age(age)}"))
    return bits


def print_tree(mgr: Manager, reg: Registry, active: Account | None, out: TextIO,
               only: Account | None = None) -> None:
    g = style.glyphs()
    now = time.time()
    accts = [only] if only else reg.ordered()
    if not accts:
        out.write("No accounts yet. Sign in with agy, then run: aswap add\n")
        return
    if not only:
        out.write(style.bold("Accounts:") + "\n")
    for a in accts:
        is_active = bool(active and active.slot == a.slot)
        name = style.active(account_name(a)) if is_active else account_name(a)
        out.write("  " + " ".join([f"{a.slot}: {name}"] + _header_bits(mgr, a, is_active, now)) + "\n")
        w = windows(mgr.cached(a).get("reading"))
        rows = [b for b in WINDOW_ORDER if b in w]
        if not rows:
            out.write(f"     {style.dim(g['end'])} {style.dim(f'no quota reading yet (aswap refresh {a.slot})')}\n")
            continue
        width = max(len(LABELS[b]) for b in rows) + 1
        for i, b in enumerate(rows):
            tree = style.dim(g["end"] if i == len(rows) - 1 else g["mid"])
            used, resets = w[b]["used"], w[b]["resets_at"]
            label = (LABELS[b] + ":").ljust(width)
            if used <= 0:
                tail = style.dim("full")
            else:
                when = fmt_reset(resets, now)
                tail = f"resets {when:<13} {style.dim('in ' + fmt_in(resets - now)) if resets else ''}".rstrip()
            out.write(f"     {tree} {label} {style.pct(used)}   {tail}\n")
    if active is None and not only:
        cred = mgr.current_login()
        if cred is not None:
            out.write(f"\n{style.warn('The current agy login')} ({cred.identity().email or 'unknown'}) "
                      f"is not managed. Run: aswap add\n")


# --- running instances -------------------------------------------------------------------


def attribute(inst: Instance, mgr: Manager, reg: Registry, active: Account | None) -> Account | None:
    """Which stored account a running agy session is on, or None when it cannot be told."""
    gd = inst.gemini_dir
    if gd is not None and mgr.isolates:
        try:
            gres = gd.expanduser().resolve()
            if gres.parent == paths.sessions_dir().resolve() and gres.name.isdigit():
                return reg.get(int(gres.name))
            if gres != mgr.gdir.resolve():
                cred = FileBackend(gres).read()
                return reg.find_identity(cred.identity()) if cred else None
        except (OSError, CredentialError):
            return None
    slot = default_account_at(inst.started, active.slot if active else None)
    return reg.get(slot) if slot else None


def instance_rows(mgr: Manager, reg: Registry, active: Account | None) -> list[dict]:
    rows = []
    for inst in list_instances():
        acct = attribute(inst, mgr, reg, active)
        rows.append({"pid": inst.pid, "mode": inst.mode, "cwd": inst.cwd,
                     "accountNumber": acct.slot if acct else None,
                     "account": account_name(acct) if acct else None,
                     "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(inst.started))
                     if inst.started else None})
    return rows


def print_instances(rows: list[dict], out: TextIO) -> None:
    if not rows:
        return
    g = style.glyphs()
    groups: dict[tuple, int] = {}
    for r in rows:
        key = (r["mode"], r["cwd"] or "?", r["accountNumber"], r["account"])
        groups[key] = groups.get(key, 0) + 1
    out.write("\n" + style.bold("Running instances:") + "\n")
    width = max(len(k[1]) for k in groups)
    for (mode, cwd, num, name), n in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        who = f"{num}: {name}" if num else "account unknown (started before a switch)"
        count = style.dim(f"({n} session{'s' if n != 1 else ''})")
        out.write(f"  {style.dim(g['dot'])} {mode:<5}  {cwd.ljust(width)}  {who}  {count}\n")
