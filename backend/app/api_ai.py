"""AI 生成 (DeepSeek) 流式接口 + 客户草稿持久化。

- POST /api/ai/generate (SSE 流式) - 实时生成,完成时落库
- GET  /api/ai/draft/{customer_id} - 拿该客户最近一条草稿(切回客户时恢复)
- DELETE /api/ai/draft/{customer_id} - 清掉草稿(可选)
"""
import json
import logging
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import llm
from app.api_admin import get_prompt_content
from app.rag import retrieve as rag_retrieve
from app.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai")


class GenerateRequest(BaseModel):
    customer_id: str
    query: str
    max_chars: int = 100  # 客服在 sidebar 选: 100 / 200 / 300
    last_seq: int = 0     # 当前轮的 last_seq, 落库时一并保存(用于 conversation 判断 stale)


async def _build_rag_context(query: str, top_k: int = 5, customer_id: str = "") -> str:
    """检索现有话术 top_k,拼成给 prompt 用的参考块。"""
    try:
        cands = await rag_retrieve.retrieve(query, top_k=top_k, customer_id=customer_id)
    except Exception as e:
        logger.warning("RAG 检索失败: %s", e)
        return "(RAG 检索失败,本次无参考话术)"
    if not cands:
        return "(知识库目前没有相关话术,请按自己的判断回答)"
    blocks = []
    for i, c in enumerate(cands, 1):
        ans = (c.get("answer") or "").strip()
        cat = (c.get("category") or "").strip()
        sim = c.get("similarity", 0)
        var = (c.get("matched_variant") or "").strip()
        blocks.append(
            f"【参考 {i}】[{cat}] 相关度 {sim:.2f}\n"
            f"家长可能问: {var}\n"
            f"标准回答: {ans}"
        )
    return "\n\n".join(blocks)


async def _build_messages(prompt_template: str, query: str, max_chars: int, customer_id: str = "") -> list[dict]:
    """把 prompt template 用 context 字典做 .format 替换。
    未识别占位符自动降级成空串(不报错)。
    """
    rag_context = await _build_rag_context(query, top_k=5, customer_id=customer_id)
    context = {
        "query": (query or "").strip(),
        "max_chars": max_chars,
        "rag_context": rag_context,
        # 未来扩展时只需在此加键: "customer_recent_msgs": "...", prompt 里直接写 {customer_recent_msgs}
    }

    class _SafeDict(dict):
        def __missing__(self, key):
            return ""

    rendered = prompt_template.format_map(_SafeDict(**context))
    return [{"role": "user", "content": rendered}]


def _save_draft(customer_id: str, query: str, answer: str, last_seq: int = 0):
    storage = get_storage()
    storage.conn.execute(
        "INSERT INTO ai_drafts (customer_id, query, answer, last_seq, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(customer_id) DO UPDATE SET "
        "query=excluded.query, answer=excluded.answer, last_seq=excluded.last_seq, updated_at=excluded.updated_at",
        (customer_id, query, answer, last_seq, time.time()),
    )


@router.post("/generate")
async def generate(req: GenerateRequest):
    customer_id = (req.customer_id or "").strip()
    query = (req.query or "").strip()
    if not customer_id:
        raise HTTPException(400, "customer_id 不能为空")
    if not query:
        raise HTTPException(400, "query 不能为空")
    max_chars = req.max_chars if req.max_chars in (100, 200, 300) else 100

    template = get_prompt_content("ai_draft")
    if not template:
        raise HTTPException(500, "ai_draft prompt 未配置")

    messages = await _build_messages(template, query, max_chars, customer_id=customer_id)
    # 中文 1 字 ≈ 1.5-2 token,留 3x 余量给 reasoning + 答案
    max_tokens = max_chars * 3

    async def event_stream():
        full_answer = []
        try:
            async for ev in llm.stream_chat(messages, max_tokens=max_tokens):
                if ev["type"] == "delta":
                    full_answer.append(ev["content"])
                    yield f"data: {json.dumps({'type': 'delta', 'content': ev['content']}, ensure_ascii=False)}\n\n"
                elif ev["type"] == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': ev['message']}, ensure_ascii=False)}\n\n"
                    return
                elif ev["type"] == "done":
                    answer = ev.get("content") or "".join(full_answer)
                    if answer.strip():
                        try:
                            _save_draft(customer_id, query, answer, last_seq=req.last_seq)
                        except Exception:
                            logger.exception("save draft 失败")
                        # 埋点
                        try:
                            from app.api_events import log_event
                            log_event("draft_generated", customer_id=customer_id, data={
                                "source": "manual_regen",
                                "answer_length": len(answer),
                                "query_length": len(query),
                                "last_seq": req.last_seq,
                                "max_chars": max_chars,
                                "model": llm.get_active_model(),
                            })
                        except Exception:
                            logger.exception("draft_generated 埋点失败")
                    yield f"data: {json.dumps({'type': 'done', 'content': answer}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception("AI 生成出错")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx 关掉缓冲,流式才有意义
        },
    )


@router.get("/draft/{customer_id}")
async def get_draft(customer_id: str):
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT customer_id, query, answer, updated_at FROM ai_drafts WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "customer_id": row[0],
        "query": row[1],
        "answer": row[2],
        "updated_at": row[3],
    }


@router.delete("/draft/{customer_id}")
async def delete_draft(customer_id: str):
    storage = get_storage()
    storage.conn.execute("DELETE FROM ai_drafts WHERE customer_id = ?", (customer_id,))
    return {"deleted": customer_id}
