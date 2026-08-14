"""对选中的候选项调用 LLM，生成中英文双语新闻条目。

流程：先写英文稿（原文为英文时以英文为准），再忠实翻译中文；来源日期由系统解析，不由模型猜测。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from . import article_fetch, fact_guard, llm, source_utils
from .article_meta import is_suspicious_feed_date, resolve_publication_date
from .models import FormattedItem

logger = logging.getLogger(__name__)

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

SOURCE_CITATION_RE = re.compile(
    r"\s*(?:\((?:Source|来源)\s*[:：][^)）]*[)）]|（(?:Source|来源)\s*[:：][^)）]*[)）])\s*$",
    re.IGNORECASE,
)

CN_TEXT_FIXES = {
    "马吉德·阿尔-霍盖勒": "马吉德·阿尔·霍盖勒",
    "马吉德·阿尔霍盖勒": "马吉德·阿尔·霍盖勒",
    "马吉德·阿尔－霍盖勒": "马吉德·阿尔·霍盖勒",
    "中安一航局（CACC）": "中国民航机场建设集团（CACC）",
    "中安一航局": "中国民航机场建设集团",
}

PLACEHOLDER_PATTERNS = (
    "本周无交易",
    "无交易或政策新闻",
    "no transaction or policy news",
    "no deal or policy news",
)


def _is_placeholder_item(item: FormattedItem) -> bool:
    blob = " ".join([item.cn_title, item.cn_body, item.en_title, item.en_body]).lower()
    return any(pattern.lower() in blob for pattern in PLACEHOLDER_PATTERNS)

EN_FORMAT_SYSTEM_PROMPT = """\
You are a senior English financial news editor at CICC, producing the Weekly Global Markets News Digest.

Write ONE English news item from the raw article. The Chinese version will be translated from your English later,
so English is the authoritative version.

[Content focus]
- Deal/policy news only. Skip pure macro, routine refinancing, politics, elections.
- State China/HK linkage explicitly when relevant.

[Title]
- One complete sentence, subject-verb-object, sentence case, factual.
- Add nationality + industry before unfamiliar companies.

[Body]
- 2-4 sentences, past tense, no opinion.
- Lead with date and subject: "On June 25, ..."
- Never write placeholder filler such as "no transaction or policy news this week"
  or "本周无交易或政策新闻". Always summarize the concrete facts available in the source.
- Preserve all numbers, amounts, dates, parties, deal terms exactly.
- Currency (English): ISO code + amount + million/billion; non-USD with USD conversion in parentheses.
- Use thousand separators.
- Do NOT include any source citation line in title or body.

Return strict JSON:
{
  "facts": {
    "parties": [],
    "action": "...",
    "amounts": [],
    "dates": [],
    "terms": [],
    "china_linkage": "..."
  },
  "en_title": "...",
  "en_body": "..."
}
"""

CN_TRANSLATE_SYSTEM_PROMPT = """\
You are a senior Chinese financial news editor at CICC.

Translate the given English weekly digest item into Chinese. The English version is authoritative.
Do NOT add, omit, or change any fact. Chinese must match English on parties, action, amounts, dates, and deal terms.

[Style]
- Professional, concise Chinese punctuation（，。；：、「」（））.
- Chinese title: complete sentence, avoid 的/了/着 where possible.
- Chinese body: 2-4 sentences; lead with date like "6月25日，……"
- Currency (Chinese): 中文单位；非美元币种括号内写“合约XXX万美元”.
- Use official Chinese names for Chinese companies; do not invent abbreviations.
- Do NOT include any source citation line in title or body.

Return strict JSON:
{
  "cn_title": "...",
  "cn_body": "..."
}
"""

FEW_SHOT = """\
Reference example (format only):

EN title: Saudi steel maker Al Yamamah Steel signed a USD 33.59 million contract with China Power Construction subsidiary SEPCO III to supply wind towers for the Yanbu wind farm
EN body: On April 15, Saudi steel maker Al Yamamah Steel Industries Co. said it has signed a nine-month contract with EPC contractor SEPCO III (Shandong Electric Power Construction Corporation No. 3), a subsidiary of China Electric Power Construction Corporation, to supply steel wind towers for the Yanbu wind farm in Madinah Province. The contract is valued at SAR 126 million (USD 33.59 million), and the company said deliveries will start this month, with the financial impact to be reflected in its Q2 2026 results.

CN title: 沙特钢铁制造商 Al Yamamah Steel 与中国电建子公司 SEPCO III 签署约 3,359 万美元合同，为沙特延布风电场供应风电塔筒
CN body: 4月15日，沙特钢铁制造商 Al Yamamah Steel Industries Co. 表示，已与中国电力建设集团有限公司子公司、EPC 承包商 SEPCO III（山东电力建设第三工程有限公司）签署合同，为沙特麦地那省延布风电场供应钢制风电塔筒。该合同期限为 9 个月，合同金额为 1.26 亿沙特里亚尔（合约 3,359 万美元），公司称将于本月开始交付，财务影响将体现在 2026 年第二季度业绩中。
"""


def _strip_source_citation(text: str) -> str:
    return SOURCE_CITATION_RE.sub("", (text or "").strip()).strip()


def _source_labels(source: str, published: date | None) -> tuple[str, str]:
    if published:
        en_date = f"{MONTHS[published.month - 1]} {published.day}, {published.year}"
        zh_date = f"{published.year}年{published.month}月{published.day}日"
        return f"(Source: {source}, {en_date})", f"（来源：{source}，{zh_date}）"
    return f"(Source: {source})", f"（来源：{source}）"


def _postprocess_cn(text: str) -> str:
    out = text or ""
    for old, new in CN_TEXT_FIXES.items():
        out = out.replace(old, new)
    out = out.replace("（约合", "（合约").replace("(约合", "(合约")
    return out


def _parse_published_date(value: str | None) -> date | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _resolve_row_publication_date(row: Mapping[str, Any], html: str, body: str) -> date | None:
    return resolve_publication_date(
        feed_published_at=row.get("published_at"),
        html=html,
        text=f"{row.get('title', '')}\n{body or row.get('summary', '')}",
    )


def _postprocess_item(
    item: FormattedItem,
    *,
    source: str,
    published: date | None,
) -> FormattedItem:
    item.cn_title = _postprocess_cn(_strip_source_citation(item.cn_title))
    item.cn_body = _postprocess_cn(_strip_source_citation(item.cn_body))
    item.en_title = _strip_source_citation(item.en_title)
    item.en_body = _strip_source_citation(item.en_body)
    item.source_label_en, item.source_label_zh = _source_labels(source, published)
    return item


def _generate_english(
    row: Mapping[str, Any],
    source_text: str,
    runtime_config: Mapping[str, Any] | None,
    retry_note: str = "",
) -> dict[str, Any]:
    user = {
        "country": row["country_code"],
        "source": row["source"],
        "url": row["url"],
        "resolved_publication_date": row.get("resolved_publication_date"),
        "raw_title": row["title"],
        "raw_body": source_text[:5000],
        "must_preserve_usd_amounts_in_usd_million": fact_guard.extract_usd_millions(source_text),
    }
    user_content = FEW_SHOT + retry_note + "\n\nInput item JSON:\n" + json.dumps(user, ensure_ascii=False)
    return llm.chat_json(
        EN_FORMAT_SYSTEM_PROMPT,
        user_content,
        temperature=0.05 if not retry_note else 0.0,
        runtime_config=runtime_config,
    )


def _translate_chinese(
    en_title: str,
    en_body: str,
    facts: Mapping[str, Any],
    runtime_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = {"facts": facts, "en_title": en_title, "en_body": en_body}
    return llm.chat_json(
        CN_TRANSLATE_SYSTEM_PROMPT,
        json.dumps(payload, ensure_ascii=False),
        temperature=0.0,
        runtime_config=runtime_config,
    )


def format_one(
    row: dict,
    runtime_config: Mapping[str, Any] | None = None,
) -> FormattedItem | None:
    """把一条 candidate 转成 FormattedItem。先英文，后中文翻译。"""
    html = ""
    body = row.get("fetched_body") or ""
    suspicious_date = is_suspicious_feed_date(row.get("published_at"))
    if row.get("url") and (len(body) < 300 or suspicious_date):
        article = article_fetch.fetch_article(row["url"])
        body = article.body or body or row.get("summary", "")
        html = article.html
        if body:
            row["fetched_body"] = body
        if article.published_at:
            row["article_published_at"] = article.published_at

    source_text = f"{row.get('title', '')}\n{body or row.get('summary', '')}"
    published = _parse_published_date(row.get("article_published_at"))
    if not published:
        published = _resolve_row_publication_date(row, html, body)
    row["resolved_publication_date"] = published.isoformat() if published else None

    source = source_utils.normalize_source_name(row.get("source") or "", url=row.get("url") or "")
    if not source:
        source = row.get("source") or "Source"

    en_data = _generate_english(row, source_text, runtime_config)
    en_title = str(en_data.get("en_title", "")).strip()
    en_body = str(en_data.get("en_body", "")).strip()
    facts = en_data.get("facts") or {}
    cn_data = _translate_chinese(en_title, en_body, facts, runtime_config)
    item = FormattedItem(
        country_code=row["country_code"],
        cn_title=str(cn_data.get("cn_title", "")).strip(),
        cn_body=str(cn_data.get("cn_body", "")).strip(),
        en_title=en_title,
        en_body=en_body,
        source_label_en="",
        source_label_zh="",
        url=row["url"],
        source_name=row.get("source", ""),
        source_url=row.get("url") or row.get("source_homepage", ""),
    )
    item = _postprocess_item(item, source=source, published=published)
    if _is_placeholder_item(item):
        logger.warning("Skip placeholder formatted item for %s", row.get("url"))
        return None

    generated_text = "\n".join([item.cn_title, item.cn_body, item.en_title, item.en_body])
    fact_check = fact_guard.check_formatted_facts(source_text, generated_text)
    item.fact_check_warnings = list(fact_check.warnings)
    if item.fact_check_warnings:
        logger.warning("Formatting guard warning for %s: %s", row.get("url"), "; ".join(item.fact_check_warnings))
    return item


def format_many(
    rows: Iterable[dict],
    runtime_config: Mapping[str, Any] | None = None,
) -> list[FormattedItem | None]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .config import resolve_llm_concurrency

    row_list = [dict(r) for r in rows]
    if not row_list:
        return []

    workers = min(resolve_llm_concurrency(runtime_config), len(row_list))
    if workers <= 1:
        out: list[FormattedItem | None] = []
        for row in row_list:
            try:
                out.append(format_one(row, runtime_config=runtime_config))
            except Exception as exc:
                logger.exception("format_one failed for id=%s: %s", row.get("id"), exc)
                out.append(None)
        return out

    slots: list[FormattedItem | None] = [None] * len(row_list)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(format_one, row, runtime_config): idx
            for idx, row in enumerate(row_list)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                slots[idx] = future.result()
            except Exception as exc:
                logger.exception("format_one failed for id=%s: %s", row_list[idx].get("id"), exc)
                slots[idx] = None

    return slots
