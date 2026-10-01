"""Reading and writing agy's stored login.

agy keeps one JSON blob per login::

    {"token": {"access_token", "token_type", "refresh_token", "expiry"},
     "auth_method": "...", "id_token": "<JWT>"}

Where the blob lives depends on the platform:

* Linux without a desktop keyring: a 0600 file, ``<gemini_dir>/antigravity-cli/antigravity-oauth-token``.
  ``agy --gemini_dir=<dir>`` reads the file in that directory, so several accounts can run
  side by side. This is the only backend that isolates sessions.
* Windows: Credential Manager, generic credential ``gemini:antigravity``. It is global:
  ``--gemini_dir`` does not change which login agy uses.
* macOS and desktop Linux: the system keyring (service ``gemini``, user ``antigravity``).
  Also global. These two are experimental.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from antigravity_swap.fsutil import write_atomic
from antigravity_swap.paths import IS_MACOS, IS_WINDOWS, token_file

KEYRING_SERVICE = "gemini"
KEYRING_USER = "antigravity"
WINDOWS_TARGET = f"{KEYRING_SERVICE}:{KEYRING_USER}"


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    email: str | None
    sub: str | None

    def matches(self, email: str | None, sub: str | None) -> bool:
        if self.sub and sub:
            return self.sub == sub
        if self.email and email:
            return self.email.lower() == email.lower()
        return False


class Credential:
    """One agy login blob. Kept as the raw string so nothing agy wrote is lost."""

    def __init__(self, raw: str):
        try:
            self.data = json.loads(raw)
        except ValueError as e:
            raise CredentialError(f"stored login is not JSON: {e}") from None
        if not isinstance(self.data, dict) or not isinstance(self.data.get("token"), dict):
            raise CredentialError("stored login has no token object")
        self.raw = raw

    @property
    def token(self) -> dict:
        return self.data["token"]

    @property
    def access_token(self) -> str | None:
        return self.token.get("access_token")

    @property
    def refresh_token(self) -> str | None:
        return self.token.get("refresh_token")

    @property
    def expiry(self) -> float | None:
        return parse_rfc3339(self.token.get("expiry"))

    def expired(self, now: float | None = None, margin: float = 60) -> bool:
        exp = self.expiry
        if exp is None:
            return False
        return (now or time.time()) + margin >= exp

    def identity(self) -> Identity:
        claims = jwt_claims(self.data.get("id_token"))
        return Identity(email=claims.get("email"), sub=claims.get("sub"))

    def newer_than(self, other: "Credential | None") -> bool:
        if other is None:
            return True
        a, b = self.expiry or 0, other.expiry or 0
        return a > b


def parse_rfc3339(value) -> float | None:
    """Go writes RFC3339 with up to nine fractional digits; Python accepts six."""
    if not isinstance(value, str) or not value:
        return None
    s = value.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        cut = len(rest)
        for i, ch in enumerate(rest):
            if not ch.isdigit():
                cut = i
                break
        s = head + "." + rest[: min(cut, 6)] + rest[cut:]
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def jwt_claims(token) -> dict:
    """Payload of a JWT, unverified. Used only to label an account, never to trust it."""
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


# --- backends -------------------------------------------------------------------------


class Backend:
    name = "?"
    isolates_sessions = False

    def read(self) -> Credential | None:
        raise NotImplementedError

    def write(self, cred: Credential) -> None:
        raise NotImplementedError


class FileBackend(Backend):
    name = "file"
    isolates_sessions = True

    def __init__(self, gdir: Path):
        self.gdir = gdir
        self.path = token_file(gdir)

    def read(self) -> Credential | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        return Credential(raw)

    def write(self, cred: Credential) -> None:
        write_atomic(self.path, cred.raw, private=True)


class WindowsBackend(Backend):
    name = "wincred"

    def __init__(self, target: str = WINDOWS_TARGET, user: str = KEYRING_USER):
        self.target = target
        self.user = user

    def _api(self):
        import ctypes
        import ctypes.wintypes as w

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", w.DWORD),
                ("Type", w.DWORD),
                ("TargetName", w.LPWSTR),
                ("Comment", w.LPWSTR),
                ("LastWritten", w.FILETIME),
                ("CredentialBlobSize", w.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", w.DWORD),
                ("AttributeCount", w.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", w.LPWSTR),
                ("UserName", w.LPWSTR),
            ]

        adv = ctypes.windll.advapi32
        adv.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        adv.CredReadW.restype = w.BOOL
        adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), w.DWORD]
        adv.CredWriteW.restype = w.BOOL
        adv.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
        adv.CredDeleteW.restype = w.BOOL
        return ctypes, CREDENTIAL, adv

    def read(self) -> Credential | None:
        ctypes, CREDENTIAL, adv = self._api()
        p = ctypes.POINTER(CREDENTIAL)()
        if not adv.CredReadW(self.target, 1, 0, ctypes.byref(p)):
            return None
        try:
            blob = ctypes.string_at(p.contents.CredentialBlob, p.contents.CredentialBlobSize)
        finally:
            adv.CredFree(p)
        return Credential(blob.decode("utf-8"))

    def write(self, cred: Credential) -> None:
        ctypes, CREDENTIAL, adv = self._api()
        blob = cred.raw.encode("utf-8")
        if len(blob) > 2560:
            raise CredentialError(f"login is {len(blob)} bytes; Credential Manager holds at most 2560")
        buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
        c = CREDENTIAL()
        c.Type = 1  # CRED_TYPE_GENERIC
        c.TargetName = self.target
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
        c.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE, what agy uses
        c.UserName = self.user
        if not adv.CredWriteW(ctypes.byref(c), 0):
            raise CredentialError(f"CredWriteW failed (error {ctypes.GetLastError()})")

    def delete(self) -> bool:
        _, _, adv = self._api()
        return bool(adv.CredDeleteW(self.target, 1, 0))


GO_KEYRING_PREFIX = "go-keyring-base64:"


class MacKeychainBackend(Backend):
    """Experimental. Mirrors how go-keyring stores a generic password on macOS."""

    name = "keychain"

    def read(self) -> Credential | None:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYRING_SERVICE, "-a", KEYRING_USER, "-w"],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        val = r.stdout.strip()
        if val.startswith(GO_KEYRING_PREFIX):
            val = base64.b64decode(val[len(GO_KEYRING_PREFIX):]).decode("utf-8")
        return Credential(val)

    def write(self, cred: Credential) -> None:
        enc = GO_KEYRING_PREFIX + base64.b64encode(cred.raw.encode("utf-8")).decode()
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYRING_SERVICE, "-a", KEYRING_USER, "-w", enc],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise CredentialError(f"security add-generic-password failed: {r.stderr.strip()}")


class SecretServiceBackend(Backend):
    """Experimental. Desktop Linux keyring via secret-tool, with go-keyring's attributes."""

    name = "secret-service"

    def read(self) -> Credential | None:
        r = subprocess.run(
            ["secret-tool", "lookup", "service", KEYRING_SERVICE, "username", KEYRING_USER],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return Credential(r.stdout.strip())

    def write(self, cred: Credential) -> None:
        r = subprocess.run(
            ["secret-tool", "store", "--label", "Password for 'antigravity' on 'gemini'",
             "service", KEYRING_SERVICE, "username", KEYRING_USER],
            input=cred.raw, capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise CredentialError(f"secret-tool store failed: {r.stderr.strip()}")


def default_backend(gdir: Path) -> Backend:
    """The backend plain ``agy`` reads its login from on this machine.

    ``ASWAP_BACKEND`` (file, wincred, keychain, secret-service) overrides the detection.
    """
    import os

    fb = FileBackend(gdir)
    forced = os.environ.get("ASWAP_BACKEND", "").strip().lower()
    if forced:
        table = {"file": lambda: fb, "wincred": WindowsBackend, "keychain": MacKeychainBackend,
                 "secret-service": SecretServiceBackend}
        if forced not in table:
            raise CredentialError(f"ASWAP_BACKEND must be one of: {', '.join(table)}")
        return table[forced]()
    if IS_WINDOWS:
        return WindowsBackend()
    if fb.path.exists():
        return fb
    if IS_MACOS:
        kc = MacKeychainBackend()
        try:
            if kc.read() is not None:
                return kc
        except (OSError, CredentialError):
            pass
        return fb
    if shutil.which("secret-tool"):
        ss = SecretServiceBackend()
        try:
            if ss.read() is not None:
                return ss
        except (OSError, CredentialError):
            pass
    return fb
