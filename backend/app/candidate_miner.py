"""话术挖掘:扫描客服真实回复 + LLM 评估 + 入候选池待审核。

流程:
1. 拿时间窗口内客服文本回复(≥20 字)
2. 配对家长 query (这条回复之前 30 分钟内同客户最近一条家长 text)
3. 跑 RAG 检索:已有 ≥0.85 类似话术则跳过(避免重复)
4. 排除 AI 一字不差采用的(已经在生产了)
5. LLM 评估:打分 + 脱敏 + 推 category + 推 variants
6. score >= 7 才入候选池(防噪)
"""
import asyncio
import hashlib
import json
import logging
import re
import time

import httpx

from app.config import settings
from app.rag import answer_store, embed as rag_embed
from app.storage import get_storage

logger = logging.getLogger(__name__)

MIN_REPLY_LEN = 20            # < 20 字的回复跳过(敷衍/简短)
MIN_PARENT_LEN = 5            # 家长 query 太短跳过
MIN_SCORE = 7.0               # LLM 评分阈值
PAIR_WINDOW_SECS = 1800       # 客服回复对应家长 query 的时间窗(30 分钟)
ANSWER_MATCH_THRESHOLD = settings.candidate_answer_match_threshold  # 答案侧相似度阈值,>= 触发"建议合并"


_EVAL_PROMPT = """你是 K12 教培客服话术整理助手。我有一条客服真实回复,请评估它是否值得沉淀为标准话术,并给出建议。

【家长当时的提问】
{query}

【客服的回复】
{reply}

请输出 JSON,严格按以下结构:
{{
  "score": 0-10 整数,
  "reason": "简短理由 10-30 字",
  "category": "分类标签 4-15 字",
  "variants": ["问法1","问法2","问法3","问法4"],
  "cleaned": "脱敏后的回复"
}}

评分标准:
- 9-10: 信息完整、口吻好、能直接复用
- 7-8: 内容好但需小幅润色
- 5-6: 一般,有信息但不够全
- 0-4: 敷衍/无信息(只说"好的""嗯""在吗"等)

variants 要求: 4 个家长口语化问法,角度互补,5-25 字

脱敏原则:
- 孩子真名 → 替换为 "孩子" 或留 "{{孩子姓名}}"
- 家长姓名/手机号/详细地址 → 删除或占位
- 保留: 学习机型号(P4 等)、课程名、操作路径、通用建议
- 不要修改话术风格和语气

只输出 JSON,不要任何额外说明或代码块标记。"""


async def _evaluate_phrase(query: str, reply: str) -> dict:
    """LLM 评估单条客服回复。"""
    prompt = _EVAL_PROMPT.format(query=query[:500], reply=reply[:1500])
    body = {
        "model": "doubao-1-5-lite-32k-250115",  # lite 速度优先,评分任务足够
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{settings.doubao_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.doubao_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
    text = resp.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text).strip()
    parsed = json.loads(text)
    return {
        "score": float(parsed.get("score", 0)),
        "reason": (parsed.get("reason") or "").strip(),
        "category": (parsed.get("category") or "").strip(),
        "variants": [v.strip() for v in (parsed.get("variants") or []) if isinstance(v, str) and v.strip()][:4],
        "cleaned": (parsed.get("cleaned") or reply).strip(),
    }


def _reply_hash(reply: str) -> str:
    return hashlib.sha1(reply[:200].encode("utf-8")).hexdigest()


async def scan(since_ts: float, until_ts: float) -> dict:
    """扫描时间窗内客服回复 → LLM 评估 → 写候选池。返回统计。"""
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT seq, received_at, from_user, to_users, raw_json "
        "FROM chat_messages "
        "WHERE received_at >= ? AND received_at < ? "
        "  AND msg_type='text' AND from_user NOT LIKE 'wm%' "
        "ORDER BY seq",
        (since_ts, until_ts),
    ).fetchall()

    stats = {"total": len(rows), "skip_short": 0, "skip_dup": 0, "skip_no_query": 0,
             "skip_rag_match": 0, "skip_was_ai_adopt": 0, "skip_low_score": 0,
             "scored": 0, "added": 0, "merge_suggested": 0, "errors": 0}

    from app.rag import retrieve as rag_retrieve

    for seq, recv_at, from_user, to_users_json, raw_json in rows:
        try:
            raw = json.loads(raw_json)
            content = raw.get("text", {}).get("content", "")
        except Exception:
            stats["errors"] += 1
            continue

        if len(content) < MIN_REPLY_LEN:
            stats["skip_short"] += 1
            continue

        rh = _reply_hash(content)
        if storage.conn.execute("SELECT 1 FROM candidate_phrases WHERE reply_hash=?", (rh,)).fetchone():
            stats["skip_dup"] += 1
            continue

        try:
            tolist = json.loads(to_users_json) if to_users_json else []
            customer_id = tolist[0] if tolist else None
        except Exception:
            customer_id = None
        if not customer_id or not customer_id.startswith("wm"):
            stats["skip_no_query"] += 1
            continue

        # 找客服这条之前 30 分钟内最近的家长 text
        parent_row = storage.conn.execute(
            "SELECT raw_json FROM chat_messages "
            "WHERE from_user = ? AND msg_type='text' "
            "  AND received_at < ? AND received_at > ? "
            "ORDER BY seq DESC LIMIT 1",
            (customer_id, recv_at, recv_at - PAIR_WINDOW_SECS),
        ).fetchone()
        if not parent_row:
            stats["skip_no_query"] += 1
            continue
        try:
            parent_query = json.loads(parent_row[0]).get("text", {}).get("content", "")
        except Exception:
            stats["skip_no_query"] += 1
            continue
        if len(parent_query) < MIN_PARENT_LEN:
            stats["skip_no_query"] += 1
            continue

        # 已有相似话术(避免重复入库)
        try:
            cands = await rag_retrieve.retrieve(parent_query, top_k=1, customer_id="")
            if cands and cands[0]["similarity"] >= 0.85:
                stats["skip_rag_match"] += 1
                continue
        except Exception:
            pass

        # 已经是 AI 一字不差采用的(已沉淀)
        ai_adopt = storage.conn.execute(
            "SELECT 1 FROM events "
            "WHERE event_type='draft_adopt' "
            "  AND created_at > ? AND created_at < ? "
            "  AND customer_id = ? "
            "  AND json_extract(data,'$.answer') = ?",
            (recv_at - 60, recv_at + 60, customer_id, content),
        ).fetchone()
        if ai_adopt:
            stats["skip_was_ai_adopt"] += 1
            continue

        # 答案侧匹配 —— 客服回复跟已采纳 best_answer 高度相似 → 跳过 LLM,直接挂"建议合并"
        suggested_merge_entry_id = None
        answer_match_similarity = None
        try:
            reply_vec = await rag_embed.embed_one(content)
            ans_matches = await answer_store.search_answer(reply_vec, top_k=1)
            if ans_matches and ans_matches[0]["_similarity"] >= ANSWER_MATCH_THRESHOLD:
                suggested_merge_entry_id = ans_matches[0]["entry_id"]
                answer_match_similarity = ans_matches[0]["_similarity"]
        except Exception:
            logger.exception("答案侧匹配失败 seq=%s (降级走 LLM 评估)", seq)

        if suggested_merge_entry_id is not None:
            try:
                storage.conn.execute(
                    "INSERT INTO candidate_phrases "
                    "(parent_query, staff_reply, cleaned_reply, suggested_category, "
                    " suggested_variants, llm_score, llm_reason, source_seq, customer_id, reply_hash, "
                    " suggested_merge_entry_id, answer_match_similarity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        parent_query, content, content,
                        "(待合并)", json.dumps([parent_query], ensure_ascii=False),
                        round(answer_match_similarity * 10, 2),
                        "客服回复与已采纳话术答案高度相似",
                        seq, customer_id, rh,
                        suggested_merge_entry_id, answer_match_similarity,
                    ),
                )
                stats["merge_suggested"] += 1
                stats["added"] += 1
            except Exception:
                logger.exception("写合并建议候选失败 seq=%s", seq)
                stats["errors"] += 1
            continue

        # LLM 评估
        try:
            ev = await _evaluate_phrase(parent_query, content)
        except Exception as e:
            logger.warning("评估 seq=%s 失败: %s", seq, e)
            stats["errors"] += 1
            continue
        stats["scored"] += 1

        if ev["score"] < MIN_SCORE:
            stats["skip_low_score"] += 1
            continue

        try:
            storage.conn.execute(
                "INSERT INTO candidate_phrases "
                "(parent_query, staff_reply, cleaned_reply, suggested_category, "
                " suggested_variants, llm_score, llm_reason, source_seq, customer_id, reply_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    parent_query, content, ev["cleaned"],
                    ev["category"], json.dumps(ev["variants"], ensure_ascii=False),
                    ev["score"], ev["reason"],
                    seq, customer_id, rh,
                ),
            )
            stats["added"] += 1
        except Exception:
            logger.exception("写候选失败 seq=%s", seq)
            stats["errors"] += 1

    return stats


async def daily_scheduler(hour_local: int = 5):
    """每天北京时间 hour_local 点跑一次,扫描"昨天"客服回复。

    candidate_phrases 写入 status='pending',等待人工去 admin 后台审核 adopt。
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    while True:
        try:
            now = datetime.now(tz)
            next_run = now.replace(hour=hour_local, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run = next_run + timedelta(days=1)
            wait_secs = (next_run - now).total_seconds()
            logger.info("[candidate-cron] 下次扫描 %s,等待 %.0f 秒", next_run, wait_secs)
            await asyncio.sleep(wait_secs)

            today0 = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            yest0 = today0 - timedelta(days=1)
            since, until = yest0.timestamp(), today0.timestamp()
            logger.info("[candidate-cron] 开始扫昨天 %s → %s", yest0.date(), today0.date())
            stats = await scan(since, until)
            logger.info("[candidate-cron] 完成 stats=%s", stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[candidate-cron] 异常,1 小时后重试")
            await asyncio.sleep(3600)
