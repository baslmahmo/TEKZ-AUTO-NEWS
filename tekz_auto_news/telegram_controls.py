from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from tekz_auto_news.database import NewsDatabase
from tekz_auto_news.telegram_publisher import TelegramPublisher

logger = logging.getLogger(__name__)


class TelegramUpdateListener(threading.Thread):
    """Long-poll Telegram for admin commands and poll result updates."""

    def __init__(
        self,
        publisher: TelegramPublisher,
        database: NewsDatabase,
        destination_chat_id: int,
        force_scan_event: threading.Event,
        stop_event: threading.Event,
        status_provider: Callable[[], str],
    ) -> None:
        super().__init__(name="telegram-update-listener", daemon=True)
        self.publisher = publisher
        self.database = database
        self.destination_chat_id = destination_chat_id
        self.force_scan_event = force_scan_event
        self.stop_event = stop_event
        self.status_provider = status_provider
        self.offset: int | None = None
        self.started_at = int(time.time())

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                updates = self.publisher.get_updates(self.offset, timeout=20)
            except Exception as exc:
                logger.warning("Telegram update polling failed (%s); retrying shortly.", type(exc).__name__)
                self.stop_event.wait(5)
                continue

            for update in updates:
                if self.stop_event.is_set():
                    break
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.offset = update_id + 1
                poll = update.get("poll")
                if isinstance(poll, dict):
                    try:
                        self.database.update_poll(poll)
                    except Exception:
                        logger.exception("Could not save updated Telegram poll results.")
                    continue
                message = update.get("message")
                if isinstance(message, dict):
                    self._handle_message(message)

    def _handle_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = str(message.get("text", "")).strip()
        if not text.startswith("/") or not sender or sender.get("is_bot"):
            return
        if chat.get("id") != self.destination_chat_id:
            return
        if int(message.get("date", 0)) < self.started_at - 120:
            return

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        try:
            authorized = self.publisher.is_chat_admin(self.destination_chat_id, int(sender["id"]))
        except Exception as exc:
            logger.warning("Could not verify Telegram command permissions (%s).", type(exc).__name__)
            return
        if not authorized:
            logger.info("Ignored a non-admin Telegram command.")
            return

        try:
            if command == "/status":
                self._send_status(chat["id"], message.get("message_id"))
            elif command == "/stats":
                self._send_stats(chat["id"], message.get("message_id"))
            elif command == "/help":
                self.publisher.send_message(
                    chat["id"],
                    "<b>أوامر TEKZ-AUTO-NEWS</b>\n/status — حالة البوت\n/now — فحص ونشر خبر واحد الآن\n/stats — إحصاءات النشر والاستطلاعات\n/help — عرض هذه المساعدة",
                    reply_to_message_id=message.get("message_id"),
                )
            elif command == "/now":
                self.force_scan_event.set()
                self.publisher.send_message(
                    chat["id"],
                    "بدأ طلب الفحص الفوري. سينشر البوت خبرًا واحدًا كحد أقصى.",
                    reply_to_message_id=message.get("message_id"),
                )
        except Exception as exc:
            logger.warning("Could not answer Telegram command %s (%s).", command, type(exc).__name__)

    def _send_status(self, chat_id: int, reply_to_message_id: int | None) -> None:
        stats = self.database.stats()
        last_post = stats["last_posted_at"] or "لا يوجد"
        text = (
            f"<b>الحالة:</b> {self.status_provider()}\n"
            f"<b>المنشورات المسجلة:</b> {stats['posted']}\n"
            f"<b>آخر نشر:</b> {last_post}"
        )
        self.publisher.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)

    def _send_stats(self, chat_id: int, reply_to_message_id: int | None) -> None:
        stats = self.database.stats()
        text = (
            "<b>إحصاءات TEKZ-AUTO-NEWS</b>\n"
            f"الأخبار المنشورة: {stats['posted']}\n"
            f"محاولات النشر: {stats['attempts']}\n"
            f"الاستطلاعات: {stats['polls']}\n"
            f"الأصوات المسجلة: {stats['votes']}"
        )
        self.publisher.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
