"""The V1.1/V1.2 acceptance table: quota-aware rotation, per pool, without over-reacting.

Every row of the brief is one test here. Nothing touches Google: the "CLI" is
``tests/fake_agy.py`` driven by a plan file, so quota errors, 403s and 503s are reproducible
without spending a single unit of real quota.

    | Account A, Claude habis                 | move to an account whose Claude works      |
    | Account A, Gemini tersedia              | Gemini still runs on Account A             |
    | Account A Claude habis; B tersedia      | automatically use Account B                |
    | A and B, Claude habis                   | a clear error naming the pool + accounts   |
    | generic HTTP 403                        | no swap, no quarantine                     |
    | Account B fails on retry                | try the next eligible account, bounded     |
"""

from __future__ import annotations

import io
import json
import time

from antigravity_swap.failover import EXHAUSTED_EXIT, plan, run_exec
from antigravity_swap.manager import with_registry
from antigravity_swap.store import Registry
from antigravity_swap.usage import pool_of_model

from test_core import add_accounts, put_usage, reading

CLAUDE = "claude-sonnet-4-6"
GEMINI = "gemini-3.8-flash-high"


def run(env, args, **kw):
    out, err = io.StringIO(), io.StringIO()
    rc = run_exec(env.manager(), args, out=out, err=err, **kw)
    return rc, out.getvalue(), err.getvalue()


def turn_calls(env):
    """Every real turn agy was asked to run (not the ``models`` probe)."""
    return [c for c in env.calls() if c["args"] and c["args"][0] != "models"]


def emails(env):
    return [c["email"] for c in turn_calls(env)]


def hold(slot, pools=None, whole=None, reason="quota: baseline model quota reached"):
    """Write a hold straight into the registry, the way a previous run would have."""
    def fn(reg):
        a = reg.get(slot)
        if pools:
            a.pool_limits = {**pools}
            a.pool_reasons = {p: reason for p in pools}
        if whole:
            a.limited_until, a.limited_reason = whole, reason
    with_registry(fn)


# --- model -> pool mapping ----------------------------------------------------------------


def test_model_maps_to_its_quota_pool():
    assert pool_of_model(CLAUDE) == "3p"
    assert pool_of_model("gpt-oss-120b-medium") == "3p"
    assert pool_of_model("Claude Sonnet 4.6 (Thinking)") == "3p"  # settings.json display name
    assert pool_of_model(GEMINI) == "gemini"
    assert pool_of_model("Gemini 3.8 Flash (High)") == "gemini"
    assert pool_of_model("some-new-model") is None  # unknown: never guessed
    assert pool_of_model(None) is None


def test_plan_reads_the_model_agy_would_use(env):
    """No ``--model``: the pool comes from agy's own settings file, not from a guess."""
    add_accounts(env, "a@x.io")
    cfg = env.gdir / "antigravity-cli" / "settings.json"
    cfg.write_text(json.dumps({"model": "Claude Sonnet 4.6 (Thinking)"}))
    assert plan(["-p", "x"], env.manager())["pool"] == "3p"
    cfg.write_text(json.dumps({"model": "Gemini 3.8 Flash (High)"}))
    assert plan(["-p", "x"], env.manager())["pool"] == "gemini"
    cfg.write_text(json.dumps({"model": "mystery"}))
    p = plan(["-p", "x"], env.manager())
    assert p["pool"] == "all" and p["pool_known"] is False


# --- row 1: Account A out of Claude quota -> move to an account whose Claude works --------


def test_claude_exhausted_moves_to_an_account_with_claude(env):
    env.plan({"a@x.io": "quota3p", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "build"], strategy="rotate")
    assert rc == 0, err
    assert "done by b@x.io" in out
    assert emails(env) == ["a@x.io", "b@x.io"]
    a = Registry.load().get(1)
    assert a.pool_limited("3p")
    assert abs(a.pool_hold("3p") - (time.time() + 3723)) < 60  # "Resets in 1h2m3s"
    assert "baseline model quota" in a.pool_reasons["3p"]
    assert a.limited_until == 0.0  # the account itself is not held out


# --- row 2: Account A still has Gemini -> Gemini keeps running on A ----------------------


def test_gemini_still_runs_on_an_account_held_for_claude(env):
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    hold(1, pools={"3p": time.time() + 4 * 86400})
    rc, out, err = run(env, ["--model", GEMINI, "-p", "x"], target="1", strategy="rotate")
    assert rc == 0, err
    assert "done by a@x.io" in out
    assert emails(env) == ["a@x.io"]  # no rotation at all
    held = Registry.load().get(1)
    assert held.pool_limited("3p") and not held.pool_limited("gemini")


def test_claude_run_skips_an_account_held_for_claude_and_says_why(env):
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    hold(1, pools={"3p": time.time() + 86400})
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], strategy="rotate")
    assert rc == 0, err
    assert "done by b@x.io" in out
    assert "skipping account 1 (a@x.io) for this Claude/GPT run" in err


def test_hold_is_per_pool_after_a_real_quota_error(env):
    """The heart of the bug: after a Claude quota error, Gemini work still runs on A."""
    env.plan({"a@x.io": "quota3p", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], strategy="rotate")
    assert rc == 0, err
    assert "done by b@x.io" in out
    a = Registry.load().get(1)
    assert a.pool_limited("3p") and not a.pool_limited("gemini")
    # a fresh Gemini run may use account A again — no manual clearing
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    rc, out, err = run(env, ["--model", GEMINI, "-p", "x"], target="1", strategy="rotate")
    assert rc == 0, err
    assert "done by a@x.io" in out


# --- row 3: Account A out of Claude, B has room -> B automatically -----------------------


def test_exec_starts_on_the_account_that_can_serve_the_pool(env):
    """With cached quota, exec must not even start on an account whose pool is used up."""
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    put_usage(1, reading(pw=100))   # Claude/GPT weekly exhausted on A
    put_usage(2, reading(pw=0))     # B is untouched
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], strategy="rotate")
    assert rc == 0, err
    assert "done by b@x.io" in out
    assert emails(env) == ["b@x.io"]


def test_a_reading_that_says_exhausted_is_never_even_tried(env):
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    put_usage(1, reading(gw=100))
    put_usage(2, reading(gw=10))
    rc, out, err = run(env, ["--model", GEMINI, "-p", "x"], target="1", strategy="rotate")
    assert rc == 0, err
    assert "done by b@x.io" in out
    assert "Gemini at 100% used" in err


# --- row 4: both accounts out of Claude -> a clear error --------------------------------


def test_both_accounts_out_of_claude_returns_a_clear_error(env):
    env.plan({"a@x.io": "quota3p", "b@x.io": "quota3p"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], strategy="rotate")
    assert rc == EXHAUSTED_EXIT
    assert "every eligible account is out of Claude/GPT quota" in err
    assert "no account can serve a Claude/GPT request" in err
    # the message must name each account and why, so the operator can act
    assert "1 a@x.io" in err and "2 b@x.io" in err
    reg = Registry.load()
    assert all(a.pool_limited("3p") for a in reg.accounts)
    assert all(not a.quarantined for a in reg.accounts)


def test_one_account_out_of_its_pool_still_serves_the_other_pool(env):
    """One account, Claude out: the Claude run stops clearly, Gemini still works."""
    env.plan({"a@x.io": "quota3p"})
    add_accounts(env, "a@x.io")
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], strategy="rotate")
    assert rc == EXHAUSTED_EXIT
    assert "Claude/GPT quota" in err
    env.plan({"a@x.io": "ok"})
    rc, out, err = run(env, ["--model", GEMINI, "-p", "x"], strategy="rotate")
    assert rc == 0, err
    assert "done by a@x.io" in out


# --- row 5: a generic 403 must not swap or quarantine -----------------------------------


def test_generic_403_never_rotates_or_quarantines(env):
    env.plan({"a@x.io": "forbidden", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], target="1", strategy="rotate")
    assert rc != 0
    assert "generic 403" in err and "NOT swapped or quarantined" in err
    assert emails(env) == ["a@x.io"]  # account B was never tried
    a = Registry.load().get(1)
    assert not a.quarantined and not a.is_limited()
    assert (a.pool_limits or {}) == {}


def test_investigate_hints_distinguish_verify_from_generic_403():
    from antigravity_swap import errors

    generic = errors.investigate_hints("PERMISSION_DENIED (code 403)", slot=2, label="b@x.io")
    assert generic and "generic 403" in generic[0] and "account 2 (b@x.io)" in generic[0]
    verify = errors.investigate_hints("PERMISSION_DENIED: Verify your account to continue.")
    assert verify and verify[0] != generic[0]
    assert errors.investigate_hints("503 UNAVAILABLE") == []


# --- row 6: the next account also fails -> try the following one, bounded ---------------


def test_second_account_failing_moves_to_the_next_one(env):
    env.plan({"a@x.io": "quota3p", "b@x.io": "quota3p", "c@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io", "c@x.io")
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], strategy="rotate")
    assert rc == 0, err
    assert "done by c@x.io" in out
    assert emails(env) == ["a@x.io", "b@x.io", "c@x.io"]
    assert "continuing on account 2" in err and "continuing on account 3" in err


def test_rotation_is_bounded_by_max_hops(env):
    env.plan({"a@x.io": "quota3p", "b@x.io": "quota3p", "c@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io", "c@x.io")
    rc, out, err = run(env, ["--model", CLAUDE, "-p", "x"], strategy="rotate", max_hops=1)
    assert rc == EXHAUSTED_EXIT
    assert "stopped after 1 account change(s)" in err
    assert emails(env) == ["a@x.io", "b@x.io"]


def test_transient_5xx_is_retried_and_never_rotates(env):
    env.plan({"a@x.io": "flaky", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["-p", "x"], target="1", strategy="rotate")
    assert rc == 0, err
    assert "done by a@x.io" in out
    assert emails(env) == ["a@x.io", "a@x.io"]  # retried in place, not rotated
    assert not Registry.load().get(1).is_limited()


def test_quota_text_is_never_retried_even_when_it_says_unavailable():
    """agy's "models remain unavailable until the weekly limit resets" must not read as 503."""
    from antigravity_swap import errors

    text = ("RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
            "Baseline model quota reached. Claude and GPT models remain unavailable until "
            "the weekly limit resets.")
    assert errors.classify_error(text) == "other"
    assert errors.plan_recovery(text, attempt=1).should_retry is False


def test_unknown_model_holds_the_whole_account(env):
    """No model identity at all: nothing narrows the hold, so it is account-wide."""
    env.plan({"a@x.io": "quota", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["-p", "x", "--model", "mystery-model"], target="1", strategy="rotate")
    assert rc == 0, err
    a = Registry.load().get(1)
    assert a.limited_until > time.time() and (a.pool_limits or {}) == {}


# --- persistence / CLI surfaces ----------------------------------------------------------


def test_registry_round_trips_pool_holds(env):
    """A pool hold must survive the JSON store, not just live in memory."""
    add_accounts(env, "a@x.io")
    until = time.time() + 3600
    hold(1, pools={"3p": until})
    a = Registry.load().get(1)
    assert abs(a.pool_hold("3p") - until) < 1
    assert a.pool_reasons["3p"].startswith("quota:")
    assert a.pool_limited("3p") and not a.pool_limited("gemini")
    assert a.rotatable(time.time(), "gemini") and not a.rotatable(time.time(), "3p")


def test_limit_clear_wipes_pool_holds(env):
    add_accounts(env, "a@x.io")
    hold(1, pools={"3p": time.time() + 3600})

    class Args:
        target, clear, duration = "1", True, None

    def clear(a):
        a.limited_until, a.limited_reason = 0.0, None
        a.pool_limits, a.pool_reasons = {}, {}
        a.quarantined, a.quarantine_reason = False, None

    from antigravity_swap import cli
    cli._set_flag(Args(), clear, "x")
    assert Registry.load().get(1).pool_limits == {}


def test_view_flags_name_the_pool(env):
    from antigravity_swap import cli

    add_accounts(env, "a@x.io")
    hold(1, pools={"3p": time.time() + 3600})
    assert "Claude/GPT out until" in cli._flags(Registry.load().get(1), time.time())


def test_user_facing_no_available_message_stays_singular_and_clear(env):
    """The refusal always starts with what cannot be done, then lists every account."""
    add_accounts(env, "a@x.io", "b@x.io")
    hold(1, whole=time.time() + 60)  # fixed: `whole` is the account-wide hold
    mgr = env.manager()
    msg = mgr.no_available_message(Registry.load(), "3p", {1})
    assert msg.startswith("no account can serve a Claude/GPT request right now")
    assert "1 a@x.io" in msg and "2 b@x.io" in msg and "already tried" in msg
