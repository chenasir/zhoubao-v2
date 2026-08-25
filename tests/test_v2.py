from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import formatter
from app import source_utils
from app.config import _env_int, load_sources, settings
from app.main import app
from app.models import FormattedItem
from app.retriever import _validate_public_url, _watchlist_queries, fetch_manual_url, resolve_publisher_url


class SourceRegistryTests(unittest.TestCase):
    def test_non_google_url_does_not_need_resolution(self):
        url = "https://www.reuters.com/world/example"
        self.assertEqual(resolve_publisher_url(url), url)

    def test_real_publisher_is_not_overwritten_by_google_news_host(self):
        google_url = "https://news.google.com/rss/articles/example"
        self.assertEqual(source_utils.normalize_source_name("SWFI", url=google_url), "SWFI")

    def test_generic_news_label_falls_back_to_publisher_homepage(self):
        self.assertEqual(
            source_utils.best_source_name(
                "News",
                article_url="https://news.google.com/rss/articles/example",
                homepage_url="https://www.reuters.com/",
            ),
            "Reuters",
        )

    def test_credited_source_is_detected_inside_reposted_content(self):
        self.assertEqual(source_utils.detect_source_from_content(text="本文来源：ZAWYA"), "Zawya")
        self.assertEqual(source_utils.detect_source_from_content(text="据路透社报道，该公司完成交易。"), "Reuters")

    def test_domain_sources_are_rendered_as_names_without_tlds(self):
        self.assertEqual(source_utils.normalize_source_name("marketscreener.com"), "MarketScreener")
        self.assertEqual(source_utils.normalize_source_name("thenationalnews.com"), "The National")
        self.assertEqual(source_utils.normalize_source_name("example-finance.com"), "Example Finance")
        self.assertEqual(source_utils.normalize_source_name("publisher.co.uk"), "Publisher")
        self.assertEqual(
            source_utils.detect_source_from_content(text="来源：marketscreener.com"),
            "MarketScreener",
        )

    def test_manual_wechat_repost_separates_carrier_and_publisher(self):
        body = "来源：ZAWYA。" + ("阿联酋投资机构宣布完成一项重大交易。" * 30)
        html = f"""
        <html><head>
          <meta property="og:title" content="阿联酋投资机构完成交易" />
          <meta property="og:site_name" content="微信公众平台" />
        </head><body><article><p>来源：ZAWYA</p><p>{body}</p>
          <a href="https://www.zawya.com/en/business/example">查看原文</a>
        </article></body></html>
        """
        response = unittest.mock.Mock()
        response.text = html
        with patch("app.retriever.fetch_public_response", return_value=response):
            item = fetch_manual_url("UAE", "https://mp.weixin.qq.com/s/example", "公众号")
        self.assertIsNotNone(item)
        self.assertEqual(item["source"], "Zawya")
        self.assertEqual(item["source_carrier"], "公众号")
        self.assertEqual(item["source_article_url"], "https://www.zawya.com/en/business/example")
        self.assertEqual(item["source_homepage"], "https://www.zawya.com/")

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
    def test_blank_integer_env_uses_default(self):
        with patch.dict("os.environ", {"TEST_BLANK_INTEGER": ""}):
            self.assertEqual(_env_int("TEST_BLANK_INTEGER", 90), 90)

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
    def test_fast_formatter_uses_two_core_llm_calls(self):
        row = {
            "country_code": "KSA",
            "source": "Test",
            "title": "Company invests USD 1 million",
            "url": "",
            "published_at": "2026-08-14T00:00:00+00:00",
            "fetched_body": ("Company announced a USD 1 million investment on August 14, 2026. " * 8),
        }
        english = {
            "en_title": "Company invests USD 1 million",
            "en_body": "The company announced a USD 1 million investment on August 14, 2026.",
            "facts": {"amounts": ["USD 1 million"], "dates": ["August 14, 2026"]},
        }
        chinese = {
            "cn_title": "公司投资100万美元",
            "cn_body": "8月14日，该公司宣布投资100万美元。",
        }
        with (
            patch("app.formatter._generate_english", return_value=english) as generate_english,
            patch("app.formatter._translate_chinese", return_value=chinese) as translate_chinese,
        ):
            item = formatter.format_one(row, runtime_config={"openrouter_api_key": "test"})
        self.assertIsNotNone(item)
        self.assertEqual(generate_english.call_count, 1)
        self.assertEqual(translate_chinese.call_count, 1)

    def test_formatter_uses_publisher_name_and_direct_article_link(self):
        google_url = "https://news.google.com/rss/articles/example"
        direct_url = "https://www.reuters.com/world/example"
        row = {
            "country_code": "UAE",
            "source": "News",
            "source_homepage": "https://www.reuters.com/",
            "title": "Company announces investment",
            "url": google_url,
            "published_at": "2026-08-14T00:00:00+00:00",
            "fetched_body": ("The company announced an investment on August 14, 2026. " * 8),
        }
        english = {
            "en_title": "Company announces investment",
            "en_body": "The company announced an investment on August 14, 2026.",
            "facts": {"dates": ["August 14, 2026"]},
        }
        chinese = {"cn_title": "公司宣布投资", "cn_body": "8月14日，该公司宣布一项投资。"}
        with (
            patch("app.formatter.retriever.resolve_publisher_url", return_value=direct_url),
            patch("app.formatter.article_fetch.fetch_article") as fetch_article,
            patch("app.formatter._generate_english", return_value=english),
            patch("app.formatter._translate_chinese", return_value=chinese),
        ):
            fetch_article.return_value.body = row["fetched_body"]
            fetch_article.return_value.html = ""
            fetch_article.return_value.published_at = None
            item = formatter.format_one(row, runtime_config={"openrouter_api_key": "test"})
        self.assertIsNotNone(item)
        self.assertEqual(item.source_name, "Reuters")
        self.assertEqual(item.source_url, direct_url)
        self.assertIn("Source: Reuters", item.source_label_en)
        self.assertIn("来源：Reuters", item.source_label_zh)
        self.assertNotIn("Source: News", item.source_label_en)

    def test_formatter_ai_can_replace_carrier_with_supported_publisher(self):
        wechat_url = "https://mp.weixin.qq.com/s/example"
        row = {
            "country_code": "KSA",
            "source": "公众号",
            "source_carrier": "公众号",
            "title": "Saudi company announces a transaction",
            "url": wechat_url,
            "published_at": "2026-08-14T00:00:00+00:00",
            "fetched_body": ("The repost says the original report was published by Reuters. " * 8),
        }
        english = {
            "en_title": "Saudi company announces a transaction",
            "en_body": "On August 14, the company announced a transaction.",
            "facts": {"dates": ["August 14, 2026"]},
            "original_source_name": "Reuters",
            "original_source_evidence": "published by Reuters",
        }
        chinese = {"cn_title": "沙特公司宣布一项交易", "cn_body": "8月14日，该公司宣布一项交易。"}
        with (
            patch("app.formatter.retriever.resolve_publisher_url", return_value=wechat_url),
            patch("app.formatter._generate_english", return_value=english),
            patch("app.formatter._translate_chinese", return_value=chinese),
        ):
            item = formatter.format_one(row, runtime_config={"openrouter_api_key": "test"})
        self.assertIsNotNone(item)
        self.assertEqual(item.source_name, "Reuters")
        self.assertEqual(item.source_url, "https://www.reuters.com/")
        self.assertIn("来源：Reuters", item.source_label_zh)

    def test_resolve_url_endpoint(self):
        google_url = "https://news.google.com/rss/articles/test"
        publisher_url = "https://example.com/news"
        with patch("app.main.retriever.resolve_publisher_url", return_value=publisher_url):
            with TestClient(app) as client:
                response = client.post("/api/resolve-url", json={"url": google_url})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"url": publisher_url, "resolved": True})

    def test_missing_llm_key_returns_readable_error(self):
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
        from app.llm import LlmConfigurationError

        with patch("app.main.formatter.format_one", side_effect=LlmConfigurationError("请配置 API Key")):
            with TestClient(app) as client:
                response = client.post("/api/format-item", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "请配置 API Key")

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
