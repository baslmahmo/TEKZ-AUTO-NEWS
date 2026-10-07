from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tekz_auto_news.models import NewsArticle


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_url(value: str) -> str:
    value = value.strip()
    value = re.sub(r"#.*$", "", value)
    value = re.sub(r"([?&])(utm_[^=&]+|fbclid|gclid|mc_cid|mc_eid)=[^&]*", "", value, flags=re.I)
    value = value.replace("?&", "?").rstrip("?&")
    return value


def _fingerprint(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = "".join(char for char in normalized if char.isalnum() or char.isspace())
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class NewsDatabase:
    """Persistent article history and duplicate guard backed by SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 20000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_url TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source_name TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    arabic_title TEXT,
                    arabic_summary TEXT,
                    arabic_hook TEXT,
                    hashtags_json TEXT,
                    image_url TEXT,
                    telegram_message_id TEXT,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'processing', 'posted', 'skipped', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    posted_at TEXT,
                    last_error TEXT
                )
                """
            )
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(articles)").fetchall()
            }
            for column, definition in (
                ("arabic_hook", "TEXT"),
                ("hashtags_json", "TEXT"),
                ("telegram_message_id", "TEXT"),
            ):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE articles ADD COLUMN {column} {definition}")

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS polls (
                    poll_id TEXT PRIMARY KEY,
                    article_url TEXT NOT NULL,
                    question TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    total_voter_count INTEGER NOT NULL DEFAULT 0,
                    results_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status, last_attempt_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_polls_created_at ON polls(created_at)"
            )

    def claim(self, article: NewsArticle, *, retry_after_hours: int = 1) -> bool:
        """Reserve an article for one attempt; posted stories are never claimed again."""
        url = _canonical_url(article.link)
        fingerprint = _fingerprint(article.title)
        now = _utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, status, last_attempt_at
                       , last_error
                FROM articles
                WHERE article_url = ? OR fingerprint = ?
                LIMIT 1
                """,
                (url, fingerprint),
            ).fetchone()

            if row:
                if row["status"] in {"posted", "skipped"}:
                    return False
                last_attempt = row["last_attempt_at"]
                last_error = str(row["last_error"] or "").lower()
                bypass_retry_window = any(
                    marker in last_error
                    for marker in (
                        "credit_balance_exhausted",
                        "insufficient_quota",
                        "openai api credits are exhausted",
                        "insufficient credits",
                        "credits exhausted",
                        "payment required",
                        "not enough rights",
                        "telegram permission",
                        "bot was kicked",
                        "bot was blocked",
                        "chat not found",
                    )
                )
                if last_attempt and not bypass_retry_window:
                    try:
                        attempted_at = datetime.fromisoformat(last_attempt)
                    except ValueError:
                        attempted_at = now - timedelta(days=1)
                    if now - attempted_at < timedelta(hours=retry_after_hours):
                        return False
                connection.execute(
                    """
                    UPDATE articles
                    SET status = 'processing', attempts = attempts + 1,
                        last_attempt_at = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (now.isoformat(), row["id"]),
                )
                return True

            connection.execute(
                """
                INSERT INTO articles (
                    article_url, fingerprint, source_name, original_title,
                    status, attempts, first_seen_at, last_attempt_at
                )
                VALUES (?, ?, ?, ?, 'processing', 1, ?, ?)
                """,
                (url, fingerprint, article.source_name, article.title, now.isoformat(), now.isoformat()),
            )
            return True

    def mark_posted(
        self,
        article: NewsArticle,
        arabic_title: str,
        arabic_summary: str,
        image_url: str,
        *,
        arabic_hook: str = "",
        hashtags: tuple[str, ...] = (),
        message_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE articles
                SET status = 'posted', arabic_title = ?, arabic_summary = ?,
                    arabic_hook = ?, hashtags_json = ?, image_url = ?,
                    telegram_message_id = ?, posted_at = ?, last_error = NULL
                WHERE article_url = ? OR fingerprint = ?
                """,
                (
                    arabic_title,
                    arabic_summary,
                    arabic_hook,
                    json.dumps(hashtags, ensure_ascii=False),
                    image_url,
                    message_id,
                    _utc_now().isoformat(),
                    _canonical_url(article.link),
                    _fingerprint(article.title),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Could not mark the published article in SQLite.")

    def mark_skipped(self, article: NewsArticle, reason: str) -> None:
        self._update_status(article, "skipped", reason[:800])

    def mark_failed(self, article: NewsArticle, reason: str) -> None:
        self._update_status(article, "failed", reason[:800])

    def _update_status(self, article: NewsArticle, status: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE articles
                SET status = ?, last_error = ?
                WHERE article_url = ? OR fingerprint = ?
                """,
                (status, error, _canonical_url(article.link), _fingerprint(article.title)),
            )

    def posted_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM articles WHERE status = 'posted'"
            ).fetchone()
        return int(row["total"])

    def record_poll(self, article: NewsArticle, poll: dict) -> None:
        poll_id = str(poll.get("id", "")).strip()
        question = str(poll.get("question", "")).strip()
        if not poll_id or not question:
            raise ValueError("Telegram returned an incomplete poll.")
        options = poll.get("options", [])
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO polls (
                    poll_id, article_url, question, options_json,
                    total_voter_count, results_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    poll_id,
                    _canonical_url(article.link),
                    question,
                    json.dumps(options, ensure_ascii=False),
                    int(poll.get("total_voter_count", 0)),
                    json.dumps(poll, ensure_ascii=False),
                    now,
                    now,
                ),
            )

    def update_poll(self, poll: dict) -> None:
        poll_id = str(poll.get("id", "")).strip()
        if not poll_id:
            return
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE polls
                SET options_json = ?, total_voter_count = ?,
                    results_json = ?, updated_at = ?
                WHERE poll_id = ?
                """,
                (
                    json.dumps(poll.get("options", []), ensure_ascii=False),
                    int(poll.get("total_voter_count", 0)),
                    json.dumps(poll, ensure_ascii=False),
                    _utc_now().isoformat(),
                    poll_id,
                ),
            )

    def poll_created_within(self, hours: int) -> bool:
        cutoff = (_utc_now() - timedelta(hours=hours)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM polls WHERE created_at >= ? LIMIT 1",
                (cutoff,),
            ).fetchone()
        return row is not None

    def stats(self) -> dict[str, int | str | None]:
        with self._connect() as connection:
            article_row = connection.execute(
                """
                SELECT COUNT(*) AS posted,
                       MAX(posted_at) AS last_posted_at
                FROM articles
                WHERE status = 'posted'
                """
            ).fetchone()
            attempts_row = connection.execute(
                "SELECT COALESCE(SUM(attempts), 0) AS attempts FROM articles"
            ).fetchone()
            poll_row = connection.execute(
                """
                SELECT COUNT(*) AS polls,
                       COALESCE(SUM(total_voter_count), 0) AS votes
                FROM polls
                """
            ).fetchone()
        return {
            "posted": int(article_row["posted"]),
            "attempts": int(attempts_row["attempts"]),
            "last_posted_at": article_row["last_posted_at"],
            "polls": int(poll_row["polls"]),
            "votes": int(poll_row["votes"]),
        }

    def is_empty(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM articles LIMIT 1").fetchone()
        return row is None

    def mark_initial_backlog_skipped(self, articles: list[NewsArticle]) -> None:
        """Remember first-run backlog without sending it to the channel later."""
        if not articles:
            return
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO articles (
                    article_url, fingerprint, source_name, original_title,
                    status, attempts, first_seen_at, last_error
                )
                VALUES (?, ?, ?, ?, 'skipped', 0, ?, 'Initial backlog skipped to prevent a burst of posts.')
                """,
                [
                    (
                        _canonical_url(article.link),
                        _fingerprint(article.title),
                        article.source_name,
                        article.title,
                        now,
                    )
                    for article in articles
                ],
            )