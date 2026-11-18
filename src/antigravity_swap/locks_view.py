"""`aswap locks` — operator visibility for the per-slot writer locks.

The lock file is an flock owned by the kernel; this command never breaks a lock, it only reports
so a human can decide. ``--json`` is the machine-readable form used by the audit scripts.
"""

from __future__ import annotations

import json

from antigravity_swap import slotlock
from antigravity_swap.store import Registry


def cmd_locks(mgr, args):
    rows = slotlock.active_locks()
    reg = Registry.load()
    by_slot = {a.slot: a for a in reg.ordered()}
    for r in rows:
        acct = by_slot.get(r["slot"])
        r["account"] = acct.label if acct else None
    if getattr(args, "json", False):
        print(json.dumps({"locks": rows}, indent=2))
        return 0
    if not rows:
        print("no slot locks have been taken yet")
        return 0
    print(f"{'slot':>4}  {'state':<10} {'pid':>7}  {'age':>8}  account")
    for r in sorted(rows, key=lambda x: x["slot"]):
        owner = r.get("owner") or {}
        started = owner.get("started")
        age = "-"
        if started:
            secs = max(0, int(__import__("time").time() - started))
            age = f"{secs // 60}m{secs % 60:02d}s"
        if r["held"]:
            state = "held"
        elif r["stale"]:
            state = "stale"
        else:
            state = "free"
        print(f"{r['slot']:>4}  {state:<10} {str(owner.get('pid') or '-'):>7}  {age:>8}  {r.get('account') or ''}")
    held = [r for r in rows if r["held"]]
    if held:
        print(f"\n{len(held)} slot(s) in use. A second writer on the same slot is refused "
              f"immediately; other slots are unaffected.")
    return 0
