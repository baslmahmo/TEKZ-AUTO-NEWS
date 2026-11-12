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
from tekz_auto_news.config import Settings, _normalize_telegram_chat_id
from tekz_auto_news.database import NewsDatabase
from tekz_auto_news.images import FALLBACK_IMAGES, ImageResolver, ResolvedImage
from tekz_auto_news.models import ArabicRewrite, NewsArticle
from tekz_auto_news.openai_rewriter import ArabicNewsRewriter
from tekz_auto_news.telegram_controls import TelegramUpdateListener
from tekz_auto_news.telegram_publisher import (
    TelegramPermissionError,
    TelegramPublisher,
    _short_caption,
)
from tekz_auto_news.writing_styles import (
    APP_EXPLAINER,
    BLOG_STORY,
    SOFTWARE_SPOTLIGHT,
    choose_post_style,
)


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
        self.assertTrue(caption.endswith("https://linktr.ee/loversincere29</a>"))

    def test_relevant_post_styles_rotate_without_forcing_an_unrelated_format(self) -> None:
        app_article = NewsArticle(
            source_name="Example Feed",
            category="Apps",
            title="A new app adds a useful feature",
            link="https://example.com/app",
            text="The mobile app is available for Android.",
            published_at=datetime.now(timezone.utc),
            image_url=None,
        )
        unrelated_article = NewsArticle(
            source_name="Example Feed",
            category="AI",
            title="Researchers publish a new safety study",
            link="https://example.com/research",
            text="The study discusses model evaluation.",
            published_at=datetime.now(timezone.utc),
            image_url=None,
        )

        self.assertEqual(choose_post_style(app_article, []), APP_EXPLAINER)
        self.assertEqual(choose_post_style(app_article, [APP_EXPLAINER]), BLOG_STORY)
        self.assertEqual(
            choose_post_style(app_article, [BLOG_STORY, APP_EXPLAINER]),
            SOFTWARE_SPOTLIGHT,
        )
        self.assertEqual(choose_post_style(unrelated_article, [BLOG_STORY]), BLOG_STORY)

    def test_rewriter_uses_requested_editorial_style_and_natural_voice(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "title": "عنوان",
                                "hook": "افتتاحية",
                                "summary": "ملخص موجز.",
                                "hashtags": ["تقنية", "تطبيقات", "ذكاء_اصطناعي"],
                            },
                            ensure_ascii=False,
                        )
                    )
                )
            ]
        )
        rewriter = ArabicNewsRewriter("not-a-real-key", "test-model")
        rewriter._create_completion = Mock(return_value=response)  # type: ignore[method-assign]

        result = rewriter.rewrite(sample_article(), preferred_style=APP_EXPLAINER)

        self.assertEqual(result.style, APP_EXPLAINER)
        prompt = rewriter._create_completion.call_args.kwargs["messages"][0]["content"]
        self.assertIn("شرح عملي لتطبيق", prompt)
        self.assertIn("تجنب العبارات الآلية", prompt)

    def test_image_resolver_can_select_the_standard_category_fallback(self) -> None:
        resolver = ImageResolver(timeout_seconds=5)
        fallback = ResolvedImage(
            b"fallback",
            "fallback.jpg",
            "image/jpeg",
            FALLBACK_IMAGES["ai"],
        )
        resolver._download = Mock(return_value=fallback)  # type: ignore[method-assign]

        result = resolver.resolve_fallback(sample_article())

        self.assertEqual(result, fallback)
        resolver._download.assert_called_once_with(FALLBACK_IMAGES["ai"])

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

    def test_public_group_link_normalizes_and_post_is_sent_to_both_destinations(self) -> None:
        class CapturingSession:
            destinations: list[str | int] = []

            def post(self, _url: str, *, data: dict, files: dict, timeout: int) -> FakeTelegramResponse:
                self.destinations.append(data["chat_id"])
                return FakeTelegramResponse()

        self.assertEqual(_normalize_telegram_chat_id("https://t.me/baslmahmod"), "@baslmahmod")
        session = CapturingSession()
        publisher = TelegramPublisher(
            "test-token",
            "@tekz",
            "https://t.me/tekz",
            "https://t.me/discussion",
            additional_chat_id="@baslmahmod",
        )
        publisher.session = session  # type: ignore[assignment]
        publisher.additional_destination_chat_id = -10012345
        image = ResolvedImage(b"image", "news.png", "image/png", "https://example.com/image.png")

        message_id = publisher.publish_photo(sample_article(), sample_rewrite(), image)

        self.assertEqual(message_id, "22")
        self.assertEqual(session.destinations, ["@tekz", -10012345])

    def test_additional_group_or_channel_checks_posting_permissions(self) -> None:
        class FakeGetResponse:
            ok = True
            status_code = 200

            def __init__(self, result: dict) -> None:
                self.result = result

            def json(self) -> dict:
                return {"ok": True, "result": self.result}

        class MembershipSession:
            def __init__(self, status: dict, chat_type: str = "supergroup") -> None:
                self.status = status
                self.chat_type = chat_type

            def get(self, url: str, *, params: dict, timeout: int) -> FakeGetResponse:
                if url.endswith("/getChat"):
                    return FakeGetResponse(
                        {
                            "id": -10012345,
                            "type": self.chat_type,
                            "permissions": {"can_send_messages": True},
                        }
                    )
                return FakeGetResponse(self.status)

        publisher = TelegramPublisher(
            "test-token",
            "@tekz",
            "https://t.me/tekz",
            "https://t.me/discussion",
            additional_chat_id="@baslmahmod",
        )
        publisher.bot_id = 77
        publisher.session = MembershipSession({"status": "member"})  # type: ignore[assignment]
        self.assertEqual(publisher.verify_additional_destination(), -10012345)

        publisher.session = MembershipSession(  # type: ignore[assignment]
            {"status": "restricted", "can_send_messages": False}
        )
        with self.assertRaises(TelegramPermissionError):
            publisher.verify_additional_destination()

        publisher.session = MembershipSession(  # type: ignore[assignment]
            {"status": "administrator", "can_post_messages": True},
            "channel",
        )
        self.assertEqual(publisher.verify_additional_destination(), -10012345)

        publisher.session = MembershipSession(  # type: ignore[assignment]
            {"status": "administrator", "can_post_messages": False},
            "channel",
        )
        with self.assertRaises(TelegramPermissionError):
            publisher.verify_additional_destination()

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
                post_style=SOFTWARE_SPOTLIGHT,
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
            self.assertEqual(database.recent_post_styles(), [SOFTWARE_SPOTLIGHT])

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
        database.recent_post_styles.return_value = []
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

    def test_cycle_retries_invalid_photo_with_a_standard_fallback(self) -> None:
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
        article = sample_article()
        database = Mock()
        database.is_empty.return_value = False
        database.claim.return_value = True
        database.poll_created_within.return_value = True
        database.posted_count.return_value = 1
        database.recent_post_styles.return_value = []
        rewriter = Mock()
        rewriter.rewrite.return_value = sample_rewrite()
        publisher = Mock()
        publisher.publish_photo.side_effect = [
            RuntimeError("Telegram sendPhoto failed: Bad Request: PHOTO_INVALID_DIMENSIONS"),
            "23",
        ]
        original_image = ResolvedImage(
            b"source",
            "source.png",
            "image/png",
            "https://example.com/source.png",
        )
        fallback_image = ResolvedImage(
            b"fallback",
            "fallback.jpg",
            "image/jpeg",
            FALLBACK_IMAGES["ai"],
        )
        image_resolver = Mock()
        image_resolver.resolve.return_value = original_image
        image_resolver.resolve_fallback.return_value = fallback_image

        with patch("tekz_auto_news.bot.load_sources", return_value=[{"name": "feed"}]), patch(
            "tekz_auto_news.bot.fetch_latest", return_value=[article]
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
        self.assertEqual(publisher.publish_photo.call_count, 2)
        self.assertEqual(database.mark_posted.call_args.args[3], fallback_image.url)

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
