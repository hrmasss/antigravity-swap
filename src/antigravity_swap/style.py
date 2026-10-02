"""Terminal styling: colors when the output is a color terminal, plain text otherwise."""

from __future__ import annotations

import os
import sys

_enabled: bool | None = None


def _vt_on_windows() -> bool:
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
            _enabled = False
        elif os.environ.get("FORCE_COLOR"):
            _enabled = True
        elif not sys.stdout.isatty():
            _enabled = False
        else:
            _enabled = _vt_on_windows() if os.name == "nt" else True
    return _enabled


def _wrap(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if enabled() else s


def bold(s: str) -> str:
    return _wrap("1", s)


def dim(s: str) -> str:
    return _wrap("2", s)


def active(s: str) -> str:
    return _wrap("1;38;5;209", s)


def warn(s: str) -> str:
    return _wrap("33", s)


def bad(s: str) -> str:
    return _wrap("31", s)


def good(s: str) -> str:
    return _wrap("32", s)


def pct(used: float) -> str:
    text = f"{used:>3.0f}%"
    if used >= 90:
        return bad(text)
    if used >= 70:
        return warn(text)
    return text


def _can_encode(s: str) -> bool:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        s.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def glyphs() -> dict[str, str]:
    if _can_encode("├└●"):
        return {"mid": "├", "end": "└", "dot": "●"}
    return {"mid": "|-", "end": "`-", "dot": "*"}
