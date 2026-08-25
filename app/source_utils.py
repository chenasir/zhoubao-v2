"""Source normalization helpers."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .config import load_source_aliases


GENERIC_SOURCE_NAMES = {
    "",
    "news",
    "google news",
    "source",
    "original publisher",
    "manual",
    "web",
    "website",
    "wechat",
    "weixin",
    "公众号",
    "微信公众号",
    "微信公众平台",
    "网站",
    "网页",
    "转载",
    "媒体",
    "待识别来源",
}

GENERIC_SOURCE_HOSTS = {
    "news.google.com",
    "mp.weixin.qq.com",
    "weixin.qq.com",
}

SOURCE_NAME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Reuters", re.compile(r"\bReuters\b|路透社|路透", re.IGNORECASE)),
    ("Zawya", re.compile(r"\bZawya\b", re.IGNORECASE)),
    ("Bloomberg", re.compile(r"\bBloomberg\b|彭博社?|彭博", re.IGNORECASE)),
    ("Arab News", re.compile(r"\bArab News\b", re.IGNORECASE)),
    ("The National", re.compile(r"\bThe National\b", re.IGNORECASE)),
    ("Saudi Gazette", re.compile(r"\bSaudi Gazette\b", re.IGNORECASE)),
    ("KED Global", re.compile(r"\bKED Global\b", re.IGNORECASE)),
    ("Korea JoongAng Daily", re.compile(r"\bKorea JoongAng Daily\b", re.IGNORECASE)),
    ("Business Korea", re.compile(r"\bBusiness Korea\b", re.IGNORECASE)),
    ("Qazinform", re.compile(r"\bQazinform\b", re.IGNORECASE)),
    ("SWFI", re.compile(r"\bSWFI\b|Sovereign Wealth Fund Institute", re.IGNORECASE)),
    ("The Real Deal", re.compile(r"\bThe Real Deal\b", re.IGNORECASE)),
)

ATTRIBUTION_MARKER_RE = re.compile(
    r"(?:原文来源|文章来源|消息来源|来源|转载自|转自|稿源|source|via)\s*[:：]?\s*",
    re.IGNORECASE,
)

DOMAIN_SOURCE_RE = re.compile(
    r"^(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,24}$",
    re.IGNORECASE,
)

DOMAIN_BRANDS = {
    "marketscreener.com": "MarketScreener",
    "thenationalnews.com": "The National",
    "swfinstitute.org": "SWFI",
    "therealdeal.com": "The Real Deal",
    "mitsloanme.com": "MIT Sloan Management Review Middle East",
    "egyptoil-gas.com": "Egypt Oil & Gas",
}

SECOND_LEVEL_SUFFIXES = {
    "co.uk",
    "com.au",
    "com.cn",
    "com.hk",
    "com.kz",
    "com.sa",
    "co.kr",
    "co.za",
}


def is_generic_source_name(raw: str) -> bool:
    """Return True for discovery-channel labels that are not publisher names."""
    return (raw or "").strip().lower() in GENERIC_SOURCE_NAMES


def is_carrier_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host in GENERIC_SOURCE_HOSTS


def carrier_name_from_url(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if host in {"mp.weixin.qq.com", "weixin.qq.com"}:
        return "公众号"
    if host == "news.google.com":
        return "Google News"
    return "网站"


def _known_source_in_text(text: str) -> str:
    for canonical, pattern in SOURCE_NAME_PATTERNS:
        if pattern.search(text or ""):
            return canonical
    return ""


def _clean_attribution_candidate(value: str) -> str:
    candidate = (value or "").strip(" \t\r\n|丨｜-—–:：()（）[]【】")
    candidate = re.split(r"[。；;，,|丨｜\r\n]", candidate, maxsplit=1)[0].strip()
    candidate = re.sub(r"\s+(?:20\d{2}[-年/].*)$", "", candidate).strip()
    return candidate[:80]


def _source_name_from_domain(raw: str, aliases: dict) -> str:
    """Convert a bare publisher domain into a readable source label."""
    value = (raw or "").strip().lower().removeprefix("www.").rstrip(".")
    if not DOMAIN_SOURCE_RE.fullmatch(value):
        return ""
    if value in aliases:
        return str(aliases[value])
    if value in DOMAIN_BRANDS:
        return DOMAIN_BRANDS[value]
    parts = value.split(".")
    suffix = ".".join(parts[-2:])
    label_index = -3 if suffix in SECOND_LEVEL_SUFFIXES and len(parts) >= 3 else -2
    label = parts[label_index].replace("-", " ")
    return label.title()


def detect_source_from_content(*, title: str = "", text: str = "", html: str = "") -> str:
    """Identify the credited publisher, not the webpage carrier.

    High-confidence structured/explicit attribution wins. Broad mentions of a
    publisher in ordinary article prose do not automatically become attribution.
    """
    soup = BeautifulSoup(html or "", "lxml") if html else None
    structured_candidates: list[str] = []
    lines: list[str] = []
    if soup is not None:
        for attrs in (
            {"property": "og:site_name"},
            {"name": "application-name"},
            {"name": "publisher"},
        ):
            node = soup.find("meta", attrs=attrs)
            if node and node.get("content"):
                structured_candidates.append(str(node.get("content")))
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                payload = json.loads(node.get_text("", strip=True) or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            records = payload if isinstance(payload, list) else [payload]
            for record in records:
                if not isinstance(record, dict):
                    continue
                publisher = record.get("publisher")
                if isinstance(publisher, dict) and publisher.get("name"):
                    structured_candidates.append(str(publisher["name"]))
        lines.extend(str(s).strip() for s in soup.stripped_strings if str(s).strip())

    for candidate in structured_candidates:
        normalized = normalize_source_name(candidate)
        if normalized and not is_generic_source_name(normalized):
            return normalized

    lines.extend(part.strip() for part in (text or "").splitlines() if part.strip())
    for line in lines:
        marker = ATTRIBUTION_MARKER_RE.search(line)
        if not marker:
            continue
        tail = _clean_attribution_candidate(line[marker.end():])
        known = _known_source_in_text(tail)
        if known:
            return known
        normalized = normalize_source_name(tail)
        if 2 <= len(normalized) <= 60 and not is_generic_source_name(normalized):
            return normalized

    combined = f"{title}\n{text}"
    # Wire-service signatures and explicit reporting phrases are sufficiently
    # strong even when webpage extraction has flattened the source line.
    for canonical, pattern in SOURCE_NAME_PATTERNS:
        strong = re.compile(
            rf"(?:{ATTRIBUTION_MARKER_RE.pattern}|据\s*)[^。；;\n]{{0,20}}(?:{pattern.pattern})",
            re.IGNORECASE,
        )
        if strong.search(combined):
            return canonical
    if re.search(r"\bReporting by\b|\bEditing by\b", combined, re.IGNORECASE):
        return "Reuters"
    return ""


def validate_model_source(raw: str, source_text: str) -> str:
    """Accept AI-detected attribution only when the source text supports it."""
    normalized = normalize_source_name(raw)
    if not normalized or is_generic_source_name(normalized):
        return ""
    for canonical, pattern in SOURCE_NAME_PATTERNS:
        if normalized == canonical:
            return canonical if pattern.search(source_text or "") else ""
    return normalized if normalized.lower() in (source_text or "").lower() else ""


def find_source_article_url(source: str, *, html: str = "", base_url: str = "") -> str:
    """Find a link to the credited publisher in a reposted article."""
    homepage = source_homepage(source)
    expected_host = (urlparse(homepage).hostname or "").lower().removeprefix("www.")
    if not html or not expected_host:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor.get("href") or "").strip())
        host = (urlparse(href).hostname or "").lower().removeprefix("www.")
        if host == expected_host or host.endswith("." + expected_host):
            return href
    return ""


def source_from_google_title(title: str) -> tuple[str, str]:
    """Split Google News title like 'Headline - Reuters' into headline/source."""
    if " - " not in title:
        return title.strip(), ""
    headline, source = title.rsplit(" - ", 1)
    if len(source) > 60:
        return title.strip(), ""
    return headline.strip() or title.strip(), source.strip()


def normalize_source_name(raw: str, url: str = "") -> str:
    aliases = load_source_aliases()
    source = (raw or "").strip()
    if source in aliases.get("aliases", {}):
        return aliases["aliases"][source]

    domain_source = _source_name_from_domain(source, aliases.get("aliases", {}))
    if domain_source:
        return domain_source

    low = source.lower()
    if "bloomberg" in low or "彭博" in source:
        return "Bloomberg"
    if "reuters" in low or "路透" in source:
        return "Reuters"
    if "zawya" in low:
        return "Zawya"
    if "arab news" in low:
        return "Arab News"
    if "saudigazette" in low or "saudi gazette" in low:
        return "Saudi Gazette"
    if "ked global" in low:
        return "KED Global"
    if "chosun" in low:
        return "Chosun Biz"
    if "yahoo" in low:
        return "Yahoo Finance"
    if "business korea" in low:
        return "Business Korea"
    if "swfinstitute" in low or "sovereign wealth fund institute" in low:
        return "SWFI"
    if "mitsloanme" in low or "mit sloan management review middle east" in low:
        return "MIT Sloan Management Review Middle East"
    if "therealdeal" in low or "the real deal" in low:
        return "The Real Deal"
    if "marketscreener" in low:
        return "MarketScreener"
    if "thenationalnews" in low:
        return "The National"
    if "egyptoil" in low or "egypt oil & gas" in low or "egypt oil and gas" in low:
        return "Egypt Oil & Gas"
    if "pulse" in low or "maeil" in low:
        return "Pulse"
    if "qazinform" in low:
        return "Qazinform"
    if "kazakhstan stock exchange" in low or "қазақстан қор биржасы" in low:
        return "KASE"
    if "astana international exchange" in low:
        return "AIX"
    if "astana international financial centre" in low or "aifc" in low:
        return "AIFC"
    if "saudi press agency" in low:
        return "Saudi Press Agency"
    if "financial services commission" in low:
        return "Korea FSC"
    if "korea exchange" in low:
        return "Korea Exchange"

    # A publisher name supplied by the RSS item is more authoritative than the
    # discovery URL. In particular, Google News article URLs must never turn a
    # real publisher such as "SWFI" into the generic label "News".
    if source and not is_generic_source_name(source):
        return source

    host = urlparse(url or "").netloc.replace("www.", "")
    if host:
        host_name = host.split(":")[0]
        if host_name in GENERIC_SOURCE_HOSTS or host_name.endswith(".news.google.com"):
            return ""
        if host_name in aliases.get("aliases", {}):
            return aliases["aliases"][host_name]
        return host_name.split(".")[0].replace("-", " ").title()
    return "" if is_generic_source_name(source) else source


def best_source_name(raw: str, *, article_url: str = "", homepage_url: str = "") -> str:
    """Choose a publisher label without mistaking an aggregator for the source."""
    for candidate, hint_url in (
        (raw, ""),
        ("", article_url),
        ("", homepage_url),
    ):
        normalized = normalize_source_name(candidate, url=hint_url)
        if normalized and not is_generic_source_name(normalized):
            return normalized
    return "Original Publisher"


def source_homepage(source: str, url: str = "") -> str:
    homepages = load_source_aliases().get("homepages", {})
    canonical = normalize_source_name(source, url=url)
    if canonical in homepages:
        return homepages[canonical]
    if url:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
    return ""
