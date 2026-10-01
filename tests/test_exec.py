import io
import json
import time

import pytest

from antigravity_swap.failover import EXHAUSTED_EXIT, ExecError, plan, resume_args, run_exec
from antigravity_swap.store import Registry

from test_core import add_accounts


def test_plan_validates_and_injects_json():
    p = plan(["-p", "hello", "--model", "claude-sonnet-4-6"])
    assert p["mode"] == "text" and p["pool"] == "3p"
    assert p["args"][-2:] == ["--output-format", "json"]
    assert plan(["-p", "x", "--output-format", "stream-json"])["mode"] == "stream-json"
    for bad in (["hello"], ["-i", "x"], ["--gemini_dir=/x", "-p", "y"]):
        with pytest.raises(ExecError):
            plan(bad)


def test_resume_args_swaps_prompt_for_conversation():
    out = resume_args(["--model", "m", "-p", "first", "-c", "--output-format", "json"], "abc", "go on")
    assert out == ["--model", "m", "--output-format", "json", "--conversation", "abc", "-p", "go on"]


def run(env, args, **kw):
    out, err = io.StringIO(), io.StringIO()
    rc = run_exec(env.manager(), args, out=out, err=err, **kw)
    return rc, out.getvalue(), err.getvalue()


def test_failover_continues_conversation_on_next_account(env):
    env.plan({"a@x.io": "quota", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["-p", "build it"], target="1", strategy="rotate")
    assert rc == 0, err
    assert out == "done by b@x.io after 2 turn(s)\n"  # the turn from a@ came along
    reg = Registry.load()
    a = reg.get(1)
    assert a.is_limited()
    assert abs(a.limited_until - (time.time() + 3723)) < 30  # "Resets in 1h2m3s"
    runs = [c for c in env.calls() if c["args"] and c["args"][0] != "models"]
    assert runs[0]["email"] == "a@x.io" and "build it" in runs[0]["args"]
    assert runs[1]["email"] == "b@x.io" and "--conversation" in runs[1]["args"]
    assert "build it" not in runs[1]["args"]
    assert "hit its quota" in err


def test_failover_skips_dead_login(env):
    env.plan({"a@x.io": "dead", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["-p", "x"], target="1", strategy="rotate")
    assert rc == 0
    assert "done by b@x.io" in out
    assert Registry.load().get(1).quarantined


def test_failover_all_exhausted(env):
    env.plan({"a@x.io": "quota", "b@x.io": "quota"})
    add_accounts(env, "a@x.io", "b@x.io")
    rc, out, err = run(env, ["-p", "x"], strategy="rotate")
    assert rc == EXHAUSTED_EXIT
    assert "every account is limited" in err
    reg = Registry.load()
    assert all(a.is_limited() for a in reg.accounts)


def test_json_mode_prints_agy_json(env):
    env.plan({"a@x.io": "ok"})
    add_accounts(env, "a@x.io")
    rc, out, err = run(env, ["-p", "x", "--output-format", "json"])
    assert rc == 0
    assert json.loads(out)["status"] == "SUCCESS"


def test_limited_accounts_are_not_picked(env):
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    reg = Registry.load()
    reg.get(1).limited_until = time.time() + 3600
    reg.save()
    rc, out, err = run(env, ["-p", "x"], strategy="rotate")
    assert "done by b@x.io" in out
