"""多供应商 LLM 流式 chat completion 客户端。

支持 DeepSeek / 豆包 / 阿里通义 (都用 OpenAI 兼容接口)。
当前激活的供应商和模型由 admin 后台 /llm-config 配置 (kv_settings 表)。

要点:
- 部分模型内置推理(reasoning_content),前端只需要最终 content,这里消化掉 reasoning。
- 用 httpx AsyncClient stream 模式接 SSE,逐 chunk 解析转发。
"""
import json
import logging
from typing import AsyncIterator, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# 客服话术中要剥掉的"非自然手打"字符:
_STRIP_CHARS = set('*"“”')


def _sanitize(text: str) -> str:
    if not text:
        return text
    return "".join(c for c in text if c not in _STRIP_CHARS)


# 支持的模型清单(供 admin UI 显示 + 路由)
SUPPORTED_MODELS = [
    {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "label": "DeepSeek V4 Flash",
        "desc": "带 reasoning,中等速度,3-5s",
    },
    {
        "provider": "doubao",
        "model": "doubao-1-5-lite-32k-250115",
        "label": "豆包 1.5 Lite 32k",
        "desc": "无 reasoning,秒回 ~0.8s,价格便宜",
    },
    {
        "provider": "doubao",
        "model": "doubao-1-5-pro-32k-250115",
        "label": "豆包 1.5 Pro 32k",
        "desc": "无 reasoning,质量更高 ~1.5s",
    },
    {
        "provider": "doubao",
        "model": "doubao-seed-1-6-flash-250828",
        "label": "豆包 1.6 Flash (Seed)",
        "desc": "带 reasoning,慢 ~12s",
    },
    {
        "provider": "aliyun",
        "model": "qwen-plus",
        "label": "通义 Qwen-Plus",
        "desc": "中等速度,质量稳定",
    },
    {
        "provider": "aliyun",
        "model": "qwen-max",
        "label": "通义 Qwen-Max",
        "desc": "最强但慢,贵",
    },
]

DEFAULT_MODEL = {"provider": "doubao", "model": "doubao-1-5-lite-32k-250115"}


def _provider_config(provider: str) -> tuple[str, str]:
    """返回 (base_url, api_key) for 指定 provider。"""
    if provider == "deepseek":
        return settings.deepseek_base_url, settings.deepseek_api_key
    if provider == "doubao":
        return settings.doubao_base_url, settings.doubao_api_key
    if provider == "aliyun":
        return settings.aliyun_embed_url, settings.aliyun_api_key
    raise ValueError(f"未知 provider: {provider}")


def get_active_model() -> dict:
    """从 kv_settings 读取当前激活模型,没配置就返回默认。"""
    from app.storage import get_storage
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT value FROM kv_settings WHERE key = ?", ("ai_model",)
    ).fetchone()
    if not row:
        return DEFAULT_MODEL.copy()
    try:
        d = json.loads(row[0])
        if "provider" in d and "model" in d:
            return d
    except Exception:
        pass
    return DEFAULT_MODEL.copy()


async def stream_chat(
    messages: List[dict],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 800,
) -> AsyncIterator[dict]:
    """以异步生成器形式 yield 事件 dict。

    yield 的事件:
        {"type": "delta", "content": "..."} - 增量内容(只来自 message.content,
                                              不含 reasoning_content)
        {"type": "done", "content": "<完整答案>"} - 结束,总内容
        {"type": "error", "message": "..."} - 出错
    """
    # 拿当前激活模型配置 (admin 后台可切换)
    active = get_active_model()
    provider = active["provider"]
    actual_model = model or active["model"]

    try:
        base_url, api_key = _provider_config(provider)
    except ValueError as e:
        yield {"type": "error", "message": str(e)}
        return
    if not api_key:
        yield {"type": "error", "message": f"{provider} API key 未配置"}
        return

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": actual_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    full_content = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    yield {"type": "error", "message": f"HTTP {resp.status_code}: {text.decode('utf-8', 'ignore')[:300]}"}
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[len("data:"):].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    # 只拿 message.content,丢弃 reasoning_content
                    content_piece = delta.get("content")
                    if content_piece:
                        safe = _sanitize(content_piece)
                        if safe:
                            full_content.append(safe)
                            yield {"type": "delta", "content": safe}
    except httpx.HTTPError as e:
        logger.exception("DeepSeek 流式请求失败")
        yield {"type": "error", "message": f"网络错误: {e}"}
        return
    except Exception as e:
        logger.exception("DeepSeek 未知错误")
        yield {"type": "error", "message": str(e)}
        return

    yield {"type": "done", "content": "".join(full_content)}
