from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import load_sources, settings
from app.main import app
from app.models import FormattedItem
from app.retriever import _validate_public_url, _watchlist_queries


class SourceRegistryTests(unittest.TestCase):
    def test_registry_has_four_countries_and_metadata(self):
        countries = load_sources()
        self.assertEqual([row["code"] for row in countries], ["KSA", "UAE", "KZ", "KR"])
        for country in countries:
            self.assertGreaterEqual(len(country["sources"]), 18)
            names = [source["name"] for source in country["sources"]]
            self.assertEqual(len(names), len(set(names)))
            for source in country["sources"]:
                self.assertIn(source["type"], {"rss", "google_news", "watchlist_search"})
                self.assertIn(int(source.get("tier", 3)), {1, 2, 3})
                self.assertTrue(source.get("category"))

    def test_only_verified_direct_rss_are_enabled(self):
        rss = [
            source["url"]
            for country in load_sources()
            for source in country["sources"]
            if source["type"] == "rss" and source.get("enabled", True)
        ]
        self.assertEqual(
            rss,
            [
                "https://www.arabnews.com/rss.xml",
                "https://www.agbi.com/feed/",
                "https://www.kedglobal.com/rss/all.xml",
            ],
        )

    def test_watchlist_queries_are_batched(self):
        source = {"limit": 10, "group_size": 4, "terms": "(deal OR investment)"}
        with patch("app.retriever.load_watchlist", return_value={"KSA": [f"Company {i}" for i in range(10)]}):
            queries = _watchlist_queries("KSA", source)
        self.assertEqual(len(queries), 3)
        self.assertIn('"Company 0" OR "Company 1"', queries[0])


class SecurityTests(unittest.TestCase):
    def test_private_urls_are_rejected(self):
        for url in ["http://127.0.0.1/x", "http://10.0.0.1/x", "http://[::1]/x", "file:///etc/passwd"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                _validate_public_url(url)

    def test_optional_api_token(self):
        old = settings.app_access_token
        settings.app_access_token = "test-secret"
        try:
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
                self.assertEqual(client.get("/api/countries").status_code, 401)
                response = client.get("/api/countries", headers={"X-App-Token": "test-secret"})
                self.assertEqual(response.status_code, 200)
        finally:
            settings.app_access_token = old


class StatelessGenerationTests(unittest.TestCase):
    def test_format_item_endpoint_returns_structured_item(self):
        item = FormattedItem(
            country_code="KSA",
            cn_title="中文标题",
            cn_body="中文正文",
            en_title="English title",
            en_body="English body",
            source_label_en="(Source: Test)",
            source_label_zh="（来源：Test）",
            url="https://example.com/news",
        )
        payload = {
            "candidate": {
                "country_code": "KSA",
                "source": "Test",
                "title": "Test title",
                "url": "https://example.com/news",
                "selected": True,
            },
            "runtime": {},
        }
        with patch("app.main.formatter.format_one", return_value=item):
            with TestClient(app) as client:
                response = client.post("/api/format-item", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["country_code"], "KSA")


if __name__ == "__main__":
    unittest.main()
