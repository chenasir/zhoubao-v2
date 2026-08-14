"""Audit configured news sources without changing application data."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import feedparser
import httpx

from app.config import load_sources
from app.retriever import HTTP_HEADERS, _google_news_rss_url, _watchlist_queries


def audit(country_filter: set[str], include_watchlist: bool) -> list[dict]:
    rows: list[dict] = []
    with httpx.Client(timeout=20.0, headers=HTTP_HEADERS, follow_redirects=True) as client:
        for country in load_sources():
            code = country["code"]
            if country_filter and code not in country_filter:
                continue
            for source in country.get("sources", []):
                if source.get("enabled", True) is False:
                    continue
                source_type = source.get("type")
                urls: list[str] = []
                if source_type == "rss":
                    urls = [source["url"]]
                elif source_type == "google_news":
                    urls = [
                        _google_news_rss_url(
                            source["query"],
                            hl=source.get("hl", "en-US"),
                            gl=source.get("gl", "US"),
                            ceid=source.get("ceid", "US:en"),
                        )
                    ]
                elif source_type == "watchlist_search" and include_watchlist:
                    queries = _watchlist_queries(code, source)
                    urls = [_google_news_rss_url(queries[0])] if queries else []
                elif source_type == "watchlist_search":
                    rows.append({"country": code, "source": source["name"], "type": source_type, "status": "skipped"})
                    continue

                for url in urls:
                    started = time.perf_counter()
                    try:
                        response = client.get(url)
                        parsed = feedparser.parse(response.content)
                        rows.append(
                            {
                                "country": code,
                                "source": source["name"],
                                "type": source_type,
                                "tier": int(source.get("tier", 3)),
                                "http_status": response.status_code,
                                "entries": len(parsed.entries),
                                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                                "ok": response.is_success and len(parsed.entries) > 0,
                                "final_url": str(response.url),
                            }
                        )
                    except Exception as exc:
                        rows.append(
                            {
                                "country": code,
                                "source": source["name"],
                                "type": source_type,
                                "tier": int(source.get("tier", 3)),
                                "ok": False,
                                "error": str(exc),
                            }
                        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--country", action="append", default=[], help="KSA/UAE/KZ/KR; repeatable")
    parser.add_argument("--include-watchlist", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = audit({value.upper() for value in args.country}, args.include_watchlist)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            status = "OK" if row.get("ok") else row.get("status", "FAIL").upper()
            detail = f"HTTP {row.get('http_status', '-')} / {row.get('entries', '-')} entries"
            if row.get("error"):
                detail = row["error"]
            print(f"{status:7} {row['country']:3} T{row.get('tier', '-')} {row['source']}: {detail}")
        ok = sum(1 for row in rows if row.get("ok"))
        checked = sum(1 for row in rows if row.get("status") != "skipped")
        print(f"\nHealthy: {ok}/{checked}; watchlist probes skipped unless --include-watchlist is used.")
    failed_rss = [row for row in rows if row.get("type") == "rss" and not row.get("ok")]
    return 1 if failed_rss else 0


if __name__ == "__main__":
    raise SystemExit(main())
