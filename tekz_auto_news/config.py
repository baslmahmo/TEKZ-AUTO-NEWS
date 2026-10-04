from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    openai_model: str
    poll_interval_minutes: int
    max_articles_per_cycle: int
    max_article_age_hours: int
    fetch_timeout_seconds: int
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


def load_settings() -> Settings:
    """Load settings from Replit environment variables or a local .env file."""
    load_dotenv()

    required = ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        names = ", ".join(missing)
        raise ValueError(
            f"Missing required environment variables: {names}. "
            "Set API keys in Replit Secrets and TELEGRAM_CHAT_ID in environment variables."
        )

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")

    return Settings(
        openai_api_key=os.environ["OPENAI_API_KEY"].strip(),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=_normalize_telegram_chat_id(os.environ["TELEGRAM_CHAT_ID"]),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        poll_interval_minutes=_positive_int("POLL_INTERVAL_MINUTES", 15),
        max_articles_per_cycle=_positive_int("MAX_ARTICLES_PER_CYCLE", 3),
        max_article_age_hours=_positive_int("MAX_ARTICLE_AGE_HOURS", 72),
        fetch_timeout_seconds=_positive_int("FETCH_TIMEOUT_SECONDS", 20),
        database_path=Path(os.getenv("DATABASE_PATH", "data/tekz_auto_news.db")),
        sources_path=Path(os.getenv("RSS_SOURCES_PATH", "rss_sources.json")),
        log_level=log_level,
    )