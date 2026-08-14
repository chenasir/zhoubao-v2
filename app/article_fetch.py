"""Fetch article HTML, body text, and publication metadata in one request."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .article_meta import resolve_publication_date
from .retriever import _BODY_SELECTORS, fetch_public_response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArticleContent:
    body: str
    html: str
    published_at: str | None


def fetch_article(url: str, max_chars: int = 6000) -> ArticleContent:
    try:
        html = fetch_public_response(url).text
    except Exception as exc:
        logger.warning("Article fetch failed: %s -> %s", url, exc)
        return ArticleContent(body="", html="", published_at=None)

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    body = ""
    for selector in _BODY_SELECTORS:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) > 200:
                body = text[:max_chars]
                break
    if not body:
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        body = " ".join(p for p in paragraphs if p)[:max_chars]

    pub = resolve_publication_date(feed_published_at=None, html=html, text=f"{url}\n{body}")
    return ArticleContent(
        body=body,
        html=html,
        published_at=pub.isoformat() if pub else None,
    )
