"""应用配置：加载 .env 与 yaml 配置。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
STATIC_DIR = ROOT / "static"
TEMPLATES_DIR = ROOT / "templates"

load_dotenv(ROOT / ".env")


class Settings:
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek/deepseek-v4-pro")
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "90"))
    llm_concurrency: int = max(1, int(os.getenv("LLM_CONCURRENCY", "3")))
    fetch_concurrency: int = max(1, min(12, int(os.getenv("FETCH_CONCURRENCY", "8"))))
    app_access_token: str = os.getenv("APP_ACCESS_TOKEN", "").strip()

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8765"))

    max_per_source: int = int(os.getenv("MAX_PER_SOURCE", "30"))
    lookback_days: int = int(os.getenv("LOOKBACK_DAYS", "7"))

    db_path: Path = DATA_DIR / "zhoubao.sqlite3"


settings = Settings()


def resolve_llm_concurrency(runtime_config: Mapping[str, Any] | None = None) -> int:
    """Frontend runtime config overrides server env when set to a positive value."""
    if runtime_config:
        raw = runtime_config.get("llm_concurrency")
        if raw is not None and int(raw) > 0:
            return max(1, min(8, int(raw)))
    return settings.llm_concurrency


@lru_cache(maxsize=1)
def load_sources() -> list[dict[str, Any]]:
    with (CONFIG_DIR / "sources.yaml").open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("countries", [])


@lru_cache(maxsize=1)
def get_country_order_map() -> dict[str, int]:
    return {c["code"]: i for i, c in enumerate(load_sources())}


@lru_cache(maxsize=1)
def get_source_order_map() -> dict[tuple[str, str], int]:
    order: dict[tuple[str, str], int] = {}
    for country in load_sources():
        code = country["code"]
        for i, src in enumerate(country.get("sources", [])):
            order[(code, src.get("name", ""))] = i
    return order


@lru_cache(maxsize=1)
def load_keywords() -> dict[str, list[str]]:
    with (CONFIG_DIR / "keywords.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def load_filtering_rules() -> dict[str, Any]:
    with (CONFIG_DIR / "filtering.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def load_watchlist() -> dict[str, list[str]]:
    path = CONFIG_DIR / "watchlist.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("countries", {}) or {}


@lru_cache(maxsize=1)
def load_source_aliases() -> dict[str, Any]:
    path = CONFIG_DIR / "source_aliases.yaml"
    if not path.exists():
        return {"aliases": {}, "homepages": {}}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"aliases": {}, "homepages": {}}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
