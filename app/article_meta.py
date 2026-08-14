"""Extract publication dates from article HTML and text."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone, timezone
from typing import Any

from bs4 import BeautifulSoup

MONTHS_EN = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

META_PUBLISHED_KEYS = (
    "article:published_time",
    "og:published_time",
    "article:published",
    "publishdate",
    "pubdate",
    "date",
    "sailthru.date",
    "parsely-pub-date",
)

ISO_DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
EN_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b",
    re.I,
)
ZH_DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _parse_iso_datetime(raw: str) -> date | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        match = ISO_DATE_RE.search(value)
        if not match:
            return None
        year, month, day = (int(match.group(i)) for i in range(1, 4))
        try:
            return date(year, month, day)
        except ValueError:
            return None


def _parse_feed_date(value: str | None) -> date | None:
    return _parse_iso_datetime(value or "")


def _dates_from_jsonld(node: Any, out: list[date]) -> None:
    if isinstance(node, dict):
        for key in ("datePublished", "dateCreated", "uploadDate"):
            parsed = _parse_iso_datetime(str(node.get(key) or ""))
            if parsed:
                out.append(parsed)
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                _dates_from_jsonld(item, out)
    elif isinstance(node, list):
        for item in node:
            _dates_from_jsonld(item, out)


def extract_published_from_html(html: str) -> date | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    candidates: list[date] = []

    for key in META_PUBLISHED_KEYS:
        for tag in soup.find_all("meta"):
            if tag.get("property") == key or tag.get("name") == key:
                parsed = _parse_iso_datetime(tag.get("content") or "")
                if parsed:
                    candidates.append(parsed)

    for tag in soup.find_all("time"):
        parsed = _parse_iso_datetime(tag.get("datetime") or tag.get_text(" ", strip=True))
        if parsed:
            candidates.append(parsed)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        _dates_from_jsonld(payload, candidates)

    if not candidates:
        return None
    return min(candidates)


def extract_published_from_text(text: str, *, prefer_head: bool = True) -> date | None:
    if not text:
        return None
    sample = text[:1200] if prefer_head else text
    candidates: list[date] = []

    for match in EN_DATE_RE.finditer(sample):
        month = MONTHS_EN[match.group(1).lower()]
        day = int(match.group(2))
        year = int(match.group(3))
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue

    for match in ISO_DATE_RE.finditer(sample):
        year, month, day = (int(match.group(i)) for i in range(1, 4))
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue

    for match in ZH_DATE_RE.finditer(sample):
        year, month, day = (int(match.group(i)) for i in range(1, 4))
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue

    if not candidates:
        return None
    return min(candidates)


def is_suspicious_feed_date(value: str | None) -> bool:
    parsed = _parse_feed_date(value or "")
    if not parsed:
        return True
    return parsed == datetime.now(timezone.utc).date()


def resolve_publication_date(
    *,
    feed_published_at: str | None,
    html: str | None,
    text: str,
) -> date | None:
    """Pick the best publication date, avoiding feed dates that default to today."""
    feed_date = _parse_feed_date(feed_published_at)
    html_date = extract_published_from_html(html or "")
    text_date = extract_published_from_text(text)

    ranked: list[tuple[int, date]] = []
    if html_date:
        ranked.append((0, html_date))
    if text_date and text_date != feed_date:
        ranked.append((1, text_date))
    if feed_date and not is_suspicious_feed_date(feed_published_at):
        ranked.append((2, feed_date))
    if text_date:
        ranked.append((3, text_date))
    if feed_date:
        ranked.append((4, feed_date))

    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][1]
