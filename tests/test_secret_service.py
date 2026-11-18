"""Secret Service storage tests (synthetic secrets only).

Two layers:

  * always run — the *contract*: secrets are stored as a single opaque payload, account metadata
    carries no token, no token reaches logs, and an unavailable/locked keyring raises instead of
    silently falling back to plaintext.
  * opt-in — the *real keyring*: set ``ASWAP_TEST_KEYRING=1`` to round-trip a synthetic token
    through the live GNOME keyring under slot 999. Skipped by default so CI never touches a
    user's real keyring.

No production credential is ever read, written or moved here.
"""

from __future__ import annotations

import json
import logging
import os
import sys

import pytest

from antigravity_swap import secretstore as ss

SYNTH = json.dumps({
    "token": {"access_token": "FIXTURE-synthetic-not-a-real-token", "token_type": "Bearer",
              "refresh_token": "FIXTURE-synthetic-refresh", "expiry": "2030-01-01T00:00:00.000000000Z"},
    "auth_method": "consumer", "id_token_email": "synthetic@example.invalid",
})
SYNTH_V2 = SYNTH.replace("FIXTURE-synthetic-not-a-real-token", "FIXTURE-synthetic-token-v2")
SLOT = 999


# --- contract (no keyring needed) ----------------------------------------------------------


def test_attributes_carry_no_secret():
    attrs = ss.attributes(SLOT)
    assert set(attrs) == {"service", "username", "slot", "purpose"}
    assert attrs["slot"] == str(SLOT)
    assert attrs["purpose"] == "oauth-token"  # a *name* for the item kind, not a credential
    blob = json.dumps(attrs).lower()
    assert "fixture" not in blob
    assert "access_token" not in blob and "refresh" not in blob


def test_label_carries_no_secret():
    assert "FIXTURE" not in ss.label(SLOT)


def test_store_rejects_empty_and_malformed_payloads(monkeypatch):
    monkeypatch.setattr(ss, "_impl", lambda: "gi")
    monkeypatch.setattr(ss, "_gi_store", lambda slot, payload: pytest.fail("must not reach the keyring"))
    for bad in ("", "   ", "not json"):
        with pytest.raises(ValueError):
            ss.store(SLOT, bad)


def test_unavailable_keyring_raises_and_never_falls_back(monkeypatch):
    """The brief's hard requirement: no silent plaintext fallback."""
    monkeypatch.setattr(ss, "_gi", lambda: (_ for _ in ()).throw(ss.KeyringUnavailable("no libsecret")))
    monkeypatch.setattr(ss, "_secret_tool", lambda: None)
    with pytest.raises(ss.KeyringUnavailable) as exc:
        ss.get(SLOT)
    assert "no libsecret" in str(exc.value) or "secret-tool" in str(exc.value)


def test_locked_keyring_is_reported_not_swallowed(monkeypatch):
    def boom():
        raise ss.KeyringUnavailable("the login keyring is locked; unlock it and retry")

    monkeypatch.setattr(ss, "_impl", lambda: "gi")
    monkeypatch.setattr(ss, "_gi_store", lambda slot, payload: boom())
    with pytest.raises(ss.KeyringUnavailable) as exc:
        ss.store(SLOT, SYNTH)
    assert "locked" in str(exc.value)


def test_probe_never_reads_a_secret(monkeypatch):
    monkeypatch.setattr(ss, "_gi", lambda: (_ for _ in ()).throw(ss.KeyringUnavailable("no libsecret")))
    monkeypatch.setattr(ss, "_secret_tool", lambda: None)
    info = ss.probe()
    assert info.available is False and info.impl is None


def test_update_requires_an_existing_item(monkeypatch):
    monkeypatch.setattr(ss, "exists", lambda slot: False)
    with pytest.raises(ss.KeyringEntryMissing):
        ss.update(SLOT, SYNTH_V2)


# --- token must not reach logs -------------------------------------------------------------


def test_no_token_in_logs_or_exceptions(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def failing_store(slot, payload):
        # a plausible failure message that includes only the D-Bus error, never the payload
        raise ss.KeyringUnavailable("Secret Service store failed (DBusError): collection is locked")

    monkeypatch.setattr(ss, "_impl", lambda: "gi")
    monkeypatch.setattr(ss, "_gi_store", failing_store)
    try:
        ss.store(SLOT, SYNTH)
    except ss.KeyringUnavailable as exc:
        assert "FIXTURE" not in str(exc)
    for record in caplog.records:
        blob = record.getMessage()
        assert "FIXTURE-synthetic" not in blob, "a secret reached the log"
        assert "refresh_token" not in blob


def test_repr_of_backend_info_is_safe(monkeypatch):
    monkeypatch.setattr(ss, "_gi", lambda: (_ for _ in ()).throw(ss.KeyringUnavailable("no libsecret")))
    monkeypatch.setattr(ss, "_secret_tool", lambda: None)
    info = ss.probe()
    assert "FIXTURE" not in repr(info)


# --- real keyring, opt-in ------------------------------------------------------------------


keyring_live = pytest.mark.skipif(
    os.environ.get("ASWAP_TEST_KEYRING") != "1",
    reason="set ASWAP_TEST_KEYRING=1 to exercise the live GNOME keyring with a synthetic token",
)


@keyring_live
def test_live_keyring_roundtrip_with_synthetic_secret():
    impl = ss._impl()
    params = [sys.executable]
    if impl == "gi" and sys.executable != "/usr/bin/python3":
        pytest.skip("libsecret GI bindings live in the system interpreter on this host")
    assert ss.probe().available
    ss.clear(SLOT)
    assert ss.exists(SLOT) is False

    ss.store(SLOT, SYNTH)
    assert ss.exists(SLOT) is True
    assert json.loads(ss.get(SLOT)) == json.loads(SYNTH)

    assert ss.update(SLOT, SYNTH_V2) == "updated"
    assert json.loads(ss.get(SLOT))["token"]["access_token"] == "FIXTURE-synthetic-token-v2"

    assert ss.clear(SLOT) is True
    assert ss.exists(SLOT) is False
    with pytest.raises(ss.KeyringEntryMissing):
        ss.get(SLOT)
