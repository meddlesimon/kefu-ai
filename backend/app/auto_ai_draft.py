"""家长发完消息后,5 秒防抖,后台自动跑 RAG + LLM 生成推荐话术存到 ai_drafts。

调用链:
  archive_pull 拉到家长消息 → schedule_for_customer(external_userid)
                                 ↓
                    取消该客户旧的延迟任务,起新的 5 秒延时
                                 ↓ 5s 后
                    compute_rounds 拿最新一轮的 merged_text 作为 query
                                 ↓
                    检查 ai_drafts 表 last_seq 跟当前 last_seq 是否一致
                                 ↓ 不一致(新轮)
                    调 LLM (含 RAG 上下文) 生成回答
                                 ↓
                    落库 ai_drafts (按 customer_id 覆盖)
"""
import asyncio
import json
import logging
import time
from typing import Optional

from app.storage import get_storage

logger = logging.getLogger(__name__)

DEBOUNCE_SECS = 5
DEFAULT_MAX_CHARS = 100
MIN_TEXT_LEN = 1  # 任何非空 text 都跑 (家长真问题可能 2-4 字: "在哪""怎么办")

_pending: dict[str, asyncio.Task] = {}
_lock = asyncio.Lock()


async def schedule_for_customer(external_userid: str, to_user: Optional[str] = None):
    """家长发完消息时调一次,5 秒后才真的跑生成,期间又来新消息会重新延时。"""
    if not external_userid:
        return
    async with _lock:
        old = _pending.get(external_userid)
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(_delayed_run(external_userid, to_user))
        _pending[external_userid] = task


async def _delayed_run(external_userid: str, to_user: Optional[str]):
    try:
        await asyncio.sleep(DEBOUNCE_SECS)
    except asyncio.CancelledError:
        return

    # 进入 LLM 阶段: 从 _pending 移除自己,避免之后再来新消息时被 cancel 中途打断。
    # LLM 跑完落库 → 这个客户的下一次 schedule 会起新任务覆盖最新草稿。
    async with _lock:
        cur = _pending.get(external_userid)
        if cur is asyncio.current_task():
            _pending.pop(external_userid, None)

    logger.info("[auto-draft] _delayed_run 开跑 customer=%s", external_userid)
    try:
        await _run_once(external_userid, to_user)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[auto-draft] 失败 customer=%s", external_userid)


async def _run_once(external_userid: str, to_user: Optional[str]):
    from app.api_sidebar import compute_rounds
    from app.api_admin import get_prompt_content
    from app.api_ai import _build_messages
    from app import llm

    # 1. 拿最新一轮 (不指定 to_user 时,把这个家长发的所有未回复消息当一轮)
    rounds = compute_rounds(external_userid, to_user or "", max_rounds=1, history_window=80)
    if not rounds:
        logger.info("[auto-draft] 跳过 %s: 无 rounds(可能 to_user 不匹配)", external_userid)
        return
    latest = rounds[0]
    if latest.get("responded"):
        logger.info("[auto-draft] 跳过 %s: 最新轮已被客服回复", external_userid)
        return
    query = (latest.get("merged_text") or "").strip()
    if len(query) < MIN_TEXT_LEN:
        logger.info("[auto-draft] 跳过 %s: 内容太短(%d 字)", external_userid, len(query))
        return

    last_seq = latest.get("last_seq", 0)

    # 2. 检查 ai_drafts: 同一客户 last_seq 一致 → 已生成过,跳过
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT last_seq FROM ai_drafts WHERE customer_id = ?", (external_userid,)
    ).fetchone()
    if row and row[0] >= last_seq:
        logger.info("[auto-draft] 跳过 %s: 库内 last_seq=%s ≥ 当前 %s,已有同样新或更新的草稿", external_userid, row[0], last_seq)
        return
    logger.info("[auto-draft] 开始生成 %s last_seq=%s query='%s'", external_userid, last_seq, query[:50])

    # 3. 拼 prompt,跑 LLM
    template = get_prompt_content("ai_draft")
    if not template:
        logger.warning("ai_draft prompt 未配置,跳过自动草稿")
        return
    try:
        messages = await _build_messages(template, query, DEFAULT_MAX_CHARS, customer_id=external_userid)
    except Exception:
        logger.exception("build messages 失败")
        return

    full_chunks: list[str] = []
    try:
        async for ev in llm.stream_chat(messages, max_tokens=DEFAULT_MAX_CHARS * 3):
            if ev["type"] == "delta":
                full_chunks.append(ev["content"])
            elif ev["type"] == "done":
                if ev.get("content"):
                    full_chunks = [ev["content"]]
                break
            elif ev["type"] == "error":
                logger.warning("auto draft LLM 失败 %s: %s", external_userid, ev["message"])
                return
    except Exception:
        logger.exception("auto draft LLM 异常")
        return

    answer = "".join(full_chunks).strip() if isinstance(full_chunks, list) else str(full_chunks)
    if not answer:
        return

    # 4. 落库 (按 customer_id 覆盖)
    storage.conn.execute(
        "INSERT INTO ai_drafts (customer_id, query, answer, last_seq, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(customer_id) DO UPDATE SET "
        "query=excluded.query, answer=excluded.answer, last_seq=excluded.last_seq, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (external_userid, query, answer, last_seq, time.time(), "auto"),
    )
    logger.info("auto draft 完成 customer=%s last_seq=%s ans=%d 字", external_userid, last_seq, len(answer))
    # 埋点(llm 在函数顶部已 import)
    try:
        from app.api_events import log_event
        log_event("draft_generated", customer_id=external_userid, data={
            "source": "auto",
            "answer_length": len(answer),
            "query_length": len(query),
            "last_seq": last_seq,
            "model": llm.get_active_model(),
        })
    except Exception:
        logger.exception("draft_generated 埋点失败")
