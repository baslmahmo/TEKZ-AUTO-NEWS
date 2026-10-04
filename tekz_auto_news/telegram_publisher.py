from __future__ import annotations

import html
import logging

import requests

from tekz_auto_news.images import ResolvedImage
from tekz_auto_news.models import ArabicRewrite, NewsArticle

logger = logging.getLogger(__name__)
TELEGRAM_CAPTION_LIMIT = 1024


class TelegramPermissionError(RuntimeError):
    """Raised when Telegram refuses to publish because the bot lacks channel rights."""


def _short_caption(article: NewsArticle, rewrite: ArabicRewrite) -> str:
    title = html.escape(rewrite.title, quote=False)
    source = html.escape(article.source_name, quote=False)
    link = html.escape(article.link, quote=True)
    attribution = f'<a href="{link}">المصدر: {source}</a>'
    suffix = "\n\n#تقنية #ذكاء_اصطناعي"
    summary = html.escape(rewrite.summary, quote=False)
    caption = f"<b>{title}</b>\n\n{summary}\n\n{attribution}{suffix}"

    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption

    available = TELEGRAM_CAPTION_LIMIT - len(f"<b>{title}</b>\n\n\n\n{attribution}{suffix}") - 20
    if available < 80:
        available = 80
    summary = rewrite.summary[:available].rsplit(" ", 1)[0].rstrip() + "…"
    caption = f"<b>{title}</b>\n\n{html.escape(summary)}\n\n{attribution}{suffix}"
    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        raise ValueError("The generated caption exceeds Telegram's photo caption limit.")
    return caption


class TelegramPublisher:
    def __init__(self, bot_token: str, chat_id: str, timeout_seconds: int = 30) -> None:
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def verify_connection(self) -> str:
        response = self.session.get(f"{self.base_url}/getMe", timeout=self.timeout_seconds)
        payload = self._read_response(response, "Telegram getMe")
        username = payload.get("result", {}).get("username")
        if not username:
            raise RuntimeError("Telegram returned no bot username.")
        return str(username)

    def publish_photo(
        self,
        article: NewsArticle,
        rewrite: ArabicRewrite,
        image: ResolvedImage,
    ) -> str:
        caption = _short_caption(article, rewrite)
        response = self.session.post(
            f"{self.base_url}/sendPhoto",
            data={
                "chat_id": self.chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            },
            files={"photo": (image.filename, image.content, image.content_type)},
            timeout=self.timeout_seconds,
        )
        payload = self._read_response(response, "Telegram sendPhoto")
        message_id = payload.get("result", {}).get("message_id")
        if message_id is None:
            raise RuntimeError("Telegram accepted the request but did not return a message id.")
        return str(message_id)

    @staticmethod
    def _read_response(response: requests.Response, operation: str) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{operation} returned an unreadable response (HTTP {response.status_code}).") from exc

        if not response.ok or not payload.get("ok"):
            description = str(payload.get("description", "Unknown Telegram API error"))
            lowered_description = description.lower()
            if any(
                marker in lowered_description
                for marker in ("not enough rights", "bot was kicked", "bot was blocked", "chat not found")
            ):
                raise TelegramPermissionError(f"{operation} failed: {description}")
            raise RuntimeError(f"{operation} failed: {description}")
        return payload