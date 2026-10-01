import json
import time

import pytest

from antigravity_swap import agy
from antigravity_swap.credentials import parse_rfc3339
from antigravity_swap.fsutil import write_json
from antigravity_swap.manager import SwitchError
from antigravity_swap.settings import Settings
from antigravity_swap.store import Registry, StoreError, load_cred, usage_path
from antigravity_swap.usage import binding_used, normalize, weekly_reset

from conftest import make_cred


def reading(g5=0.0, gw=0.0, p5=0.0, pw=0.0, week_reset_in=86400):
    def iso(sec):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + sec))
    return {"ts": time.time(), "project": "p", "host": "h", "groups": normalize({"groups": [
        {"displayName": "Gemini Models", "buckets": [
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 1 - g5 / 100, "resetTime": iso(3600)},
            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 1 - gw / 100,
             "resetTime": iso(week_reset_in)}]},
        {"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 1 - p5 / 100, "resetTime": iso(3600)},
            {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1 - pw / 100,
             "resetTime": iso(week_reset_in)}]}]})}


def put_usage(slot, r):
    write_json(usage_path(slot), {"status": "ok", "reading": r, "checked_at": time.time()})


def add_accounts(env, *emails):
    reg = Registry.load()
    mgr = env.manager()
    for e in emails:
        env.login(e)
        mgr.add(reg)
    reg.save()
    return mgr, reg


# --- parsing -----------------------------------------------------------------------------


def test_rfc3339_nanoseconds():
    from datetime import datetime, timezone
    want = datetime(2026, 10, 1, 17, 51, 40, 514475, tzinfo=timezone.utc).timestamp()
    assert abs(parse_rfc3339("2026-10-01T17:51:40.514475381Z") - want) < 0.001


@pytest.mark.parametrize("text,secs", [
    ("Resets in 4h42m2s.", 4 * 3600 + 42 * 60 + 2),
    ("Resets in 54m44s", 54 * 60 + 44),
    ("Resets in 47h", 47 * 3600),
    ("Resets in ~4h40m", None),
    ("no reset here", None),
])
def test_parse_reset(text, secs):
    assert agy.parse_reset(text) == secs


def test_classify():
    assert agy.classify_failure("RESOURCE_EXHAUSTED (code 429): Individual quota reached.") == "quota"
    assert agy.classify_failure("Error: Please sign in to view available models.") == "signin"
    assert agy.classify_failure("PERMISSION_DENIED: Verify your account to continue.") == "verify"
    assert agy.classify_failure("503 No capacity available") == "error"


def test_identity_and_pools():
    c = make_cred("a@example.com")
    assert c.identity().email == "a@example.com"
    r = reading(g5=30, gw=60, p5=95)
    assert binding_used(r, "gemini") == 60
    assert binding_used(r, "3p") == 95
    assert binding_used(r, "all") == 95
    assert weekly_reset(r, "gemini") > time.time()


def test_settings_validation():
    s = Settings()
    assert s.get("autoswitch.threshold") == 90.0
    with pytest.raises(ValueError):
        s.set("autoswitch.threshold", "150")
    with pytest.raises(ValueError):
        s.set("autoswitch.strategy", "random")
    assert s.set("switch.verify", "off") is False


# --- store -------------------------------------------------------------------------------


def test_add_updates_instead_of_duplicating(env):
    mgr, reg = add_accounts(env, "a@example.com", "b@example.com")
    assert [a.slot for a in reg.accounts] == [1, 2]
    env.login("a@example.com", access="new-token")
    acct, what = mgr.add(reg)
    assert (acct.slot, what) == (1, "updated")
    assert len(reg.accounts) == 2
    assert load_cred(1).access_token == "new-token"


def test_resolve(env):
    mgr, reg = add_accounts(env, "a@example.com", "b@example.com")
    reg.accounts[1].alias = "work"
    assert reg.resolve("2").email == "b@example.com"
    assert reg.resolve("work").slot == 2
    assert reg.resolve("A@EXAMPLE.COM").slot == 1
    with pytest.raises(StoreError):
        reg.resolve("nobody")


# --- switch ------------------------------------------------------------------------------


def test_switch_writes_login_and_captures_previous(env):
    env.plan({"a@example.com": "ok", "b@example.com": "ok"})
    mgr, reg = add_accounts(env, "a@example.com", "b@example.com")
    # b is the live login now; agy renews its token while we are away
    from antigravity_swap.store import save_cred
    save_cred(1, make_cred("a@example.com", expiry_in=100))
    env.login("b@example.com", access="b-renewed", expiry_in=7200)
    res = mgr.switch(reg, reg.get(1))
    assert res.switched and res.verified
    assert env.backend.read().identity().email == "a@example.com"
    assert load_cred(2).access_token == "b-renewed"  # captured back before leaving
    assert load_cred(1).access_token == "renewed-a@example.com"  # agy models renewed it


def test_switch_to_active_is_noop(env):
    mgr, reg = add_accounts(env, "a@example.com")
    assert not mgr.switch(reg, reg.get(1)).switched


def test_switch_dead_login_quarantines_and_restores(env):
    env.plan({"a@example.com": "ok", "b@example.com": "dead"})
    mgr, reg = add_accounts(env, "a@example.com", "b@example.com")
    mgr.switch(reg, reg.get(1))
    with pytest.raises(SwitchError):
        mgr.switch(reg, reg.get(2))
    assert reg.get(2).quarantined
    assert env.backend.read().identity().email == "a@example.com"
    with pytest.raises(SwitchError):
        mgr.switch(reg, reg.get(2))  # refused while quarantined


# --- picking -----------------------------------------------------------------------------


def test_pick_strategies(env):
    mgr, reg = add_accounts(env, "a@x.io", "b@x.io", "c@x.io", "d@x.io")
    put_usage(1, reading(gw=50))
    put_usage(2, reading(gw=20, week_reset_in=5 * 86400))
    put_usage(3, reading(gw=70, week_reset_in=3600 * 5))
    put_usage(4, reading(gw=100))
    cur = reg.get(1)
    assert mgr.pick(reg, "rotate", "gemini", current=cur).slot == 2
    assert mgr.pick(reg, "best", "gemini", current=cur).slot == 2
    assert mgr.pick(reg, "consume-first", "gemini", current=cur).slot == 3
    reg.get(2).disabled = True
    assert mgr.pick(reg, "rotate", "gemini", current=cur).slot == 3
    assert mgr.pick(reg, "next-available", "gemini", current=reg.get(3)).slot == 1
    reg.get(3).limited_until = time.time() + 600
    assert mgr.pick(reg, "best", "gemini", current=cur) is None  # 4 is exhausted, 2 disabled, 3 limited


# --- auto --------------------------------------------------------------------------------


def _decide(env, mgr, reg, policy, last=0.0):
    from antigravity_swap.auto import decide
    return decide(mgr, reg, mgr.active(reg), policy, last, time.time())


def test_auto_decisions(env):
    from antigravity_swap.auto import Outcome, Policy
    mgr, reg = add_accounts(env, "a@x.io", "b@x.io", "c@x.io")
    env.login("a@x.io")
    p = Policy(threshold=90, hysteresis=10, cooldown=300)
    put_usage(1, reading(g5=50))
    put_usage(2, reading(g5=10))
    put_usage(3, reading(g5=85))
    t, why, out, _ = _decide(env, mgr, reg, p)
    assert t is None and out == Outcome.NOTHING

    put_usage(1, reading(g5=92))
    t, *_ = _decide(env, mgr, reg, p)
    assert t.slot == 2

    t, why, out, _ = _decide(env, mgr, reg, p, last=time.time())
    assert t is None and why == "cooldown"

    put_usage(1, reading(g5=100))
    t, *_ = _decide(env, mgr, reg, p, last=time.time())
    assert t.slot == 2  # exhausted skips the cooldown

    put_usage(2, reading(g5=88))
    put_usage(3, reading(g5=85))
    put_usage(1, reading(g5=93))
    t, why, out, _ = _decide(env, mgr, reg, p)
    assert t is None and "hysteresis" in why

    put_usage(2, reading(g5=95))
    put_usage(3, reading(g5=99))
    t, why, out, _ = _decide(env, mgr, reg, p)
    assert t is None and out == Outcome.BLOCKED


def test_auto_consume_first(env):
    from antigravity_swap.auto import Policy
    mgr, reg = add_accounts(env, "a@x.io", "b@x.io")
    env.login("a@x.io")
    p = Policy(strategy="consume-first")
    put_usage(1, reading(gw=20, week_reset_in=6 * 86400))
    put_usage(2, reading(gw=40, week_reset_in=86400))
    t, *_ = _decide(env, mgr, reg, p)
    assert t.slot == 2  # its weekly window is about to reset: spend it first


def test_auto_once_switches(env, monkeypatch):
    from antigravity_swap import auto, manager
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    mgr, reg = add_accounts(env, "a@x.io", "b@x.io")
    env.login("a@x.io")
    readings = {"at-a@x.io": reading(g5=97), "renewed-a@x.io": reading(g5=97),
                "at-b@x.io": reading(g5=5), "renewed-b@x.io": reading(g5=5)}
    monkeypatch.setattr(manager, "fetch", lambda tok, project=None: readings[tok])
    t = auto.tick(mgr, auto.Policy())
    assert t.outcome == auto.Outcome.SWITCHED
    assert env.backend.read().identity().email == "b@x.io"
