"""The account registry: slots, labels, rotation flags and each slot's stored login."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from antigravity_swap import paths
from antigravity_swap.credentials import Credential, CredentialError, Identity
from antigravity_swap.fsutil import read_json, write_atomic, write_json

SCHEMA = 1


class StoreError(Exception):
    pass


@dataclass
class Account:
    slot: int
    email: str | None = None
    sub: str | None = None
    alias: str | None = None
    disabled: bool = False
    added_at: float = 0.0
    limited_until: float = 0.0
    limited_reason: str | None = None
    quarantined: bool = False
    quarantine_reason: str | None = None

    @property
    def label(self) -> str:
        return self.email or f"account-{self.slot}"

    def is_limited(self, now: float | None = None) -> bool:
        return self.limited_until > (now or time.time())

    def rotatable(self, now: float | None = None) -> bool:
        """Eligible for anything aswap picks on its own."""
        return not self.disabled and not self.quarantined and not self.is_limited(now)

    def identity(self) -> Identity:
        return Identity(self.email, self.sub)


@dataclass
class Registry:
    accounts: list[Account] = field(default_factory=list)
    mappings: dict[str, int] = field(default_factory=dict)

    # -- persistence ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "Registry":
        raw = read_json(paths.accounts_file(), default={}) or {}
        accts = []
        known = set(Account.__dataclass_fields__)
        for a in raw.get("accounts", []):
            accts.append(Account(**{k: v for k, v in a.items() if k in known}))
        accts.sort(key=lambda a: a.slot)
        return cls(accounts=accts, mappings={k: int(v) for k, v in (raw.get("mappings") or {}).items()})

    def save(self) -> None:
        write_json(
            paths.accounts_file(),
            {"schemaVersion": SCHEMA, "accounts": [asdict(a) for a in self.accounts], "mappings": self.mappings},
            private=True,
        )

    # -- lookup -----------------------------------------------------------------------

    def get(self, slot: int) -> Account | None:
        return next((a for a in self.accounts if a.slot == slot), None)

    def resolve(self, ref: str) -> Account:
        """Find an account by slot number, email or alias."""
        ref = str(ref).strip()
        if ref.isdigit():
            a = self.get(int(ref))
            if a:
                return a
        low = ref.lower()
        for a in self.accounts:
            if a.alias and a.alias.lower() == low:
                return a
        for a in self.accounts:
            if a.email and a.email.lower() == low:
                return a
        raise StoreError(f"no account matches '{ref}' (try: aswap list)")

    def find_identity(self, ident: Identity) -> Account | None:
        for a in self.accounts:
            if ident.matches(a.email, a.sub):
                return a
        return None

    def free_slot(self) -> int:
        used = {a.slot for a in self.accounts}
        n = 1
        while n in used:
            n += 1
        return n

    def ordered(self) -> list[Account]:
        return sorted(self.accounts, key=lambda a: a.slot)

    # -- mutation ---------------------------------------------------------------------

    def remove(self, slot: int) -> None:
        self.accounts = [a for a in self.accounts if a.slot != slot]
        self.mappings = {k: v for k, v in self.mappings.items() if v != slot}
        for p in (cred_path(slot), usage_path(slot)):
            try:
                p.unlink()
            except OSError:
                pass
        shutil.rmtree(paths.session_dir(slot), ignore_errors=True)


def cred_path(slot: int) -> Path:
    return paths.credentials_dir() / f"{slot}.json"


def usage_path(slot: int) -> Path:
    return paths.usage_dir() / f"{slot}.json"


def load_cred(slot: int) -> Credential | None:
    try:
        return Credential(cred_path(slot).read_text(encoding="utf-8"))
    except OSError:
        return None
    except CredentialError:
        return None


def save_cred(slot: int, cred: Credential) -> None:
    paths.credentials_dir().mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(paths.credentials_dir(), 0o700)
    write_atomic(cred_path(slot), cred.raw, private=True)


def capture(slot: int, cred: Credential | None) -> bool:
    """Store ``cred`` for ``slot`` if it is fresher than what is stored. Returns True if saved."""
    if cred is None:
        return False
    stored = load_cred(slot)
    if stored is not None and stored.raw == cred.raw:
        return False
    if stored is None or cred.newer_than(stored):
        save_cred(slot, cred)
        return True
    return False
