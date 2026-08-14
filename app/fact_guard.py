"""Lightweight fact guards for LLM-formatted news items.

These checks are intentionally conservative: they flag obvious magnitude
changes without trying to become a full financial data parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


USD_EN_RE = re.compile(
    r"(?:USD|US\$|\$)\s*([\d,.]+)\s*(million|billion|mn|bn)?",
    re.I,
)
USD_ZH_RE = re.compile(
    r"([\d,.]+)\s*(万|亿)?\s*美元",
    re.I,
)
PERCENT_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*%")
SCALE_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(GW|GWh|MW|MWh|km|公里|兆瓦|吉瓦)", re.I)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
COUNT_RE = re.compile(r"(?<!\d)(\d{2,}(?:,\d{3})*)\s*(units|homes|shares|套|户|股)", re.I)


@dataclass(frozen=True)
class FactCheckResult:
    ok: bool
    warnings: list[str]


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def extract_usd_millions(text: str) -> list[float]:
    """Extract USD amounts and normalize them to USD million."""
    values: list[float] = []
    for match in USD_EN_RE.finditer(text or ""):
        amount = _to_float(match.group(1))
        if amount is None:
            continue
        unit = (match.group(2) or "").lower()
        if unit in ("billion", "bn"):
            amount *= 1000
        values.append(amount)

    for match in USD_ZH_RE.finditer(text or ""):
        amount = _to_float(match.group(1))
        if amount is None:
            continue
        unit = match.group(2) or ""
        if unit == "亿":
            amount *= 100
        elif unit == "万":
            amount /= 100
        values.append(amount)
    return values


def _extract_pairs(pattern: re.Pattern[str], text: str) -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2).lower()) for m in pattern.finditer(text or "")}


def _close_to_any(value: float, candidates: list[float], tolerance: float = 0.08) -> bool:
    if not candidates:
        return True
    for candidate in candidates:
        if candidate == 0:
            continue
        if abs(value - candidate) / abs(candidate) <= tolerance:
            return True
    return False


def check_formatted_facts(source_text: str, generated_text: str) -> FactCheckResult:
    """Return warnings for obvious numeric drift between source and generated text."""
    warnings: list[str] = []

    source_usd = extract_usd_millions(source_text)
    generated_usd = extract_usd_millions(generated_text)
    if source_usd and generated_usd:
        for value in generated_usd:
            if not _close_to_any(value, source_usd):
                warnings.append(
                    f"USD amount changed: generated USD {value:g}m not found in source {source_usd}"
                )

    source_percents = {m.group(1) for m in PERCENT_RE.finditer(source_text or "")}
    generated_percents = {m.group(1) for m in PERCENT_RE.finditer(generated_text or "")}
    extra_percents = generated_percents - source_percents
    if source_percents and extra_percents:
        warnings.append(f"percentage changed or added: {sorted(extra_percents)}")

    source_scales = _extract_pairs(SCALE_RE, source_text)
    generated_scales = _extract_pairs(SCALE_RE, generated_text)
    extra_scales = generated_scales - source_scales
    if source_scales and extra_scales:
        warnings.append(f"capacity/scale changed or added: {sorted(extra_scales)}")

    source_years = {m.group(1) for m in YEAR_RE.finditer(source_text or "")}
    generated_years = {m.group(1) for m in YEAR_RE.finditer(generated_text or "")}
    extra_years = generated_years - source_years
    if source_years and extra_years:
        warnings.append(f"year changed or added: {sorted(extra_years)}")

    source_counts = _extract_pairs(COUNT_RE, source_text)
    generated_counts = _extract_pairs(COUNT_RE, generated_text)
    extra_counts = generated_counts - source_counts
    if source_counts and extra_counts:
        warnings.append(f"count changed or added: {sorted(extra_counts)}")

    return FactCheckResult(ok=not warnings, warnings=warnings)
