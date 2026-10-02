from antigravity_swap import agy


def test_env_beats_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ASWAP_AGY", "/from/env/agy")
    assert agy.find_agy("/from/config/agy") == ["/from/env/agy"]
    monkeypatch.delenv("ASWAP_AGY")
    assert agy.find_agy("/from/config/agy") == ["/from/config/agy"]
