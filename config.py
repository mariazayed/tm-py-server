"""
Loads and validates all configuration for the importer from environment
variables (via a .env file next to this script). Fails fast and loudly if
anything required is missing, rather than letting a half-configured run
silently import garbage.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is not None:
        value = value.strip()
    return value or None


def _get_int(name: str) -> int | None:
    raw = _get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"ERROR: {name} must be an integer, got: {raw!r}", file=sys.stderr)
        sys.exit(1)


def _get_date(name: str, default_iso: str) -> date:
    raw = _get(name) or default_iso
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        print(f"ERROR: {name} must be YYYY-MM-DD, got: {raw!r}", file=sys.stderr)
        sys.exit(1)


@dataclass(frozen=True)
class Settings:
    mt5_login: int | None
    mt5_password: str | None
    mt5_server: str | None
    mt5_terminal_path: str | None

    tm_api_base_url: str
    tm_api_token: str | None

    default_strategy_id: int

    account_history_start: date


def load_settings() -> Settings:
    base_url = _get("TM_API_BASE_URL", "http://localhost:8080")
    strategy_id = _get_int("TM_DEFAULT_STRATEGY_ID")

    if strategy_id is None:
        print(
            "ERROR: TM_DEFAULT_STRATEGY_ID is not set in .env. "
            "Every imported trade needs a strategyId -- set this to the id "
            "of the strategy you want auto-imported trades filed under.",
            file=sys.stderr,
        )
        sys.exit(1)

    return Settings(
        mt5_login=_get_int("MT5_LOGIN"),
        mt5_password=_get("MT5_PASSWORD"),
        mt5_server=_get("MT5_SERVER"),
        mt5_terminal_path=_get("MT5_TERMINAL_PATH"),
        tm_api_base_url=base_url.rstrip("/"),
        tm_api_token=_get("TM_API_TOKEN"),
        default_strategy_id=strategy_id,
        account_history_start=_get_date("ACCOUNT_HISTORY_START", "2020-01-01"),
    )
