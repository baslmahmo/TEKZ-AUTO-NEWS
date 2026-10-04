from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from tekz_auto_news.config import Settings, load_settings
from tekz_auto_news.database import NewsDatabase
from tekz_auto_news.images import ImageResolver
from tekz_auto_news.models import NewsArticle
from tekz_auto_news.openai_rewriter import ArabicNewsRewriter, ProviderCreditsUnavailable
from tekz_auto_news.rss_reader import fetch_latest, load_sources
from tekz_auto_news.telegram_publisher import TelegramPermissionError, TelegramPublisher

logger = logging.getLogger("tekz_auto_news")
_running = True
_openai_retry_after: datetime | None = None
_telegram_paused = False


def _handle_shutdown(signum: int, _frame: object) -> None:
    global _running
    logger.info("Received shutdown signal %s; finishing the current operation.", signum)
    _running = False


def _publish_cycle(
    settings: Settings,
    database: NewsDatabase,
    rewriter: ArabicNewsRewriter,
    publisher: TelegramPublisher,
    image_resolver: ImageResolver,
    *,
    max_articles_per_cycle: int | None = None,
) -> int:
    global _openai_retry_after, _telegram_paused
    if _telegram_paused:
        logger.warning(
            "Telegram publishing is paused after a permission error; grant channel posting rights and restart the workflow."
        )
        return 0

    now = datetime.now(timezone.utc)
    if _openai_retry_after and now < _openai_retry_after:
        logger.warning(
            "OpenRouter requests are paused after an exhausted credit balance; retry after %s.",
            _openai_retry_after.isoformat(timespec="minutes"),
        )
        return 0
    _openai_retry_after = None

    sources = load_sources(settings.sources_path)
    cycle_limit = max_articles_per_cycle or settings.max_articles_per_cycle
    articles = fetch_latest(
        sources,
        timeout_seconds=settings.fetch_timeout_seconds,
        max_age_hours=settings.max_article_age_hours,
    )
    logger.info("Collected %s recent stories from %s RSS feeds.", len(articles), len(sources))

    first_scan = database.is_empty()
    first_scan_backlog: list[NewsArticle] = []
    if first_scan and len(articles) > cycle_limit:
        first_scan_backlog = articles[cycle_limit:]
        articles = articles[:cycle_limit]
        logger.info(
            "First run: limiting initial publishing to %s recent stories; the remaining %s items will be skipped.",
            len(articles),
            len(first_scan_backlog),
        )

    published = 0
    attempted = 0
    for article in articles:
        if not _running or attempted >= cycle_limit:
            break
        if not database.claim(article):
            continue

        attempted += 1
        try:
            rewrite = rewriter.rewrite(article)
            image = image_resolver.resolve(article)
            message_id = publisher.publish_photo(article, rewrite, image)
            database.mark_posted(article, rewrite.title, rewrite.summary, image.url)
            published += 1
            logger.info(
                "Published article from %s to Telegram (message id %s).",
                article.source_name,
                message_id,
            )
        except ProviderCreditsUnavailable as exc:
            try:
                database.mark_failed(article, str(exc))
            except Exception:
                logger.exception("Could not save the failed status for an article.")
            _openai_retry_after = datetime.now(timezone.utc) + timedelta(hours=6)
            logger.error(
                "%s OpenRouter requests will pause for six hours; add credits and restart the workflow to resume sooner.",
                exc,
            )
            break
        except TelegramPermissionError as exc:
            try:
                database.mark_failed(article, str(exc))
            except Exception:
                logger.exception("Could not save the failed status for an article.")
            _telegram_paused = True
            logger.error(
                "%s Grant the bot permission to post in the channel, then restart the workflow.",
                exc,
            )
            break
        except Exception as exc:
            try:
                database.mark_failed(article, f"{type(exc).__name__}: {exc}")
            except Exception:
                logger.exception("Could not save the failed status for an article.")
            logger.exception("Could not publish article from %s: %s", article.source_name, exc)

    if first_scan_backlog:
        database.mark_initial_backlog_skipped(first_scan_backlog)

    logger.info(
        "Cycle finished at %s. New posts: %s; posted stories in SQLite: %s.",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        published,
        database.posted_count(),
    )
    return published


def main() -> None:
    global _running
    parser = argparse.ArgumentParser(description="Run the TEKZ-AUTO-NEWS Telegram bot.")
    parser.add_argument(
        "--test-one-post",
        action="store_true",
        help="Limit the first publishing cycle to one article, then resume normal limits.",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    database = NewsDatabase(settings.database_path)
    rewriter = ArabicNewsRewriter(settings.openrouter_api_key, settings.openrouter_model)
    publisher = TelegramPublisher(settings.telegram_bot_token, settings.telegram_chat_id)
    image_resolver = ImageResolver(settings.fetch_timeout_seconds)

    try:
        bot_username = publisher.verify_connection()
        logger.info(
            "TEKZ-AUTO-NEWS is running as @%s; checking feeds every %s minutes.",
            bot_username,
            settings.poll_interval_minutes,
        )
        if args.test_one_post:
            logger.info("One-post test enabled: the first cycle is limited to one article.")
    except Exception as exc:
        logger.error("Could not verify Telegram bot access: %s", exc)
        raise SystemExit(1) from exc

    first_cycle = True
    while _running:
        cycle_started = time.monotonic()
        try:
            _publish_cycle(
                settings,
                database,
                rewriter,
                publisher,
                image_resolver,
                max_articles_per_cycle=1 if args.test_one_post and first_cycle else None,
            )
        except Exception:
            logger.exception("The news scan failed; the bot will retry at the next interval.")
        first_cycle = False

        remaining = max(
            0,
            settings.poll_interval_minutes * 60 - (time.monotonic() - cycle_started),
        )
        while _running and remaining > 0:
            sleep_for = min(1.0, remaining)
            time.sleep(sleep_for)
            remaining -= sleep_for

    logger.info("TEKZ-AUTO-NEWS stopped cleanly.")


if __name__ == "__main__":
    main()