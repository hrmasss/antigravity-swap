import json

from antigravity_swap.cli import main
from antigravity_swap.store import Registry

from test_core import put_usage, reading


def test_add_list_switch_roundtrip(env, capsys):
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    env.login("a@x.io")
    assert main(["add", "--alias", "home"]) == 0
    env.login("b@x.io")
    assert main(["add"]) == 0
    put_usage(1, reading(g5=10))
    put_usage(2, reading(g5=60))
    capsys.readouterr()
    assert main(["list", "--cached", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["activeAccountNumber"] == 2
    assert [a["number"] for a in data["accounts"]] == [1, 2]
    assert data["accounts"][0]["alias"] == "home"
    assert data["accounts"][1]["usage"]["gemini-5h"]["usedPct"] == 60.0

    assert main(["switch", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["switched"] and res["to"]["number"] == 1
    assert main(["switch", "home"]) == 0
    assert "already active" in capsys.readouterr().out


def test_disable_limit_alias_remove(env, capsys):
    env.login("a@x.io")
    main(["add"])
    assert main(["disable", "1"]) == 0 and Registry.load().get(1).disabled
    assert main(["enable", "1"]) == 0 and not Registry.load().get(1).disabled
    assert main(["limit", "1", "90m"]) == 0 and Registry.load().get(1).is_limited()
    assert main(["limit", "1", "--clear"]) == 0 and not Registry.load().get(1).is_limited()
    assert main(["alias", "1", "main"]) == 0 and Registry.load().get(1).alias == "main"
    assert main(["remove", "main"]) == 0 and not Registry.load().accounts


def test_export_import(env, tmp_path, capsys):
    env.login("a@x.io")
    main(["add"])
    f = tmp_path / "backup.json"
    assert main(["export", str(f)]) == 0
    main(["purge", "--yes"])
    assert not Registry.load().accounts
    assert main(["import", str(f)]) == 0
    assert Registry.load().get(1).email == "a@x.io"
    assert main(["import", str(f)]) == 0
    assert "skipped" in capsys.readouterr().out


def test_config(env, capsys):
    assert main(["config", "set", "autoswitch.threshold", "80"]) == 0
    capsys.readouterr()
    assert main(["config", "get", "autoswitch.threshold"]) == 0
    assert capsys.readouterr().out.strip() == "80.0"
    assert main(["config", "set", "autoswitch.threshold", "500"]) == 1
    assert main(["config", "unset", "autoswitch.threshold"]) == 0


def test_status_unmanaged(env, capsys):
    env.login("z@x.io")
    assert main(["status", "--json"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["signedIn"] and not s["managed"]


def test_passthrough_only_for_run_exec(env, capsys):
    assert main(["list", "--", "-p", "x"]) == 2
