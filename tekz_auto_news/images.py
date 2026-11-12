from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from tekz_auto_news.models import NewsArticle

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
FALLBACK_IMAGES = {
    "ai": "https://images.unsplash.com/photo-1677442136019-21780ecad995?auto=format&fit=crop&w=1280&q=82",
    "technology": "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=1280&q=82",
}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class _OpenGraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta" or self.image:
            return
        values = {key.lower(): value for key, value in attrs if value}
        property_name = (values.get("property") or values.get("name") or "").lower()
        if property_name in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            self.image = values.get("content")


@dataclass(frozen=True)
class ResolvedImage:
    content: bytes
    filename: str
    content_type: str
    url: str


class ImageResolver:
    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; TEKZ-AUTO-NEWS/1.0)"})

    def _open_graph_image(self, article_url: str) -> str | None:
        try:
            with self.session.get(
                article_url,
                timeout=self.timeout_seconds,
                headers={"Accept": "text/html,application/xhtml+xml"},
                stream=True,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" not in content_type:
                    return None
                content = response.raw.read(1024 * 1024 + 1, decode_content=True)
                if len(content) > 1024 * 1024:
                    return None
                parser = _OpenGraphParser()
                parser.feed(content.decode(response.encoding or "utf-8", errors="replace"))
                return urljoin(response.url, parser.image) if parser.image else None
        except Exception as exc:
            logger.debug("Could not read an Open Graph image for %s: %s", article_url, exc)
            return None

    def _download(self, url: str) -> ResolvedImage | None:
        try:
            with self.session.get(url, timeout=self.timeout_seconds, stream=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in ALLOWED_IMAGE_TYPES:
                    logger.debug("Ignoring unsupported image type %s from %s", content_type, url)
                    return None
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_IMAGE_BYTES:
                    logger.debug("Ignoring image larger than the upload limit: %s", url)
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        logger.debug("Ignoring image larger than the upload limit: %s", url)
                        return None
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    return None

                suffix = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }[content_type]
                return ResolvedImage(content, f"news{suffix}", content_type, response.url)
        except Exception as exc:
            logger.debug("Could not download image %s: %s", url, exc)
            return None

    @staticmethod
    def _fallback_url(article: NewsArticle) -> str:
        category = (
            "ai"
            if article.category.lower()
            in {"ai", "artificial intelligence", "ذكاء اصطناعي"}
            else "technology"
        )
        return FALLBACK_IMAGES[category]

    def resolve_fallback(self, article: NewsArticle) -> ResolvedImage:
        fallback_url = self._fallback_url(article)
        image = self._download(fallback_url)
        if image is None:
            raise RuntimeError("Could not download the safe fallback image for this article.")
        return image

    def resolve(self, article: NewsArticle) -> ResolvedImage:
        candidates: list[str] = []
        if article.image_url:
            candidates.append(urljoin(article.link, article.image_url))
        og_image = self._open_graph_image(article.link)
        if og_image:
            candidates.append(og_image)

        candidates.append(self._fallback_url(article))

        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            image = self._download(candidate)
            if image:
                return image
        raise RuntimeError(f"No usable article or fallback image could be downloaded for: {article.link}")