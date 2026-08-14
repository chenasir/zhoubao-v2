"""Batch translation for candidate titles."""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from . import llm

logger = logging.getLogger(__name__)

TITLE_TRANSLATE_SYSTEM = """\
You translate financial news headlines into concise professional Chinese.
Rules:
- Preserve all company names, numbers, currencies, percentages, dates, and tickers exactly.
- Do not add facts that are not in the headline.
- Return strict JSON: {"results":[{"id":<int>,"title_zh":"..."}]}
"""


def translate_candidate_titles(
    rows: list[dict[str, Any]],
    runtime_config: Mapping[str, Any] | None = None,
    limit: int = 240,
) -> list[dict[str, Any]]:
    targets = [
        (idx, row)
        for idx, row in enumerate(rows)
        if not row.get("title_zh") and row.get("raw_lang", "en") != "zh"
    ][:limit]
    if not targets:
        return rows

    payload = {
        "items": [
            {"id": idx, "title": row.get("title", ""), "source": row.get("source", "")}
            for idx, row in targets
        ]
    }
    try:
        data = llm.chat_json(
            TITLE_TRANSLATE_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            temperature=0.0,
            runtime_config=runtime_config,
        )
    except Exception as exc:
        logger.warning("Title translation skipped: %s", exc)
        return rows

    result_map = {int(item.get("id")): item.get("title_zh", "") for item in data.get("results", []) if "id" in item}
    for idx, row in targets:
        title_zh = str(result_map.get(idx, "")).strip()
        if title_zh:
            row["title_zh"] = title_zh
    return rows
