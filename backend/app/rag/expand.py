"""LLM 扩展同义问法 — 用阿里百炼 qwen-plus(OpenAI 兼容协议)。

两种用法:
- expand_variants: 已有 1-3 个 variant,补到 5 个 (结构化模式)
- auto_categorize: 只有答案,生成分类 + 5 个问法 (智能模式)
"""
import json
import logging
import re
from typing import List

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """你是 K12 教培行业的客服话术整理助手。家长可能用不同方式问同一个问题(口语化、碎片、错别字、绕弯)。

【场景】家长向客服老师咨询,客服查询知识库匹配标准答案。
【已有问法】
{seed_variants}

【对应回答(供你理解语义,不要照抄到问法里)】
{answer}

请额外补充 {n_more} 个家长可能的问法,要求:
- 自然口语,贴近真实家长口吻(可以用"老师我想问下…"、"那个…"等)
- 不要重复已有问法
- 每行一个,不要编号、不要解释
- 长度 5-25 字之间为佳

直接输出问法,每行一个:"""


async def expand_variants(seed_variants: List[str], best_answer: str, target_count: int = 5) -> List[str]:
    """补足 variants 到 target_count 个。失败时返回原 seed (不抛异常,让上游决定降级)。"""
    seed_variants = [v.strip() for v in seed_variants if v.strip()]
    if len(seed_variants) >= target_count:
        return seed_variants
    n_more = target_count - len(seed_variants)
    if not settings.aliyun_api_key:
        logger.warning("ALIYUN_API_KEY 未配,跳过 LLM 扩展")
        return seed_variants

    prompt = _PROMPT_TEMPLATE.format(
        seed_variants="\n".join(f"- {v}" for v in seed_variants),
        answer=(best_answer or "")[:200],
        n_more=n_more,
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.aliyun_embed_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.aliyun_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.aliyun_llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 400,
                },
            )
        if resp.status_code != 200:
            logger.warning("LLM 扩展 HTTP %s: %s", resp.status_code, resp.text[:200])
            return seed_variants
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("LLM 扩展失败")
        return seed_variants

    # 解析每行 — 去掉编号 / 横线 / 引号 / 多余空白
    new_variants = []
    seen = set(v.lower() for v in seed_variants)
    for line in text.split("\n"):
        v = line.strip()
        v = re.sub(r"^[-•*0-9.\)]\s*", "", v)  # 去开头的 1. - • 等
        v = v.strip(' "\'""''`')
        if not v or len(v) > 60:
            continue
        if v.lower() in seen:
            continue
        seen.add(v.lower())
        new_variants.append(v)
        if len(new_variants) >= n_more:
            break

    return seed_variants + new_variants


_AUTO_PROMPT = """你是 K12 教培行业客服话术整理助手。我会给你一段客服的标准回答,请你完成两件事:

1. 给这段回答总结一个简短的分类标签 (4-15 字,例如"伴学APP使用指南"、"价格优惠政策"、"打卡积分规则")。
2. 模拟家长可能用 {n} 种不同的口语化方式来问出对应这段回答的问题。

【客服标准回答】
{answer}

要求:
- 问法贴近家长真实口吻,口语化,可以有"老师"、"想问下"、"那个…"这种,长度 5-25 字
- {n} 个问法之间表达要尽量不一样 (有的直接问、有的描述场景、有的问怎么办)
- 不要重复同一种说法

严格按 JSON 输出,不要任何额外解释或代码块标记:
{{"category": "分类标签", "variants": ["问法1", "问法2", "问法3", "问法4", "问法5"]}}"""


async def auto_categorize(answer: str, target_count: int = 5) -> dict:
    """从纯答案自动推出分类 + N 个家长问法。

    返回 {"category": str, "variants": [str, ...]}。失败抛 RuntimeError(上游决定降级)。
    """
    answer = (answer or "").strip()
    if not answer:
        raise RuntimeError("答案为空")
    if not settings.aliyun_api_key:
        raise RuntimeError("ALIYUN_API_KEY 未配置")

    prompt = _AUTO_PROMPT.format(answer=answer[:2000], n=target_count)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.aliyun_embed_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.aliyun_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.aliyun_llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.6,
                    "max_tokens": 600,
                    "response_format": {"type": "json_object"},
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"LLM 请求失败: {e}")

    # 容错: 模型可能套 ```json ``` 代码块
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM 输出非合法 JSON: {e}; 内容前 200 字: {text[:200]}")

    category = (parsed.get("category") or "").strip()
    variants = parsed.get("variants") or []
    variants = [v.strip() for v in variants if isinstance(v, str) and v.strip()]
    # 去重保序
    seen = set()
    cleaned = []
    for v in variants:
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(v)
    if not category:
        raise RuntimeError("LLM 未返回 category")
    if len(cleaned) < 2:
        raise RuntimeError(f"LLM 返回的 variants 太少: {cleaned}")
    return {"category": category, "variants": cleaned[:target_count]}
