"""Layered screening pipeline for the Filtering Strategy requirement.

The pipeline keeps the old candidate shape usable by the UI while adding
auditable fields for HOLD / ROUTE / PASS / FINAL / RESERVE decisions.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz

from .config import load_filtering_rules, load_watchlist

COUNTRIES = ("KSA", "UAE", "KZ", "KR")

MONEY_RE = re.compile(
    r"(\b(?:USD|EUR|GBP|SAR|AED|KZT|KRW|CNY|RMB)\s*[\d,.]+|\$[\d,.]+|[\d,.]+\s*(?:million|billion|mn|bn|万|亿|兆))",
    re.I,
)
NUMBER_RE = re.compile(r"\d")
SPECULATION_RE = re.compile(r"\b(may|might|could|reportedly|rumor|rumour|speculation|potential|考虑|可能|据称|传闻)\b", re.I)


def run_filtering_pipeline(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Run all four strategy layers and return UI-ready candidates plus stats."""
    rules = load_filtering_rules()
    watchlist = load_watchlist()
    prepared = [_prepare_row(row, idx) for idx, row in enumerate(rows)]

    layer1_kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    hold: list[dict[str, Any]] = []

    for row in prepared:
        _tag_watchlist(row, watchlist)
        _apply_layer1(row, rules)
        if row["status"].startswith("DROP"):
            dropped.append(row)
        elif row["status"].startswith("HOLD"):
            hold.append(row)
            layer1_kept.append(row)
        else:
            layer1_kept.append(row)

    for row in layer1_kept:
        _apply_layer2(row, rules)

    pass_rows: list[dict[str, Any]] = []
    for row in layer1_kept:
        if row["status"].startswith("HOLD"):
            continue
        _apply_layer3(row, rules)
        if row["status"] == "PASS":
            pass_rows.append(row)
        elif row["status"].startswith("HOLD"):
            hold.append(row)
        else:
            dropped.append(row)

    final_rows, reserve_rows = _apply_layer4(pass_rows, rules)
    visible = final_rows + reserve_rows + _cap_hold_rows(hold, len(prepared), rules)
    visible = _assign_display_fields(visible)

    return {
        "candidates": visible,
        "final": final_rows,
        "reserve": reserve_rows,
        "hold": hold,
        "dropped": dropped,
        "stats": {
            "after_layer1": len(layer1_kept),
            "passed_gates": len(pass_rows),
            "final": len(final_rows),
            "reserve": len(reserve_rows),
            "hold": len(hold),
            "dropped": len(dropped),
        },
    }


def _prepare_row(row: dict[str, Any], idx: int) -> dict[str, Any]:
    item = deepcopy(row)
    item.setdefault("id", item.get("url") or idx)
    item.setdefault("summary", "")
    item.setdefault("title_zh", "")
    item.setdefault("source_original", "")
    item.setdefault("source_homepage", "")
    item.setdefault("source_tier", 3)
    item.setdefault("source_category", "media")
    item.setdefault("google_news_url", "")
    item.setdefault("score", 0.0)
    item.setdefault("score_reason", "")
    item.setdefault("selected", False)
    item.setdefault("is_manual", False)
    item.setdefault("manual_order", None)
    item["original_country_code"] = item.get("country_code", "")
    item["route_country"] = item.get("country_code", "")
    item["status"] = "NEW"
    item["gate_result"] = ""
    item["final_bucket"] = ""
    item["reserve_reason"] = ""
    item["topic_cluster"] = ""
    item["watchlist_hit"] = False
    item["watchlist_country"] = ""
    item["watchlist_company"] = ""
    item["china_hk_linkage"] = False
    item["hardness_score"] = 0.0
    item["scale_score"] = 0.0
    item["specificity_score"] = 0.0
    item["layer_reasons"] = []
    return item


def _text(row: dict[str, Any]) -> str:
    return f"{row.get('title', '')} {row.get('summary', '')} {row.get('fetched_body', '')[:1200]}"


def _contains(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return any(term and term.lower() in low for term in terms)


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [term for term in terms if term and term.lower() in low]


def _has_number(text: str) -> bool:
    return bool(NUMBER_RE.search(text))


def _has_money_or_scale(text: str) -> bool:
    return bool(MONEY_RE.search(text)) or bool(re.search(r"\b\d[\d,.]*\s*(?:MW|GW|GWh|km|hectares?|%)\b", text, re.I))


def _hard_flags(text: str, rules: dict[str, Any]) -> dict[str, bool]:
    deal = _contains(text, rules.get("deal_terms", []))
    project = _contains(text, rules.get("project_terms", []))
    treaty = _contains(text, rules.get("treaty_terms", []))
    next_step = _contains(text, rules.get("next_milestone_terms", []))
    number = _has_number(text)
    return {
        "number": number,
        "terms": deal or project or treaty,
        "next_milestone": next_step,
        "deal": deal,
        "project": project,
        "treaty": treaty,
    }


def _big_hard_count(text: str, rules: dict[str, Any]) -> int:
    mechanism_terms = [
        "platform",
        "financing structure",
        "underwriting",
        "framework",
        "term framework",
        "机制",
        "平台",
        "融资结构",
        "承销",
        "框架",
    ]
    flags = [
        _contains(text, rules.get("treaty_terms", [])) or "signed" in text.lower() or "签署" in text,
        _has_money_or_scale(text),
        _contains(text, mechanism_terms),
        _contains(text, rules.get("next_milestone_terms", [])),
    ]
    return sum(1 for flag in flags if flag)


def _tag_watchlist(row: dict[str, Any], watchlist: dict[str, list[str]]) -> None:
    text = _text(row).lower()
    for country in COUNTRIES:
        for company in watchlist.get(country, []) or []:
            name = str(company).strip()
            if len(name) < 3:
                continue
            if name.lower() in text:
                row["watchlist_hit"] = True
                row["watchlist_country"] = country
                row["watchlist_company"] = name
                row["layer_reasons"].append(f"watchlist:{country}:{name}")
                return


def _apply_layer1(row: dict[str, Any], rules: dict[str, Any]) -> None:
    text = _text(row)
    flags = _hard_flags(text, rules)
    row["china_hk_linkage"] = _contains(text, rules.get("china_hk_terms", []))

    if not row.get("published_at") or not row.get("source"):
        row["status"] = "HOLD_missing_meta"
        row["layer_reasons"].append("L1:missing date/source")
        return

    is_macro = _contains(text, rules.get("macro_terms", []))
    executable_hook = flags["terms"] and (flags["number"] or flags["next_milestone"])
    if is_macro and not executable_hook:
        row["status"] = "DROP_macro"
        row["layer_reasons"].append("L1:macro without executable hook")
        return

    is_meeting = _contains(text, rules.get("meeting_terms", []))
    if is_meeting and _big_hard_count(text, rules) < 2:
        row["status"] = "DROP_meeting"
        row["layer_reasons"].append("L1:visit/meeting without >=2 hard elements")
        return

    if row.get("watchlist_hit") and not (flags["number"] or flags["terms"] or flags["next_milestone"]):
        row["status"] = "HOLD_coverage_company"
        row["layer_reasons"].append("L1:watchlist/coverage company needs review")
        return

    if not (flags["number"] or flags["terms"] or flags["next_milestone"]):
        row["status"] = "DROP_no_hard_hooks"
        row["layer_reasons"].append("L1:no number/terms/next milestone")
        return

    if flags["terms"] and not flags["number"]:
        row["status"] = "HOLD_missing_terms"
        row["layer_reasons"].append("L1:deal/project/treaty missing key numbers")
        return

    row["status"] = "L1_KEEP"
    row["layer_reasons"].append("L1:keep")


def _apply_layer2(row: dict[str, Any], rules: dict[str, Any]) -> None:
    text = _text(row)
    if not row.get("published_at") or not row.get("source"):
        row["route_country"] = "UNKNOWN"
        row["layer_reasons"].append("L2:missing metadata route unknown")
        return

    anchors = rules.get("country_anchors", {}) or {}
    flags = _hard_flags(text, rules)
    anchor_counts = {
        country: len(_matched_terms(text, terms or []))
        for country, terms in anchors.items()
        if country in COUNTRIES
    }

    route = ""
    if flags["project"] or flags["deal"]:
        route = _best_country(anchor_counts)
        if route:
            row["layer_reasons"].append(f"L2:project/deal jurisdiction {route}")

    if not route and _contains(text, ["listing", "IPO", "bond", "sukuk", "regulator", "approval", "blocked", "上市", "发债", "监管", "批准"]):
        route = _best_country(anchor_counts)
        if route:
            row["layer_reasons"].append(f"L2:venue/regulator jurisdiction {route}")

    if not route:
        route = _best_country(anchor_counts)
        if route:
            row["layer_reasons"].append(f"L2:country anchor {route}")

    if not route and row.get("watchlist_hit"):
        route = row.get("watchlist_country") or ""
        row["layer_reasons"].append(f"L2:watchlist tie-breaker {route}")

    if not route:
        route = "UNKNOWN"
        row["layer_reasons"].append("L2:route unknown")

    row["route_country"] = route


def _best_country(counts: dict[str, int]) -> str:
    positive = [(country, count) for country, count in counts.items() if count > 0]
    if not positive:
        return ""
    positive.sort(key=lambda x: (-x[1], COUNTRIES.index(x[0]) if x[0] in COUNTRIES else 999))
    if len(positive) > 1 and positive[0][1] == positive[1][1]:
        return ""
    return positive[0][0]


def _apply_layer3(row: dict[str, Any], rules: dict[str, Any]) -> None:
    text = _text(row)
    route = row.get("route_country")
    flags = _hard_flags(text, rules)
    has_hard = flags["number"] or flags["terms"] or flags["next_milestone"]
    if not has_hard:
        row["status"] = "DROP_no_hard_hooks"
        row["gate_result"] = "DROP"
        row["layer_reasons"].append("L3:no hard hooks")
        return

    if row.get("watchlist_hit"):
        soft_item = _contains(text, rules.get("macro_terms", [])) or _contains(text, rules.get("meeting_terms", []))
        if soft_item and _big_hard_count(text, rules) < 2:
            row["status"] = "DROP_watchlist_soft_item"
            row["gate_result"] = "DROP"
            row["layer_reasons"].append("L3:watchlist hit but soft macro/meeting")
            return
        row["status"] = "PASS"
        row["gate_result"] = "PASS_watchlist"
        row["layer_reasons"].append("L3:watchlist with hard hook")
        return

    if route in ("KSA", "UAE"):
        hard_deal = (flags["deal"] or flags["project"]) and (flags["number"] or flags["next_milestone"])
        if row.get("china_hk_linkage") and has_hard:
            row["status"] = "PASS"
            row["gate_result"] = "PASS_GCC_china_hk"
        elif hard_deal:
            row["status"] = "PASS"
            row["gate_result"] = "PASS_GCC_hard_deal"
        else:
            row["status"] = "DROP_GCC_gate"
            row["gate_result"] = "DROP"
        row["layer_reasons"].append(f"L3:{row['gate_result']}")
        return

    if route == "KR":
        deal_action = _contains(
            text,
            [
                "LOI",
                "teaser",
                "bid",
                "shortlist",
                "pre-approval",
                "SPA",
                "signing",
                "regulatory block",
                "closing date",
                "acquisition",
                "merger",
                "stake",
                "IPO",
                "투자",
                "인수",
                "收购",
                "并购",
                "竞标",
                "交割",
            ],
        )
        hard_term = flags["number"] and (flags["deal"] or _has_money_or_scale(text))
        if deal_action and hard_term and not SPECULATION_RE.search(text):
            row["status"] = "PASS"
            row["gate_result"] = "PASS_KR_deal_only"
        else:
            row["status"] = "DROP_KR_gate"
            row["gate_result"] = "DROP"
        row["layer_reasons"].append(f"L3:{row['gate_result']}")
        return

    if route == "KZ":
        executable = flags["treaty"] or flags["project"] or flags["deal"] or _contains(
            text,
            ["platform", "corridor", "railway", "logistics", "trade", "Panda bond", "RMB", "走廊", "铁路", "物流", "贸易", "平台", "熊猫债"],
        )
        if row.get("china_hk_linkage") and executable and has_hard:
            row["status"] = "PASS"
            row["gate_result"] = "PASS_KZ_china_first"
        elif _contains(text, rules.get("meeting_terms", [])) and _big_hard_count(text, rules) >= 2:
            row["status"] = "PASS"
            row["gate_result"] = "PASS_KZ_big_diplomatic"
        else:
            row["status"] = "DROP_KZ_gate"
            row["gate_result"] = "DROP"
        row["layer_reasons"].append(f"L3:{row['gate_result']}")
        return

    if flags["terms"] and not flags["number"]:
        row["status"] = "HOLD_missing_terms"
        row["gate_result"] = "HOLD"
        row["layer_reasons"].append("L3:borderline missing terms")
        return

    row["status"] = "DROP_route_unknown"
    row["gate_result"] = "DROP"
    row["layer_reasons"].append("L3:route unknown")


def _apply_layer4(rows: list[dict[str, Any]], rules: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deduped = _dedupe_events(rows)
    for row in deduped:
        _score_row(row, rules)
        row["topic_cluster"] = _topic_cluster(_text(row))

    by_country: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in deduped:
        country = row.get("route_country")
        if country in COUNTRIES:
            by_country[country].append(row)

    final: list[dict[str, Any]] = []
    reserve: list[dict[str, Any]] = []
    topic_limit = int(rules.get("topic_max_per_country", 2) or 2)

    for country in COUNTRIES:
        items = sorted(by_country.get(country, []), key=_rank_key)
        quota = rules.get("quotas", {}).get(country, {"max": 6})
        max_items = int(quota.get("max", 6))
        topic_counts: Counter[str] = Counter()
        picked_ids: set[Any] = set()

        for row in items:
            topic = row.get("topic_cluster", "other")
            if len([x for x in final if x.get("route_country") == country]) >= max_items:
                break
            if topic_counts[topic] >= topic_limit:
                continue
            row["status"] = f"FINAL_{country}"
            row["final_bucket"] = f"FINAL_{country}"
            row["selected"] = True
            row["layer_reasons"].append("L4:final shortlist")
            final.append(row)
            picked_ids.add(row.get("id"))
            topic_counts[topic] += 1

        for row in items:
            if row.get("id") in picked_ids:
                continue
            row["status"] = "RESERVE"
            row["final_bucket"] = "RESERVE"
            row["selected"] = False
            row["reserve_reason"] = "quota_or_topic_mix"
            row["layer_reasons"].append("L4:reserve after quota/topic mix")
            reserve.append(row)

    return final, reserve


def _dedupe_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: _source_quality(r), reverse=True):
        title = row.get("title", "")
        duplicate = None
        for existing in kept:
            if fuzz.token_set_ratio(title, existing.get("title", "")) >= 90:
                duplicate = existing
                break
        if not duplicate:
            kept.append(row)
            continue
        if _completeness(row) > _completeness(duplicate):
            kept.remove(duplicate)
            kept.append(row)
            row["layer_reasons"].append("L4:kept over duplicate with fuller terms")
    return kept


def _source_quality(row: dict[str, Any]) -> int:
    configured_tier = int(row.get("source_tier", 3) or 3)
    if configured_tier <= 1:
        return 4
    if configured_tier == 2:
        return 3
    source = (row.get("source") or "").lower()
    if any(x in source for x in ("reuters", "bloomberg")):
        return 3
    if any(x in source for x in ("zawya", "ked", "wam", "qazinform", "national", "chosun")):
        return 2
    return 1


def _completeness(row: dict[str, Any]) -> int:
    text = _text(row)
    return int(_has_money_or_scale(text)) + int(_has_number(text)) + len(MONEY_RE.findall(text)) + min(3, len(text) // 500)


def _score_row(row: dict[str, Any], rules: dict[str, Any]) -> None:
    text = _text(row)
    flags = _hard_flags(text, rules)
    hardness = 0
    hardness += 2 if flags["terms"] else 0
    hardness += 1 if flags["number"] else 0
    hardness += 1 if flags["next_milestone"] else 0
    hardness += 1 if _has_money_or_scale(text) else 0
    scale = 0
    if _has_money_or_scale(text):
        scale += 2
    if re.search(r"\b(billion|bn|亿|GW|GWh)\b", text, re.I):
        scale += 1
    specificity = min(2, len(MONEY_RE.findall(text)) + (1 if row.get("watchlist_hit") else 0) + (1 if "," in text or ";" in text else 0))
    row["hardness_score"] = float(min(5, hardness))
    row["scale_score"] = float(min(3, scale))
    row["specificity_score"] = float(min(2, specificity))
    row["score"] = row["hardness_score"] + row["scale_score"] + row["specificity_score"]
    row["score_reason"] = (
        f"L4 score={row['score']:.1f} "
        f"(hardness {row['hardness_score']:.0f}, scale {row['scale_score']:.0f}, specificity {row['specificity_score']:.0f})"
    )


def _priority_band(row: dict[str, Any]) -> int:
    country = row.get("route_country")
    if country in ("KSA", "UAE"):
        if row.get("china_hk_linkage"):
            return 0
        if row.get("watchlist_hit"):
            return 1
        return 2
    if country == "KZ":
        if row.get("china_hk_linkage"):
            return 0
        if row.get("watchlist_hit"):
            return 1
        return 2
    if country == "KR":
        return 0 if row.get("watchlist_hit") else 1
    return 9


def _rank_key(row: dict[str, Any]) -> tuple[int, int, float, float]:
    manual_rank = 0 if row.get("is_manual") else 1
    return (manual_rank, _priority_band(row), -float(row.get("score", 0) or 0), -_published_ts(row))


def _published_ts(row: dict[str, Any]) -> float:
    value = row.get("published_at")
    if not value:
        return 0.0
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _topic_cluster(text: str) -> str:
    low = text.lower()
    topics = {
        "energy": ["energy", "power", "solar", "wind", "hydrogen", "battery", "oil", "gas", "renewable", "能源", "电力", "光伏", "风电", "氢", "电池", "油气"],
        "finance": ["ipo", "listing", "bond", "sukuk", "bank", "fund", "capital", "exchange", "债券", "上市", "银行", "基金", "交易所"],
        "infrastructure": ["railway", "port", "logistics", "corridor", "airport", "road", "infrastructure", "铁路", "港口", "物流", "走廊", "机场", "基建"],
        "technology": ["ai", "semiconductor", "cloud", "data center", "telecom", "chip", "科技", "半导体", "云", "数据中心", "电信"],
        "healthcare": ["bio", "pharma", "health", "medical", "drug", "生物", "医药", "医疗"],
        "industrial": ["factory", "manufacturing", "mining", "metals", "plant", "工业", "制造", "矿", "金属", "工厂"],
    }
    for topic, terms in topics.items():
        if any(term in low for term in terms):
            return topic
    return "policy" if any(term in low for term in ["treaty", "agreement", "mou", "policy", "协定", "协议", "政策"]) else "other"


def _cap_hold_rows(rows: list[dict[str, Any]], total: int, rules: dict[str, Any]) -> list[dict[str, Any]]:
    ratio = float(rules.get("hold_missing_terms_cap_ratio", 0.05) or 0.05)
    cap = max(1, int(total * ratio)) if total else 1
    missing_terms = [r for r in rows if r.get("status") == "HOLD_missing_terms"]
    other_hold = [r for r in rows if r.get("status") != "HOLD_missing_terms"]
    for row in missing_terms[cap:]:
        row["status"] = "DROP_hold_cap"
        row["layer_reasons"].append("L3:HOLD_missing_terms cap exceeded")
    visible = other_hold + missing_terms[:cap]
    for row in visible:
        row["selected"] = False
        row.setdefault("score_reason", "")
    return visible


def _assign_display_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for row in rows:
        if float(row.get("score", 0) or 0) <= 0 and not row.get("is_manual") and not str(row.get("status", "")).startswith("HOLD"):
            continue
        route = row.get("route_country")
        if route in COUNTRIES:
            row["country_code"] = route
        elif row.get("original_country_code") in COUNTRIES:
            row["country_code"] = row["original_country_code"]
        row["layer_reasons"] = list(dict.fromkeys(row.get("layer_reasons", [])))
        visible.append(row)
    return sorted(visible, key=_display_key)


def _display_key(row: dict[str, Any]) -> tuple[int, int, float, float]:
    status = str(row.get("status", ""))
    if row.get("is_manual"):
        status_rank = 0
    elif status.startswith("FINAL"):
        status_rank = 1
    elif status == "RESERVE":
        status_rank = 2
    elif status.startswith("HOLD"):
        status_rank = 3
    else:
        status_rank = 4
    manual_order = row.get("manual_order")
    order = int(manual_order) if manual_order is not None else 9999
    return (status_rank, order, -float(row.get("score", 0) or 0), -_published_ts(row))
