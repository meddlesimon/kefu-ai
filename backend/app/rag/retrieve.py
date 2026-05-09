"""检索主入口: 家长 query → embed → 向量库找近 → 通过 entry_id 拿 best_answer。"""
import json
import logging
from typing import List

from app.rag import embed, store
from app.storage import get_storage

logger = logging.getLogger(__name__)


async def retrieve(query: str, top_k: int = 4, customer_id: str = "") -> List[dict]:
    """主入口。返回候选话术列表,按相似度降序。"""
    if not query or not query.strip():
        return []

    # 1. embed query
    query_vec = await embed.embed_one(query)

    # 2. 多取一些 variant (因为多个 variant 可能命中同一 entry, 去重后才是 top_k)
    raw_results = await store.search(query_vec, top_k=top_k * 4)
    if not raw_results:
        return []

    # 3. 用 entry_id 拿详情, 同一 entry 只保留最高相似度的一条
    storage = get_storage()
    seen = set()
    candidates = []
    for row in raw_results:
        entry_id = row["entry_id"]
        if entry_id in seen:
            continue
        seen.add(entry_id)
        entry_row = storage.conn.execute(
            "SELECT id, category, best_answer, tags FROM rag_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if not entry_row:
            # 向量库有但 SQLite 没,可能数据不一致,跳过
            continue
        # store.py 已经返回了真实余弦相似度,直接用
        similarity = float(row.get("_similarity", 0))
        similarity = max(0.0, min(1.0, similarity))
        atts = storage.conn.execute(
            "SELECT id, kind, mime_type, original_name, size_bytes "
            "FROM attachments WHERE entry_id = ? ORDER BY id",
            (entry_id,),
        ).fetchall()
        candidates.append({
            "id": entry_row[0],
            "category": entry_row[1],
            "answer": entry_row[2],
            "tags": json.loads(entry_row[3]) if entry_row[3] else [],
            "similarity": round(similarity, 3),
            "matched_variant": row["variant_text"],
            "attachments": [{
                "id": a[0], "kind": a[1], "mime": a[2], "name": a[3], "size": a[4],
            } for a in atts],
        })
        if len(candidates) >= top_k:
            break

    # 埋点 RAG 检索结果
    try:
        from app.api_events import log_event
        if candidates:
            top = candidates[0]
            log_event("rag_retrieve", customer_id=customer_id, data={
                "query": query[:200],
                "top_similarity": top["similarity"],
                "top_category": top.get("category"),
                "top_entry_id": top.get("id"),
                "candidates_count": len(candidates),
            })
        else:
            log_event("rag_retrieve", customer_id=customer_id, data={
                "query": query[:200],
                "top_similarity": 0,
                "top_category": None,
                "candidates_count": 0,
            })
    except Exception:
        logger.exception("rag_retrieve 埋点失败")

    return candidates
