from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from tekz_auto_news.bot import _publish_cycle, _safe_error_message
from tekz_auto_news.config import Settings
from tekz_auto_news.database import NewsDatabase
from tekz_auto_news.images import ResolvedImage
from tekz_auto_news.models import ArabicRewrite, NewsArticle
from tekz_auto_news.telegram_controls import TelegramUpdateListener
from tekz_auto_news.telegram_publisher import TelegramPublisher, _short_caption


def sample_article(number: int = 1) -> NewsArticle:
    return NewsArticle(
        source_name="Example Feed",
        category="AI",
        title=f"OpenAI launches a new model {number}",
        link=f"https://example.com/story/{number}",
        text="OpenAI introduced a new AI model for developers.",
        published_at=datetime.now(timezone.utc),
        image_url=None,
    )


def sample_rewrite() -> ArabicRewrite:
    return ArabicRewrite(
        title="عنوان تقني واضح",
        summary="ملخص الخبر في فقرتين قصيرتين.",
        hook="تطور جديد في الذكاء الاصطناعي",
        hashtags=("تقنية", "ذكاء_اصطناعي", "أخبار_التقنية"),
    )


class FakeTelegramResponse:
    ok = True
    status_code = 200

    def json(self) -> dict:
        return {"ok": True, "result": {"message_id": 22}}


class NewsFeatureTests(unittest.TestCase):
    def test_caption_uses_linktree_instead_of_source_attribution(self) -> None:
        article = sample_article()
        caption = _short_caption(article, sample_rewrite())

        self.assertIn("للمتابعة اضغط هنا", caption)
        self.assertIn("https://linktr.ee/loversincere29", caption)
        self.assertIn("<b>⚡", caption)
        self.assertNotIn(article.link, caption)
        self.assertNotIn("المصدر:", caption)

    def test_long_caption_with_html_characters_stays_under_telegram_limit(self) -> None:
        rewrite = ArabicRewrite(
            title="عنوان",
            summary="&<>" * 1500,
            hook="افتتاحية",
            hashtags=("تقنية", "ذكاء_اصطناعي", "أخبار_التقنية"),
        )

        caption = _short_caption(sample_article(), rewrite)

        self.assertLessEqual(len(caption), 1024)
        self.assertIn("&amp;", caption)
        self.assertNotIn("&<>", caption)

    def test_error_logs_redact_provider_and_telegram_credentials(self) -> None:
        settings = Settings(
            openrouter_api_key="openrouter-secret",
            telegram_bot_token="123456:telegram-secret",
            telegram_chat_id="@test",
            telegram_channel_url="https://t.me/channel",
            telegram_group_url="https://t.me/group",
            openrouter_model="openai/gpt-4o-mini",
            poll_interval_minutes=15,
            max_articles_per_cycle=1,
            max_article_age_hours=72,
            fetch_timeout_seconds=20,
            publish_timezone="UTC",
            publish_start_hour=0,
            publish_end_hour=23,
            database_path=Path(":memory:"),
            sources_path=Path("rss_sources.json"),
            log_level="INFO",
        )
        error = RuntimeError(
            "request https://api.telegram.org/bot123456:telegram-secret/sendPhoto "
            "failed with key openrouter-secret"
        )

        safe_message = _safe_error_message(error, settings)

        self.assertNotIn(settings.telegram_bot_token, safe_message)
        self.assertNotIn(settings.openrouter_api_key, safe_message)
        self.assertEqual(safe_message.count("[redacted]"), 2)

    def test_inline_keyboard_has_subscribe_share_and_discuss_links(self) -> None:
        class CapturingSession:
            data: dict

            def post(self, _url: str, *, data: dict, files: dict, timeout: int) -> FakeTelegramResponse:
                self.data = data
                return FakeTelegramResponse()

        article = sample_article()
        session = CapturingSession()
        publisher = TelegramPublisher(
            "test-token",
            "@test",
            "https://t.me/tekz_test",
            "https://t.me/tekz_discussion",
        )
        publisher.session = session  # type: ignore[assignment]
        image = ResolvedImage(b"image", "news.png", "image/png", "https://example.com/image.png")

        publisher.publish_photo(article, sample_rewrite(), image)

        keyboard = json.loads(session.data["reply_markup"])["inline_keyboard"]
        buttons = {button["text"]: button["url"] for row in keyboard for button in row}
        self.assertEqual(buttons["🔔 اشترك في TEKZ"], "https://t.me/tekz_test")
        self.assertEqual(buttons["💬 ناقش الخبر"], "https://t.me/tekz_discussion")
        share = parse_qs(urlparse(buttons["📤 مشاركة الخبر"]).query)
        self.assertEqual(share["url"], [article.link])
        self.assertEqual(share["text"], [sample_rewrite().title])

    def test_database_keeps_post_attempt_and_poll_results(self) -> None:
        article = sample_article()
        poll = {
            "id": "poll-123",
            "question": "هل تتوقع أن ينجح هذا المنتج؟",
            "options": [
                {"text": "نعم", "voter_count": 2},
                {"text": "لا", "voter_count": 1},
            ],
            "total_voter_count": 3,
        }
        with TemporaryDirectory() as temp_dir:
            database = NewsDatabase(Path(temp_dir) / "news.sqlite")
            self.assertTrue(database.claim(article))
            rewrite = sample_rewrite()
            database.mark_posted(
                article,
                rewrite.title,
                rewrite.summary,
                "https://example.com/image.png",
                arabic_hook=rewrite.hook,
                hashtags=rewrite.hashtags,
                message_id="22",
            )
            # A poll update can arrive before the sendPoll response is saved.
            poll["total_voter_count"] = 4
            database.update_poll(poll)
            poll["total_voter_count"] = 3
            database.record_poll(article, poll)

            stats = database.stats()
            self.assertEqual(stats["posted"], 1)
            self.assertEqual(stats["attempts"], 1)
            self.assertEqual(stats["polls"], 1)
            self.assertEqual(stats["votes"], 4)
            self.assertTrue(database.poll_created_within(24))

    def test_cycle_claims_at_most_one_article(self) -> None:
        settings = Settings(
            openrouter_api_key="test",
            telegram_bot_token="test",
            telegram_chat_id="@test",
            telegram_channel_url="https://t.me/channel",
            telegram_group_url="https://t.me/group",
            openrouter_model="openai/gpt-4o-mini",
            poll_interval_minutes=15,
            max_articles_per_cycle=1,
            max_article_age_hours=72,
            fetch_timeout_seconds=20,
            publish_timezone="UTC",
            publish_start_hour=0,
            publish_end_hour=23,
            database_path=Path(":memory:"),
            sources_path=Path("rss_sources.json"),
            log_level="INFO",
        )
        articles = [sample_article(i) for i in range(3)]
        database = Mock()
        database.is_empty.return_value = False
        database.claim.return_value = True
        database.poll_created_within.return_value = True
        database.posted_count.return_value = 1
        rewriter = Mock()
        rewriter.rewrite.return_value = sample_rewrite()
        publisher = Mock()
        publisher.publish_photo.return_value = "22"
        image_resolver = Mock()
        image_resolver.resolve.return_value = SimpleNamespace(url="https://example.com/image.png")

        with patch("tekz_auto_news.bot.load_sources", return_value=[{"name": "feed"}]), patch(
            "tekz_auto_news.bot.fetch_latest", return_value=articles
        ):
            posted = _publish_cycle(
                settings,
                database,
                rewriter,
                publisher,
                image_resolver,
                force=True,
            )

        self.assertEqual(posted, 1)
        self.assertEqual(database.claim.call_count, 1)
        self.assertEqual(publisher.publish_photo.call_count, 1)

    def test_admin_commands_reply_and_request_a_single_scan(self) -> None:
        publisher = Mock()
        publisher.is_chat_admin.return_value = True
        database = Mock()
        database.stats.return_value = {
            "posted": 1,
            "attempts": 1,
            "last_posted_at": "2026-10-07T08:00:00+00:00",
            "polls": 0,
            "votes": 0,
        }
        scan_event = Mock()
        listener = TelegramUpdateListener(
            publisher,
            database,
            -100123,
            scan_event,
            Mock(),
            lambda: "يعمل",
        )
        message = {
            "chat": {"id": -100123},
            "from": {"id": 55, "is_bot": False},
            "date": int(datetime.now(timezone.utc).timestamp()),
            "message_id": 4,
            "text": "/now",
        }

        listener._handle_message(message)

        scan_event.set.assert_called_once_with()
        publisher.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
