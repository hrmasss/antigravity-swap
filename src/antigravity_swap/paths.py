"""Where things live: aswap's own data directory and agy's default directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"


def data_dir() -> Path:
    """aswap's data directory. ``ASWAP_HOME`` overrides the platform default."""
    override = os.environ.get("ASWAP_HOME")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "antigravity-swap"
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "antigravity-swap"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "antigravity-swap"


def gemini_dir() -> Path:
    """agy's default directory (what plain ``agy`` uses). ``ASWAP_GEMINI_DIR`` overrides it."""
    override = os.environ.get("ASWAP_GEMINI_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini"


def cli_dir(gdir: Path) -> Path:
    return gdir / "antigravity-cli"


def token_file(gdir: Path) -> Path:
    return cli_dir(gdir) / "antigravity-oauth-token"


def accounts_file() -> Path:
    return data_dir() / "accounts.json"


def credentials_dir() -> Path:
    return data_dir() / "credentials"


def usage_dir() -> Path:
    return data_dir() / "usage"


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def session_dir(slot: int) -> Path:
    return sessions_dir() / str(slot)


def settings_file() -> Path:
    return data_dir() / "settings.json"


def state_file() -> Path:
    return data_dir() / "autoswitch_state.json"


def lock_file() -> Path:
    return data_dir() / ".lock"
