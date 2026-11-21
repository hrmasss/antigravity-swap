"""Error classification and bounded retry for agy runs.

Upstream ``agy.classify_failure`` knows four buckets: ``quota`` (429 / "Individual quota
reached"), ``signin``, ``verify`` and ``error`` — and ``failover.run_exec`` treats everything
that is not quota/signin/verify as "abort, do not rotate". That is wrong for two whole classes:

* **401** — the access token was rejected. The right move is a single renewal through the
  official path (``agy models``, which refreshes the token and spends no quota), then one retry;
  only if that also fails is the turn stopped. The account is *not* deleted or quarantined
  silently.
* **5xx / connection errors** — transient upstream failures. Bounded retry with exponential
  backoff and full jitter, never more than ``MAX_ATTEMPTS``; non-retryable 4xx are not retried.

403 is deliberately left alone: a generic ``PERMISSION_DENIED`` (for example the quota RPC's own
403) is *not* an exhaustion signal and must not quarantine an account; only the explicit
"Verify your account" / "not eligible" text does, and that stays in ``agy.classify_failure``.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Callable

MAX_ATTEMPTS = 3
AUTH_MAX_ATTEMPTS = 2   # the brief allows exactly one retry after a token refresh
BASE_DELAY_S = 1.0
MAX_DELAY_S = 30.0
RETRY_BUDGET_S = 120.0

AUTH_RE = re.compile(
    r"\b401\b|UNAUTHENTICATED|invalid authentication credentials|Request had invalid authentication"
    r"|access token rejected|token has expired|Invalid API key", re.I)
TRANSIENT_RE = re.compile(r"\b(?:50[0-4])\b|UNAVAILABLE|INTERNAL\b|Bad gateway|overloaded"
                          r"|connection reset|connection refused|temporarily unavailable|deadline exceeded", re.I)
NON_RETRYABLE_RE = re.compile(r"\b(?:400|404|405|409|410|411|413|414|415|422)\b|INVALID_ARGUMENT"
                              r"|FAILED_PRECONDITION|not found", re.I)
VERIFY_RE = re.compile(r"Verify your account|not eligible for Antigravity", re.I)
QUOTA_TEXT_RE = re.compile(r"RESOURCE_EXHAUSTED|Individual quota reached|Baseline model quota reached"
                           r"|\bcode 429\b", re.I)


def retryable(kind: str) -> bool:
    return kind in ("transient", "auth")


def investigate_hints(text: str, slot: int | None = None, label: str | None = None) -> list[str]:
    """Operator-facing notes for an error that must *not* trigger rotation.

    A generic 403 is the case this exists for: the brief requires that it neither swaps
    accounts nor quarantines one, because the same 403 also comes from the quota RPC itself
    (non-CLI User-Agent) and from a stale project id. The run stops; these lines say why and
    what to check, so "stopped" does not read as "broken".
    """
    who = f"account {slot} ({label})" if slot is not None else "the account"
    if re.search(r"\b403\b|\bPERMISSION_DENIED\b", text or ""):
        if VERIFY_RE.search(text or ""):
            return [f"{who}: 403 asks for account verification — quarantine and re-add that login"]
        return [f"{who}: generic 403 — not treated as exhaustion: the account is NOT swapped or "
                f"quarantined. Check the quota RPC's User-Agent and the cached project id, then retry"]
    return []


def classify_error(text: str) -> str:
    """One of ``auth`` | ``transient`` | ``non_retryable`` | ``other``.

    Runs *after* ``agy.classify_failure``: quota/signin/verify keep their existing handling.
    Quota text is checked first and always lands in ``other``: an exhaustion message must
    never enter the retry loop, even when the same blob also says "unavailable" (agy's
    "models remain unavailable until the weekly limit resets" wording does exactly that).
    """
    if not text:
        return "other"
    if QUOTA_TEXT_RE.search(text) or VERIFY_RE.search(text):
        return "other"  # handled as quota/verify upstream; never a retry candidate
    if NON_RETRYABLE_RE.search(text):
        return "non_retryable"
    if AUTH_RE.search(text):
        return "auth"
    if TRANSIENT_RE.search(text):
        return "transient"
    return "other"


def backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with full jitter, clamped; ``Retry-After`` wins when present."""
    if retry_after is not None and retry_after >= 0:
        return min(max(retry_after, 0.0), MAX_DELAY_S)
    ceiling = min(MAX_DELAY_S, BASE_DELAY_S * (2 ** max(attempt - 1, 0)))
    return random.uniform(0.0, ceiling)


def parse_retry_after(text: str) -> float | None:
    """``Retry-After: 7`` / ``retry after 7 seconds`` if the CLI surfaces one."""
    m = re.search(r"retry[- ]after[:\s]+(\d+(?:\.\d+)?)", text, re.I)
    return float(m.group(1)) if m else None


@dataclass
class RecoveryPlan:
    kind: str
    attempts: int = 1
    delay_s: float = 0.0
    renew: bool = False
    reason: str = ""

    @property
    def should_retry(self) -> bool:
        if self.kind == "auth":
            return self.attempts < AUTH_MAX_ATTEMPTS
        return self.kind == "transient" and self.attempts < MAX_ATTEMPTS


def plan_recovery(text: str, attempt: int, elapsed_s: float = 0.0) -> RecoveryPlan:
    """Decide what to do after a failed attempt (``attempt`` is 1-based)."""
    kind = classify_error(text)
    if kind == "other":
        return RecoveryPlan(kind, attempts=attempt, reason="not an auth/transient failure")
    if kind == "non_retryable":
        return RecoveryPlan(kind, attempts=attempt, reason="non-retryable client error")
    limit = AUTH_MAX_ATTEMPTS if kind == "auth" else MAX_ATTEMPTS
    if attempt >= limit:
        if kind == "auth":
            return RecoveryPlan(kind, attempts=attempt,
                                reason=f"authentication still failing after renewal "
                                       f"(limit {AUTH_MAX_ATTEMPTS} attempts)")
        return RecoveryPlan(kind, attempts=attempt, reason=f"attempt limit {limit} reached")
    if elapsed_s >= RETRY_BUDGET_S:
        return RecoveryPlan(kind, attempts=attempt, reason=f"retry budget {RETRY_BUDGET_S:.0f}s exhausted")
    delay = backoff_delay(attempt, parse_retry_after(text))
    if kind == "auth":
        return RecoveryPlan(kind, attempts=attempt, delay_s=delay, renew=True,
                            reason="access token rejected; renew via `agy models` then retry once")
    return RecoveryPlan(kind, attempts=attempt, delay_s=delay, reason="transient upstream error")


def run_with_recovery(attempt_fn: Callable[[int], tuple[int, str, str]],
                      renew_fn: Callable[[], bool] | None = None,
                      sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic,
                      log: Callable[[str], None] | None = None) -> tuple[int, str, str, int, RecoveryPlan | None]:
    """Run ``attempt_fn(i)`` with bounded retry.

    ``attempt_fn`` returns ``(rc, stdout, stderr)``. ``renew_fn`` performs the official token
    renewal and returns True when a retry is worth attempting. The final return is
    ``(rc, stdout, stderr, attempts_used, last_plan)``.
    """
    start = clock()
    attempt = 1
    renewed = False
    while True:
        rc, out, err = attempt_fn(attempt)
        text = f"{out}\n{err}"
        if rc == 0:
            return rc, out, err, attempt, None
        plan = plan_recovery(text, attempt, elapsed_s=clock() - start)
        if plan.renew and renew_fn is not None and not renewed:
            renewed = True
            ok = False
            try:
                ok = bool(renew_fn())
            except Exception as exc:  # a broken renewal must not mask the original error
                if log:
                    log(f"token renewal raised {exc.__class__.__name__}: {exc}")
            if not ok:
                plan = RecoveryPlan(plan.kind, attempts=attempt, reason="token renewal failed; stopping")
                return rc, out, err, attempt, plan
            if log:
                log(plan.reason)
            attempt += 1
            continue
        if not plan.should_retry:
            return rc, out, err, attempt, plan
        if log:
            log(f"{plan.reason}; retrying in {plan.delay_s:.1f}s (attempt {attempt + 1}/{MAX_ATTEMPTS})")
        sleep(plan.delay_s)
        attempt += 1
