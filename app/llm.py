"""OpenRouter / DeepSeek 客户端封装。"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from openai import OpenAI
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from .config import settings

logger = logging.getLogger(__name__)


class LlmConfigurationError(RuntimeError):
    pass


def _resolve_runtime_config(runtime_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    runtime = dict(runtime_config or {})
    return {
        "openrouter_api_key": runtime.get("openrouter_api_key") or settings.openrouter_api_key,
        "openrouter_base_url": runtime.get("openrouter_base_url") or settings.openrouter_base_url,
        "llm_model": runtime.get("llm_model") or settings.llm_model,
        "llm_timeout": int(runtime.get("llm_timeout") or settings.llm_timeout),
    }


def _client(runtime_config: Mapping[str, Any] | None = None) -> OpenAI:
    resolved = _resolve_runtime_config(runtime_config)
    return OpenAI(
        base_url=resolved["openrouter_base_url"],
        api_key=resolved["openrouter_api_key"],
        timeout=resolved["llm_timeout"],
        default_headers={
            # OpenRouter 推荐带上 referer / title（可选）
            "HTTP-Referer": "https://github.com/cicc/zhoubao-agent",
            "X-Title": "CICC Weekly News Digest Agent",
        },
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_not_exception_type(LlmConfigurationError),
    reraise=True,
)
def chat(
    system: str,
    user: str,
    response_format_json: bool = False,
    temperature: float = 0.2,
    runtime_config: Mapping[str, Any] | None = None,
) -> str:
    """发起一次聊天补全。response_format_json 仅用于提示 LLM 输出 JSON。"""
    resolved = _resolve_runtime_config(runtime_config)
    if not resolved["openrouter_api_key"]:
        raise LlmConfigurationError("未配置 OpenRouter API Key。请展开页面中的“运行配置”后填写，或在 Vercel 环境变量中设置 OPENROUTER_API_KEY。")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {
        "model": resolved["llm_model"],
        "messages": messages,
        "temperature": temperature,
    }
    if response_format_json:
        kwargs["response_format"] = {"type": "json_object"}

    resp = _client(resolved).chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    return content.strip()


def chat_json(
    system: str,
    user: str,
    temperature: float = 0.2,
    runtime_config: Mapping[str, Any] | None = None,
) -> dict:
    """发起一次 JSON 模式的聊天补全，返回解析后的 dict。"""
    raw = chat(
        system,
        user,
        response_format_json=True,
        temperature=temperature,
        runtime_config=runtime_config,
    )
    # 有些模型偶尔会在 JSON 外面加 ```json ... ``` 或额外文本，做一次容错清理
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 再尝试抽取第一个 { ... } 块
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise
