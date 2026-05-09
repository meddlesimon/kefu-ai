"""调阿里百炼 text-embedding-v4 (Qwen3-Embedding 商用版)。OpenAI 兼容协议。"""
import logging
from typing import List

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class EmbedError(Exception):
    pass


async def embed(texts: List[str]) -> List[List[float]]:
    """批量 embed,返回向量列表。一次最多 25 条 (阿里限制)。"""
    if not texts:
        return []
    if not settings.aliyun_api_key:
        raise EmbedError("ALIYUN_API_KEY 未配置")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.aliyun_embed_url}/embeddings",
            headers={
                "Authorization": f"Bearer {settings.aliyun_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.aliyun_embed_model,
                "input": texts,
                "dimensions": settings.aliyun_embed_dim,
                "encoding_format": "float",
            },
        )
        if resp.status_code != 200:
            raise EmbedError(f"embedding HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "error" in data:
            raise EmbedError(f"embedding 失败: {data['error']}")
        return [item["embedding"] for item in data["data"]]


async def embed_one(text: str) -> List[float]:
    return (await embed([text]))[0]
