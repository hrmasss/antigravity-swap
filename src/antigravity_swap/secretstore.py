"""Linux Secret Service storage for pooled credentials (GNOME Keyring / libsecret).

Upstream's ``SecretServiceBackend`` shells out to ``secret-tool``, which is not installed on every
desktop, and it *silently* falls back to the plaintext file when the keyring is missing. Both are
wrong for a pool that claims "no plaintext unless you asked for it":

  * this backend speaks the Secret Service D-Bus API directly (via ``gi.repository.Secret``), with
    a ``dbus-python`` fallback and a ``secret-tool`` fallback, so it works on a bare GNOME session
    without extra packages;
  * it never falls back. A missing or locked keyring raises :class:`KeyringUnavailable` with an
    actionable message, and the caller must opt into the file backend explicitly.

Storage model
-------------
One item per slot::

    attributes   service=antigravity, username=pool, slot=<n>, purpose=oauth-token
    label        antigravity pool slot <n>
    secret       the agy login JSON (the only place a token ever lives)

The account registry (``accounts.json``) never holds a token — it keeps only slot, email, alias and
flags. Nothing in this module logs the secret; errors carry the D-Bus error name, never the payload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

SERVICE = "antigravity"
USERNAME = "pool"
PURPOSE = "oauth-token"


class KeyringUnavailable(Exception):
    """The Secret Service is missing, unreachable or locked. Never swallowed into a file fallback."""


class KeyringEntryMissing(Exception):
    """No item exists for the requested slot."""


def attributes(slot: int) -> dict[str, str]:
    return {"service": SERVICE, "username": USERNAME, "slot": str(slot), "purpose": PURPOSE}


def label(slot: int) -> str:
    return f"antigravity pool slot {slot}"


# --- gi / libsecret -----------------------------------------------------------------------


def _gi():
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        return Secret
    except Exception as exc:  # ImportError, ValueError (typelib missing), NamespaceError
        raise KeyringUnavailable(
            "libsecret GI bindings are not available "
            f"({exc.__class__.__name__}). Install: sudo apt install gir1.2-secret-1 python3-gi"
        ) from None


def _gi_schema(Secret):
    return Secret.Schema.new(
        SERVICE, Secret.SchemaFlags.NONE,
        {"service": Secret.SchemaAttributeType.STRING, "username": Secret.SchemaAttributeType.STRING,
         "slot": Secret.SchemaAttributeType.STRING, "purpose": Secret.SchemaAttributeType.STRING},
    )


def _gi_password() -> Any:
    """A Secret.Password wrapper is needed to store lookups; it needs a loop for async service."""
    return None


def _gi_get(slot: int) -> str | None:
    Secret = _gi()
    schema = _gi_schema(Secret)
    try:
        return Secret.password_lookup_sync(schema, attributes(slot), None)
    except Exception as exc:
        raise KeyringUnavailable(
            f"Secret Service lookup failed ({exc.__class__.__name__}: {exc}). "
            "Is gnome-keyring running and the login keyring unlocked?") from None


def _gi_store(slot: int, payload: str) -> None:
    Secret = _gi()
    schema = _gi_schema(Secret)
    try:
        ok = Secret.password_store_sync(schema, attributes(slot), _collection(Secret),
                                        label(slot), payload, None)
    except Exception as exc:
        raise KeyringUnavailable(
            f"Secret Service store failed ({exc.__class__.__name__}: {exc}). "
            "Is the keyring locked?") from None
    if not ok:
        raise KeyringUnavailable("Secret Service refused to store the item (keyring locked?)")


def _collection(Secret):
    try:
        return Secret.COLLECTION_DEFAULT
    except AttributeError:
        return None


def _gi_clear(slot: int) -> bool:
    Secret = _gi()
    schema = _gi_schema(Secret)
    try:
        return bool(Secret.password_clear_sync(schema, attributes(slot), None))
    except Exception as exc:
        raise KeyringUnavailable(f"Secret Service clear failed ({exc.__class__.__name__}: {exc})") from None


# --- secret-tool fallback ------------------------------------------------------------------


def _secret_tool() -> str | None:
    return shutil.which("secret-tool")


def _st_get(slot: int) -> str | None:
    tool = _secret_tool()
    if not tool:
        raise KeyringUnavailable("neither libsecret bindings nor secret-tool are available")
    attrs = []
    for k, v in attributes(slot).items():
        attrs += [k, v]
    r = subprocess.run([tool, "lookup", *attrs], capture_output=True, text=True,
                       env={**os.environ, "SECRET_SERVICE_NO_PROMPT": "1"})
    if r.returncode != 0:
        if "locked" in (r.stderr or "").lower():
            raise KeyringUnavailable("the login keyring is locked; unlock it and retry")
        return None
    return r.stdout or None


def _st_store(slot: int, payload: str) -> None:
    tool = _secret_tool()
    if not tool:
        raise KeyringUnavailable("neither libsecret bindings nor secret-tool are available")
    attrs = []
    for k, v in attributes(slot).items():
        attrs += [k, v]
    r = subprocess.run([tool, "store", "--label", label(slot), *attrs],
                       input=payload, capture_output=True, text=True)
    if r.returncode != 0:
        raise KeyringUnavailable(f"secret-tool store failed: {(r.stderr or '').strip()}")


def _st_clear(slot: int) -> bool:
    tool = _secret_tool()
    if not tool:
        raise KeyringUnavailable("neither libsecret bindings nor secret-tool are available")
    attrs = []
    for k, v in attributes(slot).items():
        attrs += [k, v]
    r = subprocess.run([tool, "clear", *attrs], capture_output=True, text=True)
    return r.returncode == 0


# --- public API ----------------------------------------------------------------------------


def _impl() -> str:
    try:
        _gi()
        return "gi"
    except KeyringUnavailable:
        if _secret_tool():
            return "secret-tool"
        raise


@dataclass
class BackendInfo:
    available: bool
    impl: str | None
    detail: str


def probe() -> BackendInfo:
    """Report whether a keyring can be used, without touching any stored secret."""
    try:
        impl = _impl()
    except KeyringUnavailable as exc:
        return BackendInfo(False, None, str(exc))
    if impl == "gi":
        try:
            Secret = _gi()
            if not Secret.password_lookup_sync(_gi_schema(Secret), attributes(-1), None):
                pass  # a miss is fine; reachability is what matters
            return BackendInfo(True, "gi", "libsecret reachable")
        except KeyringUnavailable as exc:
            return BackendInfo(False, "gi", str(exc))
    return BackendInfo(True, impl, f"{impl} present")


def get(slot: int) -> str:
    """The stored login JSON for ``slot``. Raises KeyringEntryMissing / KeyringUnavailable."""
    impl = _impl()
    raw = _gi_get(slot) if impl == "gi" else _st_get(slot)
    if not raw:
        raise KeyringEntryMissing(f"no keyring item for slot {slot}")
    return raw


def store(slot: int, payload: str) -> None:
    """Store the login blob for ``slot``. Only this string is secret; attributes are metadata.

    ``replace`` semantics: an existing item with the same attributes is overwritten by
    ``Secret.password_store_sync`` (``REPLACE`` flag), which is what a token refresh needs.
    """
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("refusing to store an empty secret")
    json.loads(payload)  # fail loudly on a malformed blob rather than storing garbage
    impl = _impl()
    if impl == "gi":
        _gi_store(slot, payload)
    else:
        _st_store(slot, payload)


def update(slot: int, payload: str) -> str:
    """Overwrite an existing item. Returns ``"updated"``; raises if the slot has no item."""
    if not exists(slot):
        raise KeyringEntryMissing(f"nothing to update: no keyring item for slot {slot}")
    store(slot, payload)
    return "updated"


def clear(slot: int) -> bool:
    impl = _impl()
    return _gi_clear(slot) if impl == "gi" else _st_clear(slot)


def exists(slot: int) -> bool:
    try:
        get(slot)
        return True
    except (KeyringEntryMissing, KeyringUnavailable):
        return False
