"""Error recovery: 401 refresh, 403 classification, 429 cooldown, 5xx bounded retry.

Every failure mode is simulated. Nothing here touches Google, spends quota or needs a real
account: the "CLI" is a callable that returns whatever the test wants.

Policy under test
-----------------
401  -> renew the token through the official path (``agy models``), retry **once**, then stop and
        report. The account is never deleted or quarantined for a bare 401.
403  -> a generic ``PERMISSION_DENIED`` is not exhaustion and must not quarantine; only the
        explicit "Verify your account" text does, and that path stays in ``agy.classify_failure``.
429  -> honour the reset time / ``Retry-After``; no aggressive retry.
5xx  -> exponential backoff with jitter, at most ``MAX_ATTEMPTS``; non-retryable 4xx never retry.
"""

from __future__ import annotations

import pytest

from antigravity_swap import agy, errors


# --- classification ------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("401 Unauthorized: Request had invalid authentication credentials", "auth"),
    ("rpc error: code = Unauthenticated desc = access token rejected", "auth"),
    ("token has expired", "auth"),
    ("500 Internal error encountered", "transient"),
    ("502 Bad gateway", "transient"),
    ("503 The model is overloaded", "transient"),
    ("UNAVAILABLE: connection reset by peer", "transient"),
    ("400 INVALID_ARGUMENT: bad request", "non_retryable"),
    ("404 not found", "non_retryable"),
    ("PERMISSION_DENIED (code 403): permission denied", "other"),
    ("PERMISSION_DENIED (code 403): Verify your account to continue.", "other"),
    ("RESOURCE_EXHAUSTED (code 429): Individual quota reached.", "other"),
])
def test_classify_error(text, expected):
    assert errors.classify_error(text) == expected


def test_quota_text_never_marked_retryable():
    """429 is handled as a cooldown upstream — it must never enter the retry loop."""
    text = "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 4h42m2s."
    assert errors.classify_error(text) == "other"
    plan = errors.plan_recovery(text, attempt=1)
    assert plan.should_retry is False
    assert plan.renew is False


def test_403_is_not_quota_and_not_retried():
    """The exact regression the brief calls out: a quota RPC 403 must not quarantine an account."""
    text = "quota summary: failed to retrieve user quota summary: PERMISSION_DENIED (code 403)"
    assert agy.classify_failure(text) != "quota"
    plan = errors.plan_recovery(text, attempt=1)
    assert plan.should_retry is False


def test_verify_403_still_classified_by_upstream():
    assert agy.classify_failure("PERMISSION_DENIED (code 403): Verify your account to continue.") == "verify"


# --- 401 -----------------------------------------------------------------------------------


def test_401_renews_then_retries_once_and_succeeds():
    calls = []
    renewals = []

    def attempt(n):
        calls.append(n)
        if n == 1:
            return 1, "", "401 Unauthorized: invalid authentication credentials"
        return 0, '{"status":"SUCCESS"}', ""

    def renew():
        renewals.append(1)
        return True

    rc, out, err, attempts, plan = errors.run_with_recovery(attempt, renew_fn=renew,
                                                            sleep=lambda s: None)
    assert rc == 0
    assert attempts == 2, "one retry after renewal"
    assert renewals == [1]
    assert calls == [1, 2]


def test_401_stops_when_renewal_fails():
    def attempt(n):
        return 1, "", "401 Unauthorized: invalid authentication credentials"

    rc, out, err, attempts, plan = errors.run_with_recovery(attempt, renew_fn=lambda: False,
                                                            sleep=lambda s: None)
    assert rc == 1
    assert attempts == 1, "no retry when the token could not be renewed"
    assert plan.kind == "auth"
    assert "renewal failed" in plan.reason


def test_401_retries_only_once_even_if_it_keeps_failing():
    attempts_seen = []

    def attempt(n):
        attempts_seen.append(n)
        return 1, "", "401 Unauthorized"

    rc, _, _, attempts, plan = errors.run_with_recovery(attempt, renew_fn=lambda: True,
                                                        sleep=lambda s: None)
    assert attempts == 2, "renewal buys exactly one extra attempt"
    assert len(attempts_seen) == 2
    assert "renewal" in plan.reason or "authentication" in plan.reason


def test_renewal_exception_does_not_mask_the_error():
    def attempt(n):
        return 1, "", "401 Unauthorized"

    def boom():
        raise RuntimeError("keyring exploded")

    logs = []
    rc, _, err, attempts, plan = errors.run_with_recovery(
        attempt, renew_fn=boom, sleep=lambda s: None, log=logs.append)
    assert rc == 1
    assert attempts == 1
    assert any("RuntimeError" in m for m in logs)


# --- 5xx -----------------------------------------------------------------------------------


def test_5xx_retries_with_backoff_up_to_the_limit():
    delays = []
    calls = []

    def attempt(n):
        calls.append(n)
        return 1, "", "503 The model is overloaded"

    rc, _, err, attempts, plan = errors.run_with_recovery(attempt, sleep=delays.append)
    assert rc == 1
    assert len(calls) == errors.MAX_ATTEMPTS == 3
    assert len(delays) == 2, "no sleep after the final attempt"
    assert all(0 <= d <= errors.MAX_DELAY_S for d in delays)


def test_5xx_succeeds_on_second_attempt():
    def attempt(n):
        if n == 1:
            return 1, "", "502 Bad gateway"
        return 0, "ok", ""

    rc, out, _, attempts, _ = errors.run_with_recovery(attempt, sleep=lambda s: None)
    assert (rc, out, attempts) == (0, "ok", 2)


def test_non_retryable_4xx_is_not_retried():
    calls = []

    def attempt(n):
        calls.append(n)
        return 1, "", "400 INVALID_ARGUMENT: bad request"

    rc, _, _, attempts, plan = errors.run_with_recovery(attempt, sleep=lambda s: None)
    assert len(calls) == 1
    assert plan.kind == "non_retryable"


def test_retry_after_is_honoured():
    delays = []

    def attempt(n):
        return 1, "", "503 overloaded; Retry-After: 7"

    errors.run_with_recovery(attempt, sleep=delays.append)
    assert delays and abs(delays[0] - 7.0) < 0.001, delays


def test_backoff_is_bounded_and_jittered():
    d1 = errors.backoff_delay(1)
    d2 = errors.backoff_delay(2)
    assert 0 <= d1 <= errors.BASE_DELAY_S
    assert 0 <= d2 <= 2 * errors.BASE_DELAY_S
    assert errors.backoff_delay(99) <= errors.MAX_DELAY_S
    sample = {round(errors.backoff_delay(3), 6) for _ in range(20)}
    assert len(sample) > 1, "jitter must produce different delays"


def test_retry_budget_stops_an_endless_loop():
    """A clock that always reports the budget as exhausted must cap the attempts."""
    now = [0.0]

    def clock():
        now[0] += 1000.0
        return now[0]

    calls = []

    def attempt(n):
        calls.append(n)
        return 1, "", "503 overloaded"

    rc, _, _, attempts, plan = errors.run_with_recovery(attempt, sleep=lambda s: None, clock=clock)
    assert attempts <= errors.MAX_ATTEMPTS
    assert "budget" in plan.reason or "limit" in plan.reason


def test_success_first_time_never_retries():
    rc, out, err, attempts, plan = errors.run_with_recovery(lambda n: (0, "fine", ""),
                                                           sleep=lambda s: None)
    assert (rc, attempts, plan) == (0, 1, None)


# --- 429 -----------------------------------------------------------------------------------


def test_429_reset_time_is_parsed_for_the_cooldown():
    text = "RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 4h42m2s."
    secs = agy.parse_reset(text)
    assert secs == 4 * 3600 + 42 * 60 + 2, secs


def test_429_without_reset_falls_back_and_does_not_retry():
    text = "RESOURCE_EXHAUSTED (code 429): Individual quota reached."
    assert agy.parse_reset(text) is None
    plan = errors.plan_recovery(text, attempt=1)
    assert plan.should_retry is False
