"""Operations on accounts: add, switch, read quota, pick the next one."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from antigravity_swap import agy, paths
from antigravity_swap.credentials import Backend, Credential, CredentialError, FileBackend, default_backend
from antigravity_swap.fsutil import locked, read_json, write_json
from antigravity_swap.settings import Settings
from antigravity_swap.store import Account, Registry, StoreError, capture, load_cred, save_cred, usage_path
from antigravity_swap.usage import UsageError, binding_used, fetch, weekly_reset

EXHAUSTED = 99.5


class SwitchError(Exception):
    pass


@dataclass
class SwitchResult:
    switched: bool
    frm: Account | None
    to: Account
    reason: str
    verified: bool | None = None

    def as_json(self) -> dict:
        def ref(a):
            return None if a is None else {"number": a.slot, "email": a.email, "alias": a.alias}

        return {"switched": self.switched, "from": ref(self.frm), "to": ref(self.to),
                "reason": self.reason, "verified": self.verified}


class Manager:
    def __init__(self, settings: Settings | None = None, backend: Backend | None = None):
        self.settings = settings or Settings.load()
        self.gdir = paths.gemini_dir()
        self.backend = backend or default_backend(self.gdir)
        self._agy: list[str] | None = None

    # -- basics -----------------------------------------------------------------------

    @property
    def isolates(self) -> bool:
        """True when each account can run in its own directory at the same time."""
        return self.backend.isolates_sessions

    def agy_cmd(self) -> list[str]:
        if self._agy is None:
            self._agy = agy.find_agy(self.settings.get("agy.path"))
        return self._agy

    def current_login(self) -> Credential | None:
        try:
            return self.backend.read()
        except (OSError, CredentialError):
            return None

    def active(self, reg: Registry) -> Account | None:
        cred = self.current_login()
        return reg.find_identity(cred.identity()) if cred else None

    def capture_active(self, reg: Registry) -> Account | None:
        """Save the live default login back to its slot, so a renewed token is never lost."""
        cred = self.current_login()
        if cred is None:
            return None
        acct = reg.find_identity(cred.identity())
        if acct:
            capture(acct.slot, cred)
        return acct

    # -- add --------------------------------------------------------------------------

    def add(self, reg: Registry, slot: int | None = None, alias: str | None = None,
            from_dir: Path | None = None, force: bool = False) -> tuple[Account, str]:
        if from_dir is not None:
            cred = FileBackend(Path(from_dir)).read()
            if cred is None:
                raise StoreError(f"no agy login in {from_dir}")
        else:
            cred = self.current_login()
            if cred is None:
                raise StoreError("agy is not signed in. Run agy, sign in, then: aswap add")
        ident = cred.identity()
        if not ident.email and not ident.sub:
            raise StoreError("this login carries no identity (no id_token); sign in again with agy")
        existing = reg.find_identity(ident)
        if existing and (slot is None or slot == existing.slot):
            save_cred(existing.slot, cred)
            existing.email, existing.sub = ident.email or existing.email, ident.sub or existing.sub
            if alias:
                self._check_alias(reg, alias, existing.slot)
                existing.alias = alias
            existing.quarantined, existing.quarantine_reason = False, None
            return existing, "updated"
        if existing and slot is not None and slot != existing.slot:
            raise StoreError(f"{ident.email} is already account {existing.slot}")
        if slot is not None and reg.get(slot):
            if not force:
                raise StoreError(f"slot {slot} holds {reg.get(slot).label}; pass --force to overwrite")
            reg.remove(slot)
        slot = slot or reg.free_slot()
        if alias:
            self._check_alias(reg, alias, slot)
        acct = Account(slot=slot, email=ident.email, sub=ident.sub, alias=alias, added_at=time.time())
        reg.accounts.append(acct)
        reg.accounts.sort(key=lambda a: a.slot)
        save_cred(slot, cred)
        return acct, "added"

    @staticmethod
    def _check_alias(reg: Registry, alias: str, slot: int) -> None:
        if alias.isdigit():
            raise StoreError("an alias cannot be a number; numbers are slots")
        for a in reg.accounts:
            if a.slot != slot and a.alias and a.alias.lower() == alias.lower():
                raise StoreError(f"alias '{alias}' already names account {a.slot}")

    # -- switch -----------------------------------------------------------------------

    def switch(self, reg: Registry, target: Account, verify: bool | None = None,
               force: bool = False) -> SwitchResult:
        """Make ``target`` the login plain ``agy`` uses. New agy launches pick it up."""
        if target.quarantined and not force:
            raise SwitchError(f"account {target.slot} is quarantined ({target.quarantine_reason}); "
                              f"sign in with it and run: aswap add --slot {target.slot}")
        prev = self.capture_active(reg)
        if prev and prev.slot == target.slot and not force:
            return SwitchResult(False, prev, target, "already active")
        cred = self._freshest(target)
        if cred is None:
            raise SwitchError(f"no stored login for account {target.slot}")
        before = self.current_login()
        self.backend.write(cred)
        verify = self.settings.get("switch.verify") if verify is None else verify
        verified = None
        if verify:
            try:
                check = agy.check_login(self.agy_cmd(), self.gdir)
            except agy.AgyNotFound:
                check = None
            if check is not None:
                verified = check.ok
                if not check.ok and check.kind in ("signin", "verify"):
                    target.quarantined = True
                    target.quarantine_reason = "login rejected" if check.kind == "signin" else "account needs verification"
                    if before is not None:
                        self.backend.write(before)
                    raise SwitchError(f"account {target.slot} login does not work ({check.message}); "
                                      f"switched back. It is quarantined until you re-add it.")
                if check.ok:
                    capture(target.slot, self.current_login())
        record_switch(target.slot)
        return SwitchResult(True, prev, target, "switched", verified)

    def _freshest(self, acct: Account) -> Credential | None:
        """The stored login, or the session directory's copy if agy renewed it there."""
        stored = load_cred(acct.slot)
        if self.isolates:
            sess = FileBackend(paths.session_dir(acct.slot))
            if sess.path.exists():
                try:
                    live = sess.read()
                except CredentialError:
                    live = None
                if live is not None and live.identity().matches(acct.email, acct.sub) and live.newer_than(stored):
                    save_cred(acct.slot, live)
                    return live
        return stored

    # -- quota ------------------------------------------------------------------------

    def cached(self, acct: Account) -> dict:
        return read_json(usage_path(acct.slot), default={}) or {}

    def usage_age(self, acct: Account) -> float | None:
        rec = self.cached(acct)
        ts = (rec.get("reading") or {}).get("ts")
        return None if not ts else time.time() - ts

    def refresh_usage(self, reg: Registry, acct: Account, force: bool = False,
                      active: Account | None = None, max_age: float | None = None) -> dict:
        """Fetch quota for one account unless the cached reading is fresh enough."""
        rec = self.cached(acct)
        if max_age is None:
            max_age = float(self.settings.get("usage.max_age_seconds"))
        age = self.usage_age(acct)
        if not force and age is not None and age < max_age and rec.get("status") == "ok":
            return rec
        is_active = active is not None and active.slot == acct.slot
        cred, renewed = self._usage_cred(acct, is_active)
        status, err = "ok", None
        reading = None
        for attempt in range(2):
            if cred is None:
                status, err = "no_login", "no stored login"
                break
            if cred.expired() and not renewed:
                cred, renewed, renew_status = self._renew(reg, acct, is_active)
                if renew_status:
                    status, err = renew_status
                    break
            try:
                reading = fetch(cred.access_token or "", (rec.get("reading") or {}).get("project"))
                status, err = "ok", None
                break
            except UsageError as e:
                if e.kind == "auth" and not renewed and attempt == 0:
                    cred, renewed, renew_status = self._renew(reg, acct, is_active)
                    if renew_status:
                        status, err = renew_status
                        break
                    continue
                status = "token_expired" if e.kind == "auth" else "unavailable"
                err = e.kind
                break
        new = {"status": status, "error": err, "checked_at": time.time(),
               "reading": reading or rec.get("reading")}
        write_json(usage_path(acct.slot), new, private=True)
        return new

    def _usage_cred(self, acct: Account, is_active: bool) -> tuple[Credential | None, bool]:
        if is_active:
            cred = self.current_login()
            if cred is not None:
                capture(acct.slot, cred)
                return cred, False
        return self._freshest(acct), False

    def _renew(self, reg: Registry, acct: Account, is_active: bool):
        """Renew one account's access token with ``agy models``. Returns (cred, renewed, failure)."""
        if not self.settings.get("usage.renew"):
            return load_cred(acct.slot), True, ("token_expired", "renew disabled")
        try:
            cmd = self.agy_cmd()
        except agy.AgyNotFound:
            return load_cred(acct.slot), True, ("token_expired", "agy not found")
        if is_active:
            check = agy.check_login(cmd, self.gdir)
            cred = self.current_login()
            if cred is not None:
                capture(acct.slot, cred)
        elif self.isolates:
            sdir = agy.prepare_session(acct.slot, default_gdir=self.gdir)
            check = agy.check_login(cmd, sdir)
            cred = FileBackend(sdir).read()
            capture(acct.slot, cred)
        else:
            return load_cred(acct.slot), True, ("token_expired", "only the active login renews on this platform")
        if not check.ok:
            if check.kind in ("signin", "verify"):
                acct.quarantined = True
                acct.quarantine_reason = "login rejected" if check.kind == "signin" else "account needs verification"
                return cred, True, ("dead_login", check.message)
            return cred, True, ("unavailable", check.message or check.kind)
        return cred, True, None

    def refresh_all(self, reg: Registry, force: bool = False) -> None:
        active = self.active(reg)
        for a in reg.ordered():
            if a.disabled and not force:
                continue
            self.refresh_usage(reg, a, force=force, active=active)

    # -- picking ----------------------------------------------------------------------

    def used(self, acct: Account, pool: str) -> float | None:
        return binding_used(self.cached(acct).get("reading"), pool)

    def pick(self, reg: Registry, strategy: str, pool: str, current: Account | None = None,
             exclude: set[int] | None = None, threshold: float = 90.0) -> Account | None:
        exclude = set(exclude or ())
        now = time.time()
        order = reg.ordered()
        pool_ = [a for a in order if a.rotatable(now) and a.slot not in exclude]
        if current is not None:
            pool_ = [a for a in pool_ if a.slot != current.slot]
        if not pool_:
            return None
        if strategy in ("rotate", "next-available"):
            if strategy == "next-available":
                pool_ = [a for a in pool_ if (self.used(a, pool) or 0) < EXHAUSTED]
                if not pool_:
                    return None
            start = current.slot if current else 0
            after = [a for a in pool_ if a.slot > start]
            return (after or pool_)[0]
        scored = [(a, self.used(a, pool)) for a in pool_]
        scored = [(a, u) for a, u in scored if u is None or u < EXHAUSTED]
        if not scored:
            return None
        if strategy == "consume-first":
            room = [(a, u) for a, u in scored if u is None or u < threshold]
            if not room:
                room = scored
            def key(item):
                a, u = item
                wr = weekly_reset(self.cached(a).get("reading"), pool)
                return (u is None, wr if wr is not None else float("inf"), u or 0, a.slot)
            return min(room, key=key)[0]
        return min(scored, key=lambda item: (item[1] is None, item[1] or 0, item[0].slot))[0]


SWITCH_LOG_LEN = 200


def record_switch(slot: int, when: float | None = None) -> None:
    """Remember when the default login changed, so running sessions can be attributed."""
    st = read_json(paths.state_file(), default={}) or {}
    log = st.get("switches") or []
    log.append({"ts": when or time.time(), "slot": slot})
    st["switches"] = log[-SWITCH_LOG_LEN:]
    write_json(paths.state_file(), st)


def default_account_at(ts: float | None, current_slot: int | None) -> int | None:
    """Which slot the default login was at time ``ts``. None when it cannot be known."""
    log = (read_json(paths.state_file(), default={}) or {}).get("switches") or []
    if ts is None:
        return current_slot if not log else None
    later = [e for e in log if e["ts"] > ts]
    if not later:
        return current_slot
    earlier = [e for e in log if e["ts"] <= ts]
    return earlier[-1]["slot"] if earlier else None


def with_registry(fn):
    """Run ``fn(reg)`` under the store lock and save the registry afterwards."""
    with locked():
        reg = Registry.load()
        out = fn(reg)
        reg.save()
        return out
