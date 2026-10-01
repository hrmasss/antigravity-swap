"""Tool preferences, validated. ``aswap config`` is the front door."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from antigravity_swap import paths
from antigravity_swap.fsutil import read_json, write_json

STRATEGIES = ("best", "consume-first")
PICK_STRATEGIES = ("rotate", "best", "next-available", "consume-first")
POOLS = ("gemini", "3p", "all")


@dataclass(frozen=True)
class Key:
    name: str
    default: Any
    kind: type
    help: str
    check: Callable[[Any], bool] | None = None
    choices: tuple | None = None


KEYS: dict[str, Key] = {k.name: k for k in [
    Key("autoswitch.threshold", 90.0, float,
        "Used-% of the binding window at which auto looks for a better account (1-100).",
        lambda v: 1 <= v <= 100),
    Key("autoswitch.interval_seconds", 60.0, float, "Seconds between auto checks (>= 15).", lambda v: v >= 15),
    Key("autoswitch.cooldown_seconds", 300.0, float, "Minimum seconds between two proactive switches.",
        lambda v: v >= 0),
    Key("autoswitch.hysteresis_pct", 10.0, float,
        "A proactive switch must land on an account at least this many points less used (0-50).",
        lambda v: 0 <= v <= 50),
    Key("autoswitch.strategy", "best", str,
        "best: most headroom. consume-first: weekly window that resets soonest.", choices=STRATEGIES),
    Key("autoswitch.pool", "gemini", str,
        "Which quota pool drives decisions: gemini, 3p (Claude/GPT through agy) or all.", choices=POOLS),
    Key("switch.verify", True, bool,
        "After a switch, run `agy models` to prove the login works (no quota is spent)."),
    Key("exec.strategy", "best", str, "How `aswap exec` picks the next account.", choices=PICK_STRATEGIES),
    Key("exec.max_hops", 0, int, "Most account changes in one exec run; 0 means every account once.",
        lambda v: v >= 0),
    Key("exec.resume_prompt",
        "You were interrupted by a usage limit and are now running on a different account. "
        "Continue the task exactly where you stopped. Do not restart finished steps.",
        str, "Prompt sent when a conversation continues on the next account."),
    Key("exec.limit_fallback_hours", 5.0, float,
        "How long to hold an account out when a quota error carries no reset time.", lambda v: v > 0),
    Key("usage.max_age_seconds", 300.0, float,
        "Quota readings older than this are re-fetched by list/auto.", lambda v: v >= 0),
    Key("usage.renew", True, bool,
        "Renew expired access tokens with `agy models` before reading quota (no quota is spent)."),
    Key("agy.path", "", str, "Path to the agy binary. Empty: find it on PATH."),
]}


def _coerce(key: Key, value: Any) -> Any:
    if key.kind is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key.name} takes true or false")
    try:
        v = key.kind(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key.name} takes a {key.kind.__name__}") from None
    if key.choices and v not in key.choices:
        raise ValueError(f"{key.name} must be one of: {', '.join(key.choices)}")
    if key.check and not key.check(v):
        raise ValueError(f"{key.name}: {v!r} is out of range. {key.help}")
    return v


class Settings:
    def __init__(self, values: dict | None = None):
        self.values = values or {}

    @classmethod
    def load(cls) -> "Settings":
        return cls(read_json(paths.settings_file(), default={}) or {})

    def save(self) -> None:
        write_json(paths.settings_file(), self.values)

    def get(self, name: str) -> Any:
        key = KEYS[name]
        if name in self.values:
            try:
                return _coerce(key, self.values[name])
            except ValueError:
                return key.default
        return key.default

    def is_default(self, name: str) -> bool:
        return name not in self.values

    def set(self, name: str, value: Any) -> Any:
        if name not in KEYS:
            raise KeyError(name)
        v = _coerce(KEYS[name], value)
        self.values[name] = v
        return v

    def unset(self, name: str) -> None:
        if name not in KEYS:
            raise KeyError(name)
        self.values.pop(name, None)
