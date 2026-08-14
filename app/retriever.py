"""新闻抓取模块。

策略：
  - 标准 RSS / Google News RSS：用 feedparser 解析
  - 手动 URL：用 httpx 拿 HTML，BeautifulSoup 抽取正文
  - 正文抓取：用于 formatter 阶段补全细节。入库时只存标题/摘要，
    选中后再按需抓取（省流量，省时间）
"""
from __future__ import annotations

import json
import logging
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .config import load_sources, load_watchlist, settings
from . import source_utils

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HTTP_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}


def _google_news_rss_url(query: str, hl: str = "en-US", gl: str = "US", ceid: str = "US:en") -> str:
    # when:7d 限制过去 7 天
    q = f"{query} when:{settings.lookback_days}d"
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(q)}&hl={hl}&gl={gl}&ceid={quote_plus(ceid)}"
    )


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _is_recent(dt: datetime | None) -> bool:
    if dt is None:
        return True  # 无日期的条目先保留，由 LLM/人工再判断
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.lookback_days)
    return dt >= cutoff


def _strip_html(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text(" ", strip=True)


def _fetch_feed(url: str, max_items: int | None = None) -> list[dict]:
    """封装 feedparser + httpx，避免某些主机对 feedparser 默认 UA 过滤。"""
    try:
        with httpx.Client(timeout=20.0, headers=HTTP_HEADERS, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
    except Exception as exc:
        logger.warning("Feed fetch failed: %s -> %s", url, exc)
        return []
    items: list[dict] = []
    limit = max(1, min(settings.max_per_source, int(max_items or settings.max_per_source)))
    for entry in parsed.entries[:limit]:
        pub = _parse_date(entry)
        if not _is_recent(pub):
            continue
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        google_title, google_source = source_utils.source_from_google_title(title)
        if google_source:
            title = google_title
        feed_source = entry.get("source") or {}
        detected_source = (feed_source.get("title") or google_source or "").strip()
        detected_source_homepage = (feed_source.get("href") or "").strip()
        summary = _strip_html(entry.get("summary", ""))[:1500]
        items.append(
            {
                "title": title,
                "url": link,
                "google_news_url": link if "news.google." in link else "",
                "detected_source": detected_source,
                "detected_source_homepage": detected_source_homepage,
                "published_at": pub.isoformat() if pub else None,
                "summary": summary,
            }
        )
    return items


def _watchlist_queries(country_code: str, src: dict) -> list[str]:
    watchlist = load_watchlist()
    names = watchlist.get(country_code, []) or []
    limit = int(src.get("limit", 40))
    per_company_terms = src.get("terms", "(deal OR investment OR acquisition OR project OR IPO OR bond OR MoU)")
    group_size = max(1, min(8, int(src.get("group_size", 4))))
    selected = [str(name).strip() for name in names[:limit] if len(str(name).strip()) >= 3]
    queries: list[str] = []
    for start in range(0, len(selected), group_size):
        group = selected[start : start + group_size]
        company_terms = " OR ".join(f'"{name}"' for name in group)
        queries.append(f"({company_terms}) {per_company_terms}")
    return queries


def fetch_all(country_codes: Iterable[str] | None = None) -> list[dict]:
    """并发抓取启用的信源，返回候选列表（尚未入库）。"""
    requested = {str(code).upper() for code in country_codes or []}
    tasks: list[dict] = []
    for country in load_sources():
        code = country["code"]
        if requested and code not in requested:
            continue
        for src in country.get("sources", []):
            if src.get("enabled", True) is False:
                continue
            src_type = src.get("type")
            src_name = src.get("name", src_type)
            if src_type == "rss":
                feed_url = src["url"]
                lang = "zh" if "中文" in src_name or "公众号" in src_name else "en"
                feed_urls = [feed_url]
            elif src_type == "google_news":
                hl = src.get("hl", "en-US")
                gl = src.get("gl", "US")
                ceid = src.get("ceid", "US:en")
                feed_urls = [_google_news_rss_url(src["query"], hl=hl, gl=gl, ceid=ceid)]
                lang = "zh" if hl.startswith("zh") else "en"
            elif src_type == "watchlist_search":
                hl = src.get("hl", "en-US")
                gl = src.get("gl", "US")
                ceid = src.get("ceid", "US:en")
                feed_urls = [_google_news_rss_url(q, hl=hl, gl=gl, ceid=ceid) for q in _watchlist_queries(code, src)]
                lang = "zh" if hl.startswith("zh") else "en"
            else:
                logger.info("Skip unsupported source type: %s", src_type)
                continue

            for feed_url in feed_urls:
                tasks.append({
                    "country_code": code,
                    "source": src,
                    "source_name": src_name,
                    "feed_url": feed_url,
                    "lang": lang,
                })

    def _run_task(task: dict) -> list[dict]:
        src = task["source"]
        code = task["country_code"]
        src_name = task["source_name"]
        logger.info("[%s] fetching %s", code, src_name)
        items = _fetch_feed(task["feed_url"], max_items=src.get("max_items"))
        for it in items:
            detected = it.get("detected_source") or src_name
            source_hint_url = it.get("detected_source_homepage") or it.get("url", "")
            canonical_source = source_utils.normalize_source_name(detected, url=source_hint_url)
            it["country_code"] = code
            it["source"] = canonical_source
            it["source_original"] = src_name
            it["source_homepage"] = source_utils.source_homepage(canonical_source, url=source_hint_url)
            it["source_tier"] = int(src.get("tier", 3))
            it["source_category"] = str(src.get("category", "media"))
            it["raw_lang"] = task["lang"]
            it["is_manual"] = False
            it["manual_order"] = None
        return items

    slots: list[list[dict] | None] = [None] * len(tasks)
    workers = min(settings.fetch_concurrency, len(tasks)) if tasks else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_run_task, task): idx for idx, task in enumerate(tasks)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                slots[idx] = future.result()
            except Exception as exc:
                logger.warning("Source worker failed: %s", exc)
                slots[idx] = []

    all_items = [item for batch in slots if batch for item in batch]
    logger.info("Fetched %d raw items across all sources", len(all_items))
    return all_items


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http/https URLs are allowed")
    host = parsed.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Local URLs are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("URL host cannot be resolved") from exc
    if not addresses:
        raise ValueError("URL host cannot be resolved")
    for value in addresses:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
        if not ip.is_global:
            raise ValueError("Private, loopback, and link-local URLs are not allowed")


def fetch_public_response(url: str, timeout: float = 25.0) -> httpx.Response:
    """Fetch a public URL while validating every redirect hop against SSRF."""
    current = url
    with httpx.Client(timeout=timeout, headers=HTTP_HEADERS, follow_redirects=False) as client:
        for _ in range(6):
            _validate_public_url(current)
            response = client.get(current)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                current = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            return response
    raise ValueError("Too many redirects")


def is_google_news_url(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.hostname == "news.google.com" and "/articles/" in parsed.path


@lru_cache(maxsize=512)
def resolve_publisher_url(url: str) -> str:
    """Best-effort conversion of a Google News RSS link to the publisher URL."""
    if not is_google_news_url(url):
        return url
    article_id = urlparse(url).path.rstrip("/").split("/")[-1]
    if not article_id:
        return url
    try:
        with httpx.Client(timeout=15.0, headers=HTTP_HEADERS, follow_redirects=True) as client:
            page = client.get(f"https://news.google.com/articles/{article_id}")
            page.raise_for_status()
            node = BeautifulSoup(page.text, "lxml").select_one("c-wiz > div[jscontroller]")
            if node is None:
                return url
            signature = node.get("data-n-a-sg")
            timestamp = node.get("data-n-a-ts")
            if not signature or not timestamp:
                return url
            rpc_payload = [
                "Fbv4je",
                (
                    '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
                    'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
                    f'"{article_id}",{timestamp},"{signature}"]'
                ),
            ]
            response = client.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                data={"f.req": json.dumps([[rpc_payload]])},
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            )
            response.raise_for_status()
            envelope = json.loads(response.text.split("\n\n", 1)[1])[:-2]
            decoded = json.loads(envelope[0][2])[1]
            if not isinstance(decoded, str) or is_google_news_url(decoded):
                return url
            _validate_public_url(decoded)
            return decoded
    except Exception as exc:
        logger.info("Google News URL decode failed: %s -> %s", url, exc)
        return url


# ---------------- 正文抓取（选中后再调用） ---------------- #

_BODY_SELECTORS = [
    "article",
    "div.article-body",
    "div#article-body",
    "div.entry-content",
    "div.post-content",
    "div.content__article-body",
    "main",
]


def fetch_article_body(url: str, max_chars: int = 6000) -> str:
    """抓取单篇文章正文（尽力而为）。"""
    try:
        html = fetch_public_response(url).text
    except Exception as exc:
        logger.warning("Article fetch failed: %s -> %s", url, exc)
        return ""
    soup = BeautifulSoup(html, "lxml")
    # 清理噪声标签
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    for sel in _BODY_SELECTORS:
        node = soup.select_one(sel)
        if node:
            text = node.get_text(" ", strip=True)
            if len(text) > 200:
                return text[:max_chars]
    # 兜底：所有 <p> 拼接
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    text = " ".join(p for p in paragraphs if p)
    return text[:max_chars]


def fetch_manual_url(country_code: str, url: str, source_label: str = "Manual") -> dict | None:
    """手动添加：抓取 URL 的标题和正文，返回一条候选。"""
    try:
        html = fetch_public_response(url).text
    except Exception as exc:
        logger.warning("Manual URL fetch failed: %s -> %s", url, exc)
        return None

    soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        title = url

    body = fetch_article_body(url)
    host = urlparse(url).netloc.replace("www.", "")
    canonical_source = source_utils.normalize_source_name(source_label, url=url)
    return {
        "country_code": country_code,
        "source": canonical_source,
        "source_original": f"{source_label} ({host})",
        "source_homepage": source_utils.source_homepage(canonical_source, url=url),
        "title": title,
        "url": url,
        "published_at": datetime.utcnow().isoformat(),
        "summary": body[:500],
        "fetched_body": body,
        "raw_lang": "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in title[:30]) else "en",
        "is_manual": True,
        "manual_order": 0,
    }


def search_related_sources(title: str, country_code: str = "", max_results: int = 10) -> list[dict]:
    """Search Google News for alternate sources covering the same event."""
    query = f'"{title[:120]}"'
    if country_code:
        query = f"{query} {country_code}"
    feed_url = _google_news_rss_url(query)
    items = _fetch_feed(feed_url)
    out: list[dict] = []
    seen_urls: set[str] = set()
    for it in items:
        url = it.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        detected = it.get("detected_source") or source_utils.normalize_source_name("", url=url)
        source = source_utils.normalize_source_name(detected, url=url)
        out.append(
            {
                "title": it.get("title", ""),
                "url": url,
                "source": source,
                "source_homepage": source_utils.source_homepage(source, url=url),
                "published_at": it.get("published_at"),
            }
        )
        if len(out) >= max_results:
            break
    priority = {"Reuters": 0, "Bloomberg": 1, "Zawya": 2, "KED Global": 3, "Chosun Biz": 4, "Yahoo Finance": 9}
    return sorted(out, key=lambda x: (priority.get(x.get("source", ""), 5), x.get("source", "")))
