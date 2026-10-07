from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_channel_url: str
    telegram_group_url: str
    openrouter_model: str
    poll_interval_minutes: int
    max_articles_per_cycle: int
    max_article_age_hours: int
    fetch_timeout_seconds: int
    publish_timezone: str
    publish_start_hour: int
    publish_end_hour: int
    database_path: Path
    sources_path: Path
    log_level: str


def _positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def _normalize_telegram_chat_id(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
        if parsed.hostname.lower() in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            username = parsed.path.strip("/")
            if not username or "/" in username or username.startswith("+"):
                raise ValueError(
                    "A private Telegram channel invite link is not a chat ID; use its numeric -100... ID."
                )
            return f"@{username}"
    return value


def _public_url(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value.startswith("@"):
        value = f"https://t.me/{value[1:]}"
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be a valid public http(s) URL.")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain embedded credentials.")
    return value.rstrip("/")


def _hour(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer from 0 to 23.") from exc
    if not 0 <= value <= 23:
        raise ValueError(f"{name} must be an integer from 0 to 23.")
    return value


def load_settings() -> Settings:
    """Load settings from Replit environment variables or a local .env file."""
    load_dotenv()

    required = (
        "OPENROUTER_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_CHANNEL_URL",
        "TELEGRAM_GROUP_URL",
    )
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        names = ", ".join(missing)
        raise ValueError(
            f"Missing required environment variables: {names}. "
            "Set API keys and Telegram button URLs in Replit Secrets, and TELEGRAM_CHAT_ID in environment variables."
        )

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")

    publish_timezone = os.getenv("PUBLISH_TIMEZONE", "Asia/Jerusalem").strip()
    try:
        ZoneInfo(publish_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("PUBLISH_TIMEZONE must be a valid IANA timezone name.") from exc

    return Settings(
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"].strip(),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=_normalize_telegram_chat_id(os.environ["TELEGRAM_CHAT_ID"]),
        telegram_channel_url=_public_url("TELEGRAM_CHANNEL_URL"),
        telegram_group_url=_public_url("TELEGRAM_GROUP_URL"),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
        poll_interval_minutes=_positive_int("POLL_INTERVAL_MINUTES", 15),
        max_articles_per_cycle=1,
        max_article_age_hours=_positive_int("MAX_ARTICLE_AGE_HOURS", 72),
        fetch_timeout_seconds=_positive_int("FETCH_TIMEOUT_SECONDS", 20),
        publish_timezone=publish_timezone,
        publish_start_hour=_hour("PUBLISH_START_HOUR", 8),
        publish_end_hour=_hour("PUBLISH_END_HOUR", 22),
        database_path=Path(os.getenv("DATABASE_PATH", "data/tekz_auto_news.db")),
        sources_path=Path(os.getenv("RSS_SOURCES_PATH", "rss_sources.json")),
        log_level=log_level,
    )