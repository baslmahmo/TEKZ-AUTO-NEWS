from __future__ import annotations

from datetime import datetime, timezone

from tekz_auto_news.models import NewsArticle

AI_TERMS = (
    "artificial intelligence",
    "machine learning",
    "generative ai",
    "large language model",
    "llm",
    "ai ",
    " ai",
    "ذكاء اصطناعي",
)
PRODUCT_TERMS = (
    "launch",
    "launches",
    "launched",
    "introduces",
    "introduced",
    "unveils",
    "unveiled",
    "releases",
    "released",
    "new model",
    "new product",
    "new tool",
    "new feature",
    "announces",
    "announced",
    "إطلاق",
    "تطلق",
    "يكشف",
    "تعلن",
)
MAJOR_COMPANIES = ("openai", "google", "apple", "microsoft", "nvidia", "meta", "anthropic", "amazon")
URGENT_TERMS = ("breakthrough", "first time", "record", "urgent", "عاجل", "اختراق", "الأول")
LOW_VALUE_TERMS = ("opinion", "op-ed", "sponsored", "podcast", "roundup", "أفضل ما")


def score_article(article: NewsArticle, *, now: datetime | None = None) -> int:
    """Rank recent, substantive AI/product reporting above opinion and roundup items."""
    now = now or datetime.now(timezone.utc)
    title = article.title.casefold()
    body = article.text.casefold()
    category = article.category.casefold()
    combined = f"{title} {body}"
    score = 2 if any(term in category for term in ("ai", "artificial intelligence", "ذكاء")) else 1

    if any(term in combined for term in AI_TERMS):
        score += 2
    if any(term in title for term in PRODUCT_TERMS):
        score += 3
    if any(company in title for company in MAJOR_COMPANIES):
        score += 1
    if any(term in title for term in URGENT_TERMS):
        score += 2
    if any(term in title for term in LOW_VALUE_TERMS):
        score -= 4

    if article.published_at:
        age_hours = max(0.0, (now - article.published_at.astimezone(timezone.utc)).total_seconds() / 3600)
        score += 2 if age_hours <= 12 else 1 if age_hours <= 36 else 0
    return score


def rank_articles(articles: list[NewsArticle]) -> list[NewsArticle]:
    """Return only reasonable candidates, with the strongest and newest first."""
    ranked = sorted(articles, key=lambda article: score_article(article), reverse=True)
    return [article for article in ranked if score_article(article) > 0]


def is_poll_worthy(article: NewsArticle) -> bool:
    """Create polls only for concrete launches and product announcements."""
    title = article.title.casefold()
    return any(term in title for term in PRODUCT_TERMS)
