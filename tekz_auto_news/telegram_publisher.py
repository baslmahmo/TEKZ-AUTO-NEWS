from __future__ import annotations

import html
import json
import logging
from urllib.parse import urlencode

import requests

from tekz_auto_news.images import ResolvedImage
from tekz_auto_news.models import ArabicRewrite, NewsArticle

logger = logging.getLogger(__name__)
TELEGRAM_CAPTION_LIMIT = 1024
FOLLOW_URL = "https://linktr.ee/loversincere29"
FOLLOW_TEXT = "📲 تابع TEKZ لأحدث أخبار التقنية والذكاء الاصطناعي"


class TelegramPermissionError(RuntimeError):
    """Raised when Telegram refuses to publish because the bot lacks channel rights."""


def _short_caption(_article: NewsArticle, rewrite: ArabicRewrite) -> str:
    title = html.escape(rewrite.title, quote=False)
    hook = html.escape(rewrite.hook, quote=False)
    summary = html.escape(rewrite.summary, quote=False)
    tags = rewrite.hashtags or ("تقنية", "ذكاء_اصطناعي", "أخبار_التقنية")
    hashtags = " ".join(f"#{html.escape(tag.lstrip('#'), quote=False)}" for tag in tags[:5])
    follow_url = html.escape(FOLLOW_URL, quote=True)
    link_line = f'للمتابعة اضغط هنا\n<a href="{follow_url}">{html.escape(FOLLOW_URL)}</a>'

    def compose(summary_text: str) -> str:
        parts = [f"<b>{title}</b>"]
        if hook:
            parts.append(f"<b>⚡ {hook}</b>")
        if summary_text:
            parts.append(summary_text)
        parts.extend((FOLLOW_TEXT, hashtags, link_line))
        return "\n\n".join(parts)

    caption = compose(summary)
    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        return caption

    fixed = compose("")
    if len(fixed) > TELEGRAM_CAPTION_LIMIT:
        raise ValueError("The title and follow links exceed Telegram's photo caption limit.")

    low, high = 0, len(rewrite.summary)
    best = fixed
    while low <= high:
        midpoint = (low + high) // 2
        shortened = rewrite.summary[:midpoint].rstrip()
        if midpoint < len(rewrite.summary) and shortened:
            shortened += "…"
        candidate = compose(html.escape(shortened, quote=False))
        if len(candidate) <= TELEGRAM_CAPTION_LIMIT:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


class TelegramPublisher:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        channel_url: str,
        group_url: str,
        timeout_seconds: int = 30,
        additional_chat_id: str | None = None,
    ) -> None:
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.channel_url = channel_url
        self.group_url = group_url
        self.timeout_seconds = timeout_seconds
        self.additional_chat_id = additional_chat_id
        self.session = requests.Session()
        self.bot_id: int | None = None
        self.destination_chat_id: int | None = None
        self.additional_destination_chat_id: int | None = None

    def verify_connection(self) -> str:
        response = self.session.get(f"{self.base_url}/getMe", timeout=self.timeout_seconds)
        payload = self._read_response(response, "Telegram getMe")
        bot = payload.get("result", {})
        username = bot.get("username")
        if not username:
            raise RuntimeError("Telegram returned no bot username.")
        self.bot_id = int(bot["id"])
        return str(username)

    def verify_destination(self) -> int:
        if self.bot_id is None:
            self.verify_connection()
        response = self.session.get(
            f"{self.base_url}/getChat",
            params={"chat_id": self.chat_id},
            timeout=self.timeout_seconds,
        )
        payload = self._read_response(response, "Telegram getChat")
        chat = payload.get("result", {})
        chat_id = int(chat["id"])
        if chat.get("type") not in {"channel", "group", "supergroup"}:
            raise RuntimeError("TELEGRAM_CHAT_ID must point to a Telegram group or channel.")

        member_response = self.session.get(
            f"{self.base_url}/getChatMember",
            params={"chat_id": chat_id, "user_id": self.bot_id},
            timeout=self.timeout_seconds,
        )
        member_payload = self._read_response(member_response, "Telegram getChatMember")
        member = member_payload.get("result", {})
        if member.get("status") not in {"administrator", "creator"}:
            raise TelegramPermissionError("The bot must be an administrator in the configured Telegram destination.")
        if chat.get("type") == "channel" and member.get("can_post_messages") is False:
            raise TelegramPermissionError("The bot does not have permission to post messages in the channel.")
        self.destination_chat_id = chat_id
        return chat_id

    def verify_additional_destination(self) -> int | None:
        self.additional_destination_chat_id = None
        if not self.additional_chat_id:
            return None
        if self.bot_id is None:
            self.verify_connection()

        response = self.session.get(
            f"{self.base_url}/getChat",
            params={"chat_id": self.additional_chat_id},
            timeout=self.timeout_seconds,
        )
        chat = self._read_response(response, "Telegram getChat").get("result", {})
        chat_type = chat.get("type")
        if chat_type not in {"group", "supergroup", "channel"}:
            raise RuntimeError(
                "The additional Telegram username resolved to "
                f"{chat_type or 'an unknown chat type'}, not a group or channel."
            )
        chat_id = int(chat["id"])

        member_response = self.session.get(
            f"{self.base_url}/getChatMember",
            params={"chat_id": chat_id, "user_id": self.bot_id},
            timeout=self.timeout_seconds,
        )
        member = self._read_response(member_response, "Telegram getChatMember").get("result", {})
        status = member.get("status")
        if chat_type == "channel":
            if status not in {"administrator", "creator"}:
                raise TelegramPermissionError(
                    "The bot must be a channel administrator to publish to the additional destination."
                )
            if member.get("can_post_messages") is False:
                raise TelegramPermissionError(
                    "The bot does not have permission to post in the additional channel."
                )
            self.additional_destination_chat_id = chat_id
            return chat_id

        if status in {"left", "kicked"} or status not in {
            "member",
            "administrator",
            "creator",
            "restricted",
        }:
            raise TelegramPermissionError(
                "Add the bot to the additional Telegram group and allow it to send messages."
            )
        if status == "restricted" and member.get("can_send_messages") is False:
            raise TelegramPermissionError(
                "The bot is in the additional Telegram group but cannot send messages."
            )
        if (
            status == "member"
            and (chat.get("permissions") or {}).get("can_send_messages") is False
        ):
            raise TelegramPermissionError(
                "Group permissions currently prevent the bot from sending messages."
            )

        self.additional_destination_chat_id = chat_id
        return chat_id

    def publish_photo(
        self,
        article: NewsArticle,
        rewrite: ArabicRewrite,
        image: ResolvedImage,
    ) -> str:
        caption = _short_caption(article, rewrite)
        share_url = "https://t.me/share/url?" + urlencode(
            {"url": article.link, "text": rewrite.title}
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "🔔 اشترك في TEKZ", "url": self.channel_url},
                    {"text": "📤 مشاركة الخبر", "url": share_url},
                ],
                [{"text": "💬 ناقش الخبر", "url": self.group_url}],
            ]
        }
        message_id = self._send_photo(
            self.chat_id,
            caption,
            reply_markup,
            image,
        )
        if self.additional_destination_chat_id is not None:
            try:
                group_message_id = self._send_photo(
                    self.additional_destination_chat_id,
                    caption,
                    reply_markup,
                    image,
                )
            except Exception as exc:
                logger.warning(
                    "Could not publish the story to the additional Telegram group (%s).",
                    type(exc).__name__,
                )
                if isinstance(exc, TelegramPermissionError):
                    self.additional_destination_chat_id = None
            else:
                logger.info(
                    "Published story copy to the additional Telegram group (message id %s).",
                    group_message_id,
                )
        return message_id

    def _send_photo(
        self,
        chat_id: int | str,
        caption: str,
        reply_markup: dict,
        image: ResolvedImage,
    ) -> str:
        response = self.session.post(
            f"{self.base_url}/sendPhoto",
            data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
            },
            files={"photo": (image.filename, image.content, image.content_type)},
            timeout=self.timeout_seconds,
        )
        payload = self._read_response(response, "Telegram sendPhoto")
        message_id = payload.get("result", {}).get("message_id")
        if message_id is None:
            raise RuntimeError("Telegram accepted the request but did not return a message id.")
        return str(message_id)

    def send_message(self, chat_id: int | str, text: str, *, reply_to_message_id: int | None = None) -> None:
        data: dict[str, str | int] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = reply_to_message_id
        response = self.session.post(
            f"{self.base_url}/sendMessage",
            data=data,
            timeout=self.timeout_seconds,
        )
        self._read_response(response, "Telegram sendMessage")

    def send_poll(self, question: str, options: tuple[str, ...] | list[str]) -> dict:
        response = self.session.post(
            f"{self.base_url}/sendPoll",
            data={
                "chat_id": self.chat_id,
                "question": question,
                "options": json.dumps(list(options), ensure_ascii=False),
                "is_anonymous": "true",
                "allows_multiple_answers": "false",
            },
            timeout=self.timeout_seconds,
        )
        payload = self._read_response(response, "Telegram sendPoll")
        poll = payload.get("result", {}).get("poll")
        if not isinstance(poll, dict):
            raise RuntimeError("Telegram accepted the poll request but returned no poll details.")
        return poll

    def get_updates(self, offset: int | None, *, timeout: int = 20) -> list[dict]:
        params: dict[str, str | int] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "poll"]),
        }
        if offset is not None:
            params["offset"] = offset
        response = self.session.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=max(self.timeout_seconds, timeout + 5),
        )
        payload = self._read_response(response, "Telegram getUpdates")
        updates = payload.get("result", [])
        return updates if isinstance(updates, list) else []

    def is_chat_admin(self, chat_id: int | str, user_id: int) -> bool:
        response = self.session.get(
            f"{self.base_url}/getChatMember",
            params={"chat_id": chat_id, "user_id": user_id},
            timeout=self.timeout_seconds,
        )
        member = self._read_response(response, "Telegram getChatMember").get("result", {})
        return member.get("status") in {"administrator", "creator"}

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