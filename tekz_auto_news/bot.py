from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tekz_auto_news.config import Settings, load_settings
from tekz_auto_news.database import NewsDatabase
from tekz_auto_news.images import ImageResolver
from tekz_auto_news.models import NewsArticle
from tekz_auto_news.openai_rewriter import ArabicNewsRewriter, ProviderCreditsUnavailable
from tekz_auto_news.ranking import is_poll_worthy, rank_articles
from tekz_auto_news.rss_reader import fetch_latest, load_sources
from tekz_auto_news.telegram_publisher import TelegramPermissionError, TelegramPublisher
from tekz_auto_news.telegram_controls import TelegramUpdateListener
from tekz_auto_news.writing_styles import choose_post_style

logger = logging.getLogger("tekz_auto_news")
_running = True
_wake_event = threading.Event()
_openrouter_retry_after: datetime | None = None
_telegram_paused = False


def _handle_shutdown(signum: int, _frame: object) -> None:
    global _running
    logger.info("Received shutdown signal %s; finishing the current operation.", signum)
    _running = False
    _wake_event.set()


def _inside_publish_window(settings: Settings) -> bool:
    local_now = datetime.now(ZoneInfo(settings.publish_timezone))
    start = settings.publish_start_hour
    end = settings.publish_end_hour
    if start == end:
        return True
    if start < end:
        return start <= local_now.hour < end
    return local_now.hour >= start or local_now.hour < end


def _safe_error_message(exc: Exception, settings: Settings) -> str:
    message = str(exc)
    for secret in (settings.telegram_bot_token, settings.openrouter_api_key):
        if secret:
            message = message.replace(secret, "[redacted]")
    return f"{type(exc).__name__}: {message}"[:800]


def _publish_cycle(
    settings: Settings,
    database: NewsDatabase,
    rewriter: ArabicNewsRewriter,
    publisher: TelegramPublisher,
    image_resolver: ImageResolver,
    *,
    max_articles_per_cycle: int | None = None,
    force: bool = False,
) -> int:
    global _openrouter_retry_after, _telegram_paused
    if _telegram_paused:
        logger.warning(
            "Telegram publishing is paused after a permission error; grant channel posting rights and restart the workflow."
        )
        return 0

    if not force and not _inside_publish_window(settings):
        logger.info(
            "Outside the configured publishing window (%02d:00-%02d:00 %s); skipping this cycle.",
            settings.publish_start_hour,
            settings.publish_end_hour,
            settings.publish_timezone,
        )
        return 0

    now = datetime.now(timezone.utc)
    if _openrouter_retry_after and now < _openrouter_retry_after:
        logger.warning(
            "OpenRouter requests are paused after an exhausted credit balance; retry after %s.",
            _openrouter_retry_after.isoformat(timespec="minutes"),
        )
        return 0
    _openrouter_retry_after = None

    sources = load_sources(settings.sources_path)
    cycle_limit = max_articles_per_cycle or settings.max_articles_per_cycle
    articles = rank_articles(
        fetch_latest(
            sources,
            timeout_seconds=settings.fetch_timeout_seconds,
            max_age_hours=settings.max_article_age_hours,
        )
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
            post_style = choose_post_style(article, database.recent_post_styles(limit=2))
            rewrite = rewriter.rewrite(article, preferred_style=post_style)
            image = image_resolver.resolve(article)
            message_id = publisher.publish_photo(article, rewrite, image)
            database.mark_posted(
                article,
                rewrite.title,
                rewrite.summary,
                image.url,
                arabic_hook=rewrite.hook,
                hashtags=rewrite.hashtags,
                message_id=message_id,
                post_style=rewrite.style,
            )
            published += 1
            logger.info(
                "Published article from %s to Telegram (message id %s).",
                article.source_name,
                message_id,
            )
            if is_poll_worthy(article) and not database.poll_created_within(24):
                try:
                    poll = publisher.send_poll(
                        "هل تتوقع أن ينجح هذا المنتج؟",
                        ("نعم، على الأرجح", "لا، يحتاج إلى تطوير", "من المبكر الحكم"),
                    )
                    database.record_poll(article, poll)
                    logger.info("Created a Telegram poll for a product announcement.")
                except Exception as exc:
                    logger.warning("Could not create an optional Telegram poll (%s).", type(exc).__name__)
        except ProviderCreditsUnavailable as exc:
            safe_error = _safe_error_message(exc, settings)
            try:
                database.mark_failed(article, safe_error)
            except Exception:
                logger.exception("Could not save the failed status for an article.")
            _openrouter_retry_after = datetime.now(timezone.utc) + timedelta(hours=6)
            logger.error(
                "%s OpenRouter requests will pause for six hours; add credits and restart the workflow to resume sooner.",
                safe_error,
            )
            break
        except TelegramPermissionError as exc:
            safe_error = _safe_error_message(exc, settings)
            try:
                database.mark_failed(article, safe_error)
            except Exception:
                logger.exception("Could not save the failed status for an article.")
            _telegram_paused = True
            logger.error(
                "%s Grant the bot permission to post in the channel, then restart the workflow.",
                safe_error,
            )
            break
        except Exception as exc:
            safe_error = _safe_error_message(exc, settings)
            try:
                database.mark_failed(article, safe_error)
            except Exception:
                logger.exception("Could not save the failed status for an article.")
            logger.error("Could not publish article from %s: %s", article.source_name, safe_error)

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
    global _running, _telegram_paused, _openrouter_retry_after
    parser = argparse.ArgumentParser(description="Run the TEKZ-AUTO-NEWS Telegram bot.")
    parser.add_argument(
        "--test-one-post",
        action="store_true",
        help="Limit the first publishing cycle to one article, then resume normal limits.",
    )
    parser.add_argument(
        "--defer-first-cycle",
        action="store_true",
        help="Wait for the normal interval before the first news scan.",
    )
    args = parser.parse_args()
    _running = True
    _telegram_paused = False
    _openrouter_retry_after = None

    try:
        settings = load_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

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
    publisher = TelegramPublisher(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        settings.telegram_channel_url,
        settings.telegram_group_url,
        settings.fetch_timeout_seconds,
    )
    image_resolver = ImageResolver(settings.fetch_timeout_seconds)

    try:
        bot_username = publisher.verify_connection()
        destination_chat_id = publisher.verify_destination()
        logger.info(
            "TEKZ-AUTO-NEWS is running as @%s; Telegram destination verified. Checking feeds every %s minutes; maximum one story per cycle.",
            bot_username,
            settings.poll_interval_minutes,
        )
        if args.test_one_post:
            logger.info("One-post test enabled: the first cycle is limited to one article.")
    except Exception as exc:
        logger.error("Could not verify Telegram bot access: %s", _safe_error_message(exc, settings))
        raise SystemExit(1) from None

    listener_stop = threading.Event()
    listener = TelegramUpdateListener(
        publisher,
        database,
        destination_chat_id,
        _wake_event,
        listener_stop,
        lambda: (
            "متوقف مؤقتًا بسبب صلاحيات Telegram"
            if _telegram_paused
            else "متوقف مؤقتًا بسبب رصيد OpenRouter"
            if _openrouter_retry_after and datetime.now(timezone.utc) < _openrouter_retry_after
            else "يعمل"
        ),
    )
    listener.start()

    interval_seconds = settings.poll_interval_minutes * 60
    next_run_at = time.monotonic() + (interval_seconds if args.defer_first_cycle else 0)
    if args.defer_first_cycle:
        logger.info(
            "Initial news scan deferred for %s minutes; scheduled cycles remain limited to one story.",
            settings.poll_interval_minutes,
        )
    first_cycle = True
    try:
        while _running:
            delay = max(0.0, next_run_at - time.monotonic())
            forced = _wake_event.wait(timeout=delay)
            _wake_event.clear()
            if not _running:
                break
            try:
                _publish_cycle(
                    settings,
                    database,
                    rewriter,
                    publisher,
                    image_resolver,
                    max_articles_per_cycle=1 if args.test_one_post and first_cycle else None,
                    force=forced,
                )
            except Exception:
                logger.exception("The news scan failed; the bot will retry at the next interval.")
            first_cycle = False
            next_run_at = time.monotonic() + interval_seconds
    finally:
        listener_stop.set()
        _wake_event.set()
        listener.join(timeout=2)

    logger.info("TEKZ-AUTO-NEWS stopped cleanly.")


if __name__ == "__main__":
    main()