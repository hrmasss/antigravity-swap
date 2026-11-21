"""Quota readings, from the same two RPCs the Antigravity CLI uses.

    POST /v1internal:loadCodeAssist            -> {cloudaicompanionProject}
    POST /v1internal:retrieveUserQuotaSummary  -> {groups: [{displayName, buckets: [
           {bucketId, window, remainingFraction, resetTime}]}]}

Buckets seen so far: ``gemini-5h`` and ``gemini-weekly`` (Gemini models), ``3p-5h`` and
``3p-weekly`` (Claude and GPT models through agy). agy reports what is *left*; aswap shows
what is *used*, so the numbers read like claude-swap's.

The two groups are independent: an account can be out of Claude/GPT quota for four days while
its Gemini quota is untouched. Every hold in aswap is therefore per *pool* (``gemini`` /
``3p``), not per account — see ``store.Account.pool_limited``.

Not every limit is visible here. Accounts also hit an "Individual quota reached" ceiling
that this endpoint does not report; ``aswap exec`` catches that one when it happens.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from antigravity_swap import __version__
from antigravity_swap.credentials import parse_rfc3339

HOSTS = ("daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com")
TIMEOUT = 15
WINDOW_ORDER = ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly")
POOLS = ("gemini", "3p", "all")
POOL_LABELS = {"gemini": "Gemini", "3p": "Claude/GPT", "all": "all-model"}

# A model id (``agy models``) or a display name (settings.json's ``model``) -> quota pool.
MODEL_3P_RE = re.compile(r"claude|gpt|o1|o3|o4|sonnet|opus", re.I)
MODEL_GEMINI_RE = re.compile(r"gemini|imagen|veo|nano[- ]?banana", re.I)


class UsageError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # "auth", "http-<code>", "network", "empty"


def pool_of_model(model: str | None) -> str | None:
    """The quota pool a model draws from, or None when the model cannot be identified.

    None is a real answer, never a guess: a hold placed on the wrong pool either blocks a
    request that would have worked or lets through one that cannot succeed.
    """
    m = str(model or "").strip()
    if not m:
        return None
    if MODEL_3P_RE.search(m):
        return "3p"
    if MODEL_GEMINI_RE.search(m):
        return "gemini"
    return None


def pool_label(pool: str | None) -> str:
    return POOL_LABELS.get(pool or "", "model")


def _call(host: str, path: str, body: dict, token: str) -> dict:
    req = urllib.request.Request(
        f"https://{host}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"antigravity-swap/{__version__}",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch(token: str, project: str | None = None) -> dict:
    """Fetch and normalize one account's quota. Raises UsageError."""
    last: UsageError | None = None
    for host in HOSTS:
        proj = project
        for attempt in range(2):
            try:
                if not proj:
                    proj = _call(host, "/v1internal:loadCodeAssist", {}, token).get("cloudaicompanionProject")
                if not proj:
                    last = UsageError("empty", f"{host}: no project in loadCodeAssist")
                    break
                resp = _call(host, "/v1internal:retrieveUserQuotaSummary", {"project": proj}, token)
                return {"ts": time.time(), "project": proj, "host": host, "groups": normalize(resp)}
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise UsageError("auth", "access token rejected (401)") from None
                if proj and attempt == 0 and e.code in (403, 404):
                    proj = None  # a stale cached project; ask for it again
                    continue
                last = UsageError(f"http-{e.code}", f"HTTP {e.code} from {host}")
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = UsageError("network", f"{host}: {e}")
                break
    raise last or UsageError("network", "no host answered")


def normalize(resp: dict) -> list[dict]:
    groups = []
    for g in resp.get("groups") or []:
        buckets = []
        for b in g.get("buckets") or []:
            if b.get("disabled") or b.get("remainingFraction") is None:
                continue
            buckets.append({
                "bucket_id": b.get("bucketId") or "",
                "window": b.get("window") or "",
                "remaining_fraction": float(b["remainingFraction"]),
                "reset_time": b.get("resetTime") or "",
            })
        if buckets:
            groups.append({"display_name": g.get("displayName") or "", "buckets": buckets})
    return groups


def windows(reading: dict | None) -> dict[str, dict]:
    """{bucket_id: {"used": pct, "resets_at": epoch|None}} for one reading."""
    out: dict[str, dict] = {}
    for g in (reading or {}).get("groups") or []:
        for b in g.get("buckets") or []:
            used = round((1.0 - float(b.get("remaining_fraction", 1.0))) * 100.0, 1)
            out[b.get("bucket_id", "")] = {"used": max(0.0, min(100.0, used)),
                                           "resets_at": parse_rfc3339(b.get("reset_time"))}
    return out


def in_pool(bucket_id: str, pool: str) -> bool:
    if pool == "all":
        return True
    if pool == "3p":
        return bucket_id.startswith("3p-")
    return bucket_id.startswith("gemini-")


def binding_used(reading: dict | None, pool: str) -> float | None:
    """The most-used window in the pool: the one that will stop you first."""
    vals = [w["used"] for b, w in windows(reading).items() if in_pool(b, pool)]
    return max(vals) if vals else None


def weekly_reset(reading: dict | None, pool: str) -> float | None:
    ts = [w["resets_at"] for b, w in windows(reading).items()
          if in_pool(b, pool) and b.endswith("weekly") and w["resets_at"]]
    return min(ts) if ts else None


def earliest_recovery(reading: dict | None, pool: str, threshold: float) -> float | None:
    """When the binding window in the pool drops back under ``threshold``: its reset."""
    ts = [w["resets_at"] for b, w in windows(reading).items()
          if in_pool(b, pool) and w["used"] >= threshold and w["resets_at"]]
    return max(ts) if ts else None
