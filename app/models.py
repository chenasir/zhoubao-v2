"""数据模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RuntimeLlmConfig(BaseModel):
    """前端运行时传入的 OpenRouter 配置。"""

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "deepseek/deepseek-v4-pro"
    llm_timeout: int = 90
    llm_concurrency: int = 0


class CandidatePayload(BaseModel):
    """无状态 API 在前后端之间传输的候选新闻条目。"""

    id: int | str | None = None
    country_code: str
    source: str
    source_original: str = ""
    source_homepage: str = ""
    source_tier: int = 3
    source_category: str = "media"
    title: str
    title_zh: str = ""
    url: str
    google_news_url: str = ""
    published_at: Optional[str] = None
    summary: str = ""
    raw_lang: str = "en"
    score: float = 0.0
    score_reason: str = ""
    selected: bool = False
    is_manual: bool = False
    manual_order: Optional[int] = None
    fetched_body: str = ""
    created_at: Optional[str] = None
    original_country_code: str = ""
    route_country: str = ""
    status: str = ""
    gate_result: str = ""
    final_bucket: str = ""
    reserve_reason: str = ""
    topic_cluster: str = ""
    watchlist_hit: bool = False
    watchlist_country: str = ""
    watchlist_company: str = ""
    china_hk_linkage: bool = False
    hardness_score: float = 0.0
    scale_score: float = 0.0
    specificity_score: float = 0.0
    layer_reasons: list[str] = Field(default_factory=list)


class FormattedItem(BaseModel):
    """格式化后的一条新闻。"""

    country_code: str
    cn_title: str
    cn_body: str
    en_title: str
    en_body: str
    source_label_en: str
    source_label_zh: str
    url: str
    source_name: str = ""
    source_url: str = ""
    fact_check_warnings: list[str] = Field(default_factory=list)


class ScoreRequest(BaseModel):
    candidates: list[CandidatePayload]
    runtime: RuntimeLlmConfig


class GenerateRequest(BaseModel):
    candidates: list[CandidatePayload]
    runtime: RuntimeLlmConfig
    issue_number: int = 137
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    week_label: str = ""


class ManualUrlRequest(BaseModel):
    country_code: str
    url: str
    source: str = "Manual"


class RelatedSourcesRequest(BaseModel):
    title: str
    country_code: str = ""
    current_url: str = ""


class FormatItemRequest(BaseModel):
    candidate: CandidatePayload
    runtime: RuntimeLlmConfig


class RenderRequest(BaseModel):
    formatted_items: list[FormattedItem]
    issue_number: int = 137
    start_date: Optional[str] = None
    end_date: Optional[str] = None
