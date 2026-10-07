from __future__ import annotations

import re

from tekz_auto_news.models import NewsArticle

APP_EXPLAINER = "app_explainer"
BLOG_STORY = "blog_story"
SOFTWARE_SPOTLIGHT = "software_spotlight"

STYLE_INSTRUCTIONS = {
    APP_EXPLAINER: (
        "قدّم الخبر كشرح عملي لتطبيق: وضّح ما الذي يقدمه التطبيق ولمن قد يفيد اعتمادًا على المصدر فقط. "
        "لا تخترع خطوات استخدام أو سعرًا أو توافقًا مع أجهزة غير مذكور."
    ),
    BLOG_STORY: (
        "اكتبه كتدوينة تقنية قصيرة وطبيعية: ابدأ من المعلومة الأهم، ثم اربطها بسياق الخبر أو أثره الواضح. "
        "لا تضف رأيًا أو توقعًا غير مدعوم بالمصدر."
    ),
    SOFTWARE_SPOTLIGHT: (
        "عرّف بالبرنامج أو الأداة بوضوح: ما وظيفته، وما الميزة المذكورة في المصدر، ومن قد يستفيد منه. "
        "لا تخترع توفرًا أو مزايا أو نتائج لم يذكرها المصدر."
    ),
}

_APP_TERMS = (
    "application",
    "mobile app",
    "app store",
    "google play",
    "تطبيق",
    "تطبيقات",
)
_SOFTWARE_TERMS = (
    "software",
    "program",
    "tool",
    "platform",
    "برنامج",
    "برمجية",
    "أداة",
    "منصة",
    "نموذج",
)
_MODEL_RELEASE_TERMS = (
    "launch",
    "launches",
    "released",
    "unveiled",
    "introduces",
    "announced",
    "إطلاق",
    "تطلق",
    "يكشف",
    "تعلن",
)


def _contains_english_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def choose_post_style(article: NewsArticle, recent_styles: list[str] | tuple[str, ...]) -> str:
    """Choose a relevant format and avoid repeating recent formats when possible."""
    text = f"{article.title} {article.category} {article.text}".casefold()
    has_app = any(term in text for term in _APP_TERMS) or _contains_english_word(text, "app")
    title = article.title.casefold()
    has_software = any(term in text for term in _SOFTWARE_TERMS) or any(
        _contains_english_word(text, term) for term in ("program", "tool", "platform")
    )
    if not has_software and _contains_english_word(title, "model"):
        has_software = any(term in title for term in _MODEL_RELEASE_TERMS)

    if has_app:
        candidates = (APP_EXPLAINER, BLOG_STORY, SOFTWARE_SPOTLIGHT)
    elif has_software:
        candidates = (SOFTWARE_SPOTLIGHT, BLOG_STORY)
    else:
        candidates = (BLOG_STORY,)

    recent = set(recent_styles[:2])
    return next((style for style in candidates if style not in recent), candidates[0])
