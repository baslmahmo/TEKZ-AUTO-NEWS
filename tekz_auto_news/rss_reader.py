from __future__ import annotations

import calendar
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import feedparser
import requests

from tekz_auto_news.models import NewsArticle

logger = logging.getLogger(__name__)
USER_AGENT = "TEKZ-AUTO-NEWS/1.0 (RSS news reader)"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(value))).strip()


def _entry_image(entry: Any) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key, []) or []:
            url = media.get("url")
            if url:
                return str(url)

    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" or str(link.get("type", "")).startswith("image/"):
            if link.get("href"):
                return str(link["href"])

    fragments = [entry.get("summary", "")]
    for content in entry.get("content", []) or []:
        fragments.append(content.get("value", ""))
    for fragment in fragments:
        match = re.search(r"""<img[^>]+src=["']([^"']+)["']""", str(fragment), flags=re.I)
        if match:
            return html.unescape(match.group(1))
    return None


def _entry_date(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(parsed), timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return None


def load_sources(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"RSS source list not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("rss_sources.json must contain a non-empty 'sources' list.")

    validated: list[dict[str, str]] = []
    for index, source in enumerate(sources, start=1):
        if not all(source.get(key) for key in ("name", "url", "category")):
            raise ValueError(f"RSS source #{index} must include name, url, and category.")
        validated.append(
            {
                "name": str(source["name"]),
                "url": str(source["url"]),
                "category": str(source["category"]),
            }
        )
    return validated


def fetch_latest(
    sources: list[dict[str, str]],
    *,
    timeout_seconds: int,
    max_age_hours: int,
    max_items_per_feed: int = 50,
) -> list[NewsArticle]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    articles: list[NewsArticle] = []
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"})

    for source in sources:
        try:
            response = session.get(source["url"], timeout=timeout_seconds)
            response.raise_for_status()
            parsed_feed = feedparser.parse(response.content)
            if not parsed_feed.entries:
                raise ValueError("The feed returned no parseable entries.")

            source_articles: list[NewsArticle] = []
            for entry in parsed_feed.entries:
                title = _plain_text(str(entry.get("title", "")))
                link = str(entry.get("link", "")).strip()
                if not title or not link.startswith(("https://", "http://")):
                    continue

                published_at = _entry_date(entry)
                if published_at and published_at < cutoff:
                    continue

                text = _plain_text(str(entry.get("summary", "")))
                if not text:
                    content = entry.get("content", []) or []
                    if content:
                        text = _plain_text(str(content[0].get("value", "")))

                source_articles.append(
                    NewsArticle(
                        source_name=source["name"],
                        category=source["category"],
                        title=title,
                        link=link,
                        text=text[:12000],
                        published_at=published_at,
                        image_url=_entry_image(entry),
                    )
                )
            source_articles.sort(
                key=lambda article: article.published_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            articles.extend(source_articles[:max_items_per_feed])
            logger.info("Read %s recent entries from %s", len(source_articles), source["name"])
        except Exception as exc:
            logger.warning("Could not read RSS source %s: %s", source["name"], exc)

    articles.sort(
        key=lambda article: article.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return articles