"""``aswap auto``: watch the active account's quota and switch before it runs out.

A switch changes the login that *new* agy launches use. A session that is already running
keeps the account it started with (agy holds its token in memory), so pair this with
``aswap exec`` for long unattended runs.
"""

from __future__ import annotations

import enum
import json
import sys
import time
from dataclasses import dataclass, field
from typing import TextIO

from antigravity_swap import paths
from antigravity_swap.fsutil import locked, read_json, write_json
from antigravity_swap.manager import EXHAUSTED, Manager, SwitchError
from antigravity_swap.store import Registry
from antigravity_swap.usage import binding_used, weekly_reset

IDLE_POLL = 600.0       # other accounts: re-read about every ten minutes
OTHERS_PER_TICK = 2     # bounded, so traffic stays flat however many accounts there are
CONSUME_FIRST_MARGIN = 3600.0


class Outcome(enum.IntEnum):
    SWITCHED = 0
    ERROR = 1
    NOTHING = 2
    BLOCKED = 3


@dataclass
class Policy:
    threshold: float = 90.0
    interval: float = 60.0
    cooldown: float = 300.0
    hysteresis: float = 10.0
    strategy: str = "best"
    pool: str = "gemini"
    dry_run: bool = False

    @classmethod
    def from_settings(cls, s, **over) -> "Policy":
        p = cls(
            threshold=float(s.get("autoswitch.threshold")),
            interval=float(s.get("autoswitch.interval_seconds")),
            cooldown=float(s.get("autoswitch.cooldown_seconds")),
            hysteresis=float(s.get("autoswitch.hysteresis_pct")),
            strategy=s.get("autoswitch.strategy"),
            pool=s.get("autoswitch.pool"),
        )
        for k, v in over.items():
            if v is not None:
                setattr(p, k, v)
        return p


@dataclass
class Tick:
    outcome: Outcome
    events: list[dict] = field(default_factory=list)
    wake_at: float | None = None


def _ref(a) -> dict | None:
    return None if a is None else {"number": a.slot, "email": a.email}


def _ev(kind: str, **kw) -> dict:
    return {"schemaVersion": 1, "event": kind, "ts": time.time(), **kw}


def decide(mgr: Manager, reg: Registry, active, policy: Policy, last_switch: float, now: float):
    """Pure decision step: returns (target or None, reason, outcome_if_no_switch)."""
    if active.is_limited(now):
        used = 100.0
    else:
        used = binding_used(mgr.cached(active).get("reading"), policy.pool)
    if used is None:
        return None, "active account usage unknown; holding", Outcome.NOTHING, used
    exhausted = used >= EXHAUSTED or active.is_limited(now)
    candidates = []
    for a in reg.ordered():
        if a.slot == active.slot or not a.rotatable(now):
            continue
        u = binding_used(mgr.cached(a).get("reading"), policy.pool)
        if u is None or u >= policy.threshold:
            continue
        candidates.append((a, u))

    if policy.strategy == "consume-first":
        if not candidates:
            if used >= policy.threshold:
                return None, "every account is at or over the threshold", Outcome.BLOCKED, used
            return None, "no other account has room", Outcome.NOTHING, used
        mine = weekly_reset(mgr.cached(active).get("reading"), policy.pool) or float("inf")
        best = min(candidates, key=lambda c: (weekly_reset(mgr.cached(c[0]).get("reading"), policy.pool)
                                              or float("inf"), c[1], c[0].slot))
        theirs = weekly_reset(mgr.cached(best[0]).get("reading"), policy.pool) or float("inf")
        if used >= policy.threshold or theirs + CONSUME_FIRST_MARGIN < mine:
            if not exhausted and now - last_switch < policy.cooldown:
                return None, "cooldown", Outcome.NOTHING, used
            return best[0], ("over threshold" if used >= policy.threshold
                             else "another account's weekly window resets sooner"), None, used
        return None, f"{used:.0f}% used, under {policy.threshold:.0f}%", Outcome.NOTHING, used

    if used < policy.threshold:
        return None, f"{used:.0f}% used, under {policy.threshold:.0f}%", Outcome.NOTHING, used
    better = [(a, u) for a, u in candidates if exhausted or used - u >= policy.hysteresis]
    if not better:
        if not candidates:
            return None, "every account is at or over the threshold", Outcome.BLOCKED, used
        return None, "no account is better by the hysteresis margin", Outcome.NOTHING, used
    if not exhausted and now - last_switch < policy.cooldown:
        return None, "cooldown", Outcome.NOTHING, used
    return min(better, key=lambda c: (c[1], c[0].slot))[0], "over threshold", None, used


def tick(mgr: Manager, policy: Policy) -> Tick:
    now = time.time()
    state = read_json(paths.state_file(), default={}) or {}
    with locked():
        reg = Registry.load()
        active = mgr.capture_active(reg)
        if active is None:
            reg.save()
            return Tick(Outcome.NOTHING, [_ev("no-switch", reason="the current agy login is not a managed account")])
        events = []
        try:
            mgr.refresh_usage(reg, active, active=active, max_age=max(policy.interval * 0.9, 15))
            others = sorted((a for a in reg.ordered() if a.slot != active.slot and not a.disabled),
                            key=lambda a: mgr.usage_age(a) if mgr.usage_age(a) is not None else 1e12,
                            reverse=True)
            for a in others[:OTHERS_PER_TICK]:
                mgr.refresh_usage(reg, a, active=active, max_age=IDLE_POLL)
        except Exception as e:  # keep trusting last-known numbers
            events.append(_ev("error", message=f"usage poll failed: {e}"))
        target, reason, outcome, used = decide(mgr, reg, active, policy, state.get("last_switch", 0.0), now)
        events.append(_ev("poll", active=_ref(active), used=used, pool=policy.pool))
        if target is None:
            reg.save()
            kind = "all-exhausted" if outcome == Outcome.BLOCKED else "no-switch"
            t = Tick(outcome, events + [_ev(kind, reason=reason)])
            if outcome == Outcome.BLOCKED:
                resets = [x for x in (a.limited_until for a in reg.accounts) if x > now]
                t.wake_at = min(resets) if resets else None
            return t
        if policy.dry_run:
            reg.save()
            return Tick(Outcome.NOTHING, events + [_ev("would-switch", frm=_ref(active), to=_ref(target),
                                                         reason=reason)])
        try:
            res = mgr.switch(reg, target)
        except SwitchError as e:
            reg.save()
            return Tick(Outcome.ERROR, events + [_ev("error", message=str(e))])
        reg.save()
    write_json(paths.state_file(), {**state, "last_switch": now})
    return Tick(Outcome.SWITCHED, events + [_ev("switch", frm=_ref(active), to=_ref(target), reason=reason,
                                                  verified=res.verified)])


def render(ev: dict) -> str:
    k = ev["event"]
    t = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
    if k == "poll":
        u = ev.get("used")
        a = ev.get("active") or {}
        return f"{t}  account {a.get('number')} {a.get('email') or ''}: " + \
            ("usage unknown" if u is None else f"{u:.0f}% used ({ev.get('pool')})")
    if k == "switch":
        return f"{t}  switched {ev['frm']['number']} -> {ev['to']['number']} ({ev['reason']})"
    if k == "would-switch":
        return f"{t}  would switch {ev['frm']['number']} -> {ev['to']['number']} ({ev['reason']}) [dry run]"
    if k == "all-exhausted":
        return f"{t}  blocked: {ev['reason']}"
    if k == "no-switch":
        return f"{t}  staying: {ev['reason']}"
    return f"{t}  {k}: {ev.get('message', '')}"


def run(mgr: Manager, policy: Policy, once: bool = False, as_json: bool = False,
        out: TextIO = sys.stdout) -> int:
    def emit(t: Tick):
        for ev in t.events:
            if as_json:
                out.write(json.dumps(ev) + "\n")
            elif ev["event"] != "poll" or once:
                out.write(render(ev) + "\n")
        out.flush()

    if once:
        t = tick(mgr, policy)
        emit(t)
        return int(t.outcome)
    try:
        while True:
            t = tick(mgr, policy)
            emit(t)
            delay = policy.interval
            if t.outcome == Outcome.BLOCKED:
                delay = policy.interval * 5
                if t.wake_at:
                    delay = max(policy.interval, min(delay, t.wake_at - time.time() + 5))
            time.sleep(delay)
    except KeyboardInterrupt:
        return 0
