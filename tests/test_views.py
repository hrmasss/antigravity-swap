import io
import time
from pathlib import Path

from antigravity_swap import paths, views
from antigravity_swap.cli import main, translate_legacy
from antigravity_swap.manager import default_account_at, record_switch
from antigravity_swap.processes import Instance
from antigravity_swap.store import Registry

from test_core import add_accounts, put_usage, reading


def test_legacy_flags():
    assert translate_legacy(["--list"]) == ["list"]
    assert translate_legacy(["--switch-to", "2"]) == ["switch", "2"]
    assert translate_legacy(["--remove-account", "work"]) == ["remove", "work"]
    assert translate_legacy(["list", "--json"]) == ["list", "--json"]


def test_legacy_flags_run(env, capsys):
    env.login("a@x.io")
    assert main(["--add-account"]) == 0
    assert main(["--status"]) == 0
    assert "a@x.io" in capsys.readouterr().out


def test_fmt_in():
    assert views.fmt_in(4 * 3600 + 10 * 60 + 5) == "4h 10m"
    assert views.fmt_in(4 * 86400 + 11 * 3600) == "4d 11h"
    assert views.fmt_in(30) == "now"


def test_tree_marks_active_and_shows_both_pools(env, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    mgr, reg = add_accounts(env, "a@x.io", "b@x.io")
    reg.get(1).alias = "home"
    put_usage(1, reading(g5=10, gw=42, p5=0, pw=33))
    out = io.StringIO()
    views.print_tree(mgr, reg, mgr.active(reg), out)
    text = out.getvalue()
    assert "2: b@x.io (active)" in text
    assert "1: a@x.io [home]" in text
    for label in ("Gemini 5h:", "Gemini 7d:", "Claude/GPT 5h:", "Claude/GPT 7d:"):
        assert label in text
    assert "full" in text and "resets" in text and " in " in text


def test_switch_log_attribution(env):
    t0 = time.time()
    assert default_account_at(t0, 3) == 3            # no switches ever: the current login
    record_switch(1, when=t0 + 10)
    record_switch(2, when=t0 + 20)
    assert default_account_at(t0 + 15, 2) == 1       # started between the two switches
    assert default_account_at(t0 + 25, 2) == 2       # started after the last one
    assert default_account_at(t0, 2) is None         # before any recorded switch: unknown


def test_attribute_session_dir(env):
    mgr, reg = add_accounts(env, "a@x.io", "b@x.io")
    sdir = paths.session_dir(1)
    sdir.mkdir(parents=True)
    inst = Instance(123, ["agy", f"--gemini_dir={sdir}", "-p", "x"], "/w", time.time())
    assert inst.mode == "print"
    assert views.attribute(inst, mgr, reg, mgr.active(reg)).slot == 1
    plain = Instance(124, ["agy"], "/w", time.time())
    assert plain.mode == "TUI"
    assert views.attribute(plain, mgr, reg, mgr.active(reg)).slot == 2
    assert Instance(125, ["agy", "models"], None, None).is_helper


def test_instances_grouping():
    rows = [{"pid": 1, "mode": "TUI", "cwd": "/a", "accountNumber": 1, "account": "a@x.io"},
            {"pid": 2, "mode": "TUI", "cwd": "/a", "accountNumber": 1, "account": "a@x.io"},
            {"pid": 3, "mode": "print", "cwd": "/b", "accountNumber": None, "account": None}]
    out = io.StringIO()
    views.print_instances(rows, out)
    text = out.getvalue()
    assert "Running instances:" in text
    assert "(2 sessions)" in text and "(1 session)" in text
    assert "account unknown" in text
