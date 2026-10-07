from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsArticle:
    source_name: str
    category: str
    title: str
    link: str
    text: str
    published_at: datetime | None
    image_url: str | None


@dataclass(frozen=True)
class ArabicRewrite:
    title: str
    summary: str
    hook: str = ""
    hashtags: tuple[str, ...] = ()
    style: str = "blog_story"