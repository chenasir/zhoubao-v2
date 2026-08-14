"""候选过滤与打分。

分两步：
  1) 规则过滤：黑名单直接丢弃；白/降名单调整基础分
  2) LLM 打分（批量）：评估相关性 0~5，并给一句 reason

LLM 批量打分一次传一组（例如 10 条）给 DeepSeek，显著省 token。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable, Mapping

from rapidfuzz import fuzz

from .config import get_source_order_map, load_keywords
from . import llm

logger = logging.getLogger(__name__)

LLM_SCORE_SYSTEM = """\
You are a news analyst for CICC's Weekly Global Markets News Digest.
Your job: provide a 0-10 auxiliary ranking score for items that already passed
the layered Filtering Strategy pipeline.

Scoring (0-10):
  Hardness (0-5): disclosed terms/structure plus next milestone
  Scale (0-3): amount/capacity/valuation, with larger or more strategic items higher
  Specificity (0-2): concrete numbers and clear parties/regulators/advisers

Rules:
  - Prioritize China/HK-linked GCC items when they have hard hooks
  - Korea must stay deal-only; do not reward sector news without a deal process
  - Kazakhstan should prioritize China-related executable cooperation
  - Facts only; penalize speculation and soft visit/meeting items

Return STRICT JSON:
{"results":[{"id":<int>,"score":<0-10>,"reason":"<short reason>"}]}
No commentary outside JSON.
"""


def _kw_adjust(text: str) -> tuple[float, str]:
    """基于关键词对基础分做加减。"""
    kw = load_keywords()
    low = text.lower()
    score = 2.0
    reasons: list[str] = []
    for w in kw.get("blacklist", []) or []:
        if w.lower() in low:
            return -1, f"blacklist:{w}"  # -1 表示丢弃
    for w in kw.get("prioritize", []) or []:
        if w.lower() in low:
            score += 0.4
            reasons.append(f"+{w}")
    for w in kw.get("deprioritize", []) or []:
        if w.lower() in low:
            score -= 0.6
            reasons.append(f"-{w}")
    score = max(0.0, min(5.0, score))
    return score, ", ".join(reasons[:8])


def dedupe_by_title(rows: list[dict], threshold: int = 88) -> list[dict]:
    """按标题模糊去重。"""
    kept: list[dict] = []
    for r in rows:
        title = r["title"]
        is_dup = False
        for k in kept:
            if fuzz.token_set_ratio(title, k["title"]) >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(r)
    return kept


def rule_prefilter(rows: list[dict]) -> list[dict]:
    """应用关键词规则，返回带有初始 score 的列表，并剔除黑名单。"""
    out: list[dict] = []
    for r in rows:
        base = f"{r.get('title','')} {r.get('summary','')}"
        s, reason = _kw_adjust(base)
        if s < 0:
            continue
        r["score"] = s
        r["score_reason"] = reason
        out.append(r)
    return out


def _published_ts(row: dict) -> float:
    value = row.get("published_at")
    if not value:
        return 0.0
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _shortlist_sort_key(row: dict) -> tuple[float, int, float]:
    source_order = get_source_order_map()
    return (
        -float(row.get("score", 0) or 0),
        source_order.get((row.get("country_code", ""), row.get("source", "")), 999),
        -_published_ts(row),
    )


def shortlist_by_country(
    rows: list[dict],
    per_country_min: int = 10,
    per_country_max: int = 20,
) -> list[dict]:
    """按国家压缩候选量，优先高相关，同时尽量覆盖不同信源。"""
    by_country: dict[str, list[dict]] = {}
    for row in rows:
        by_country.setdefault(row.get("country_code", ""), []).append(row)

    final_rows: list[dict] = []
    for country_code, items in by_country.items():
        if len(items) <= per_country_max:
            final_rows.extend(sorted(items, key=_shortlist_sort_key))
            continue

        ranked = sorted(items, key=_shortlist_sort_key)
        source_best: list[dict] = []
        source_seen: set[str] = set()
        for item in ranked:
            source = item.get("source", "")
            if source in source_seen:
                continue
            source_best.append(item)
            source_seen.add(source)

        picked: list[dict] = []
        picked_ids: set[str] = set()

        # 第一轮：尽量每个信源先保留一条，最多保留到下限或上限
        target_first_round = min(per_country_max, max(per_country_min, min(len(source_best), per_country_max)))
        for item in source_best:
            item_id = item.get("url") or str(id(item))
            if item_id in picked_ids:
                continue
            picked.append(item)
            picked_ids.add(item_id)
            if len(picked) >= target_first_round:
                break

        # 第二轮：再按综合相关性补满到上限
        if len(picked) < per_country_max:
            for item in ranked:
                item_id = item.get("url") or str(id(item))
                if item_id in picked_ids:
                    continue
                picked.append(item)
                picked_ids.add(item_id)
                if len(picked) >= per_country_max:
                    break

        logger.info(
            "Shortlisted %s: %d -> %d candidates",
            country_code,
            len(items),
            len(picked),
        )
        final_rows.extend(picked)

    return final_rows


# ---------- LLM 批量打分 ---------- #

BATCH_SIZE = 10


def _score_batch(
    batch: list[dict],
    runtime_config: Mapping[str, Any] | None = None,
) -> list[dict]:
    user_payload = [
        {
            "id": idx,
            "country": r["country_code"],
            "title": r["title"],
            "summary": (r.get("summary") or "")[:400],
            "status": r.get("status", ""),
            "route_country": r.get("route_country", r.get("country_code", "")),
            "watchlist_hit": r.get("watchlist_hit", False),
            "topic_cluster": r.get("topic_cluster", ""),
        }
        for idx, r in enumerate(batch)
    ]
    user = json.dumps({"items": user_payload}, ensure_ascii=False)
    try:
        data = llm.chat_json(
            LLM_SCORE_SYSTEM,
            user,
            temperature=0.1,
            runtime_config=runtime_config,
        )
        results = {item["id"]: item for item in data.get("results", [])}
    except Exception as exc:
        logger.warning("LLM scoring batch failed: %s", exc)
        results = {}
    for idx, r in enumerate(batch):
        res = results.get(idx)
        if res:
            try:
                r["score"] = max(0.0, min(10.0, float(res.get("score", r["score"]))))
                r["score_reason"] = (r.get("score_reason", "") + " | " + str(res.get("reason", ""))).strip(" |")
            except Exception:
                pass
    return batch


def llm_score(
    rows: list[dict],
    runtime_config: Mapping[str, Any] | None = None,
) -> list[dict]:
    """对输入列表做 LLM 打分，就地更新 score / score_reason。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .config import resolve_llm_concurrency

    if not rows:
        return []

    batches = [rows[i : i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    workers = min(resolve_llm_concurrency(runtime_config), len(batches))
    if workers <= 1:
        updated: list[dict] = []
        for batch in batches:
            updated.extend(_score_batch(batch, runtime_config=runtime_config))
        return updated

    ordered: list[list[dict] | None] = [None] * len(batches)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_score_batch, batch, runtime_config): idx
            for idx, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                ordered[idx] = future.result()
            except Exception as exc:
                logger.warning("LLM scoring batch worker failed: %s", exc)
                ordered[idx] = batches[idx]

    updated = []
    for batch in ordered:
        if batch:
            updated.extend(batch)
    return updated
