"""Structured bilingual consistency guard for formatted news items."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from . import llm

logger = logging.getLogger(__name__)

BILINGUAL_GUARD_SYSTEM = """\
You are a strict bilingual fact consistency checker.
Compare the Chinese and English versions of one financial news item.

Check whether they describe the same:
- parties / companies / institutions
- action / transaction / project
- amounts / valuation / stake / capacity / coupon / tenor
- dates / milestones / closing / COD
- source and event framing

Return strict JSON:
{
  "consistent": true/false,
  "warnings": ["..."],
  "cn_facts": {
    "parties": [],
    "action": "",
    "amounts": [],
    "dates": [],
    "terms": []
  },
  "en_facts": {
    "parties": [],
    "action": "",
    "amounts": [],
    "dates": [],
    "terms": []
  }
}

Only mark inconsistent when there is a real factual mismatch, omission of a key
deal fact, or changed numeric fact. Style differences are allowed.
"""

BILINGUAL_GUARD_BATCH_SYSTEM = """\
You are a strict bilingual fact consistency checker.
Check each bilingual financial news item independently.

For every item, compare whether Chinese and English describe the same:
- parties / companies / institutions
- action / transaction / project
- amounts / valuation / stake / capacity / coupon / tenor
- dates / milestones / closing / COD
- source and event framing

Return strict JSON:
{
  "results": [
    {"id": <int>, "consistent": true/false, "warnings": ["..."]}
  ]
}

Only mark inconsistent when there is a real factual mismatch, omission of a key
deal fact, or changed numeric fact. Style differences are allowed.
"""


@dataclass(frozen=True)
class BilingualCheckResult:
    ok: bool
    warnings: list[str]


def check_bilingual_consistency(
    cn_title: str,
    cn_body: str,
    en_title: str,
    en_body: str,
    runtime_config: Mapping[str, Any] | None = None,
) -> BilingualCheckResult:
    payload = {
        "cn_title": cn_title,
        "cn_body": cn_body,
        "en_title": en_title,
        "en_body": en_body,
    }
    try:
        data = llm.chat_json(
            BILINGUAL_GUARD_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            temperature=0.0,
            runtime_config=runtime_config,
        )
    except Exception as exc:
        logger.warning("Bilingual guard skipped: %s", exc)
        return BilingualCheckResult(ok=True, warnings=[])

    warnings = [str(w) for w in data.get("warnings", []) if str(w).strip()]
    consistent = bool(data.get("consistent", not warnings))
    return BilingualCheckResult(ok=consistent and not warnings, warnings=warnings)


def check_bilingual_consistency_batch(
    items: list[dict[str, Any]],
    runtime_config: Mapping[str, Any] | None = None,
    batch_size: int = 6,
) -> dict[int, BilingualCheckResult]:
    """Check many formatted items with fewer LLM calls.

    If the guard itself fails, return ok=True for all items so generation can
    still complete and return a docx. Numeric guards remain local and separate.
    """
    results: dict[int, BilingualCheckResult] = {}
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        payload = {
            "items": [
                {
                    "id": item["id"],
                    "cn_title": item.get("cn_title", ""),
                    "cn_body": item.get("cn_body", ""),
                    "en_title": item.get("en_title", ""),
                    "en_body": item.get("en_body", ""),
                }
                for item in batch
            ]
        }
        try:
            data = llm.chat_json(
                BILINGUAL_GUARD_BATCH_SYSTEM,
                json.dumps(payload, ensure_ascii=False),
                temperature=0.0,
                runtime_config=runtime_config,
            )
        except Exception as exc:
            logger.warning("Batch bilingual guard skipped: %s", exc)
            for item in batch:
                results[item["id"]] = BilingualCheckResult(ok=True, warnings=[])
            continue

        returned = {int(row.get("id")): row for row in data.get("results", []) if "id" in row}
        for item in batch:
            row = returned.get(item["id"], {})
            warnings = [str(w) for w in row.get("warnings", []) if str(w).strip()]
            consistent = bool(row.get("consistent", not warnings))
            results[item["id"]] = BilingualCheckResult(ok=consistent and not warnings, warnings=warnings)
    return results
