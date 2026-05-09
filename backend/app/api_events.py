"""埋点和统计聚合。

埋点:
- POST /api/events/log     - 客户端 (sidebar) 调用,埋"采用 / 重新生成"等动作
- log_event(event_type, customer_id, staff_id, data) - 后端内部直接调

统计:
- GET /api/admin/stats     - admin 后台拉数据(带 auth)
"""
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.admin_auth import get_admin
from app.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter()


def log_event(event_type: str, customer_id: str = "", staff_id: str = "", data: dict | None = None):
    """后端内部埋点(同步,失败不抛)。"""
    try:
        storage = get_storage()
        storage.conn.execute(
            "INSERT INTO events (event_type, customer_id, staff_id, data) VALUES (?, ?, ?, ?)",
            (event_type, customer_id or "", staff_id or "", json.dumps(data or {}, ensure_ascii=False)),
        )
    except Exception:
        logger.exception("埋点失败 type=%s", event_type)


class EventLogRequest(BaseModel):
    event_type: str
    customer_id: str = ""
    staff_id: str = ""
    data: dict = {}


@router.post("/api/events/log")
async def events_log(req: EventLogRequest):
    """sidebar 客户端调用,无 admin auth。"""
    if not req.event_type:
        raise HTTPException(400, "event_type 不能为空")
    log_event(req.event_type, req.customer_id, req.staff_id, req.data)
    return {"ok": True}


def _today_start_ts() -> float:
    """北京时间今天 0:00 的 unix 时间戳。"""
    # SQLite 默认 UTC,北京 = UTC+8。北京 0:00 = UTC 16:00 of 前一天。
    # 用 Python 算更可靠:
    import datetime as _dt
    now_utc = _dt.datetime.utcnow()
    cn = now_utc + _dt.timedelta(hours=8)
    cn_today_0 = cn.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_today_0 = cn_today_0 - _dt.timedelta(hours=8)
    return utc_today_0.timestamp()


def _week_start_ts() -> float:
    """北京时间本周一 0:00。"""
    import datetime as _dt
    now_utc = _dt.datetime.utcnow()
    cn = now_utc + _dt.timedelta(hours=8)
    monday = cn - _dt.timedelta(days=cn.weekday())
    monday_0 = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return (monday_0 - _dt.timedelta(hours=8)).timestamp()


@router.get("/api/admin/stats")
async def admin_stats(admin: dict = Depends(get_admin)):
    storage = get_storage()
    today = _today_start_ts()
    week = _week_start_ts()

    def n(sql, *args):
        row = storage.conn.execute(sql, args).fetchone()
        return row[0] if row and row[0] is not None else 0

    def block(since: float) -> dict:
        # 客服 = from_user 不是外部联系人(企微外部联系人 ID 都以 wm 开头)
        replies = n(
            "SELECT COUNT(*) FROM chat_messages WHERE received_at >= ? AND msg_type='text' AND from_user NOT LIKE 'wm%'",
            since,
        )
        adopted = n("SELECT COUNT(*) FROM events WHERE event_type='draft_adopt' AND created_at >= ?", since)
        regen = n("SELECT COUNT(*) FROM events WHERE event_type='draft_regen' AND created_at >= ?", since)
        gen = n("SELECT COUNT(*) FROM events WHERE event_type='draft_generated' AND created_at >= ?", since)
        rag_avg = n("SELECT AVG(CAST(json_extract(data,'$.top_similarity') AS REAL)) FROM events WHERE event_type='rag_retrieve' AND created_at >= ?", since)
        rag_high = n("SELECT COUNT(*) FROM events WHERE event_type='rag_retrieve' AND CAST(json_extract(data,'$.top_similarity') AS REAL) >= 0.85 AND created_at >= ?", since)
        rag_total = n("SELECT COUNT(*) FROM events WHERE event_type='rag_retrieve' AND created_at >= ?", since)
        # 一字不差采用 = 客服在"采用事件"后 120 秒内,发出的某条消息内容 == 采用时的 answer
        verbatim = n(
            "SELECT COUNT(*) FROM events e "
            "WHERE e.event_type='draft_adopt' AND e.created_at >= ? "
            "AND EXISTS ("
            "  SELECT 1 FROM chat_messages c "
            "  WHERE c.from_user NOT LIKE 'wm%' AND c.msg_type='text' "
            "    AND c.to_users LIKE '%' || e.customer_id || '%' "
            "    AND c.received_at >= e.created_at "
            "    AND c.received_at < e.created_at + 120 "
            "    AND json_extract(c.raw_json,'$.text.content') = json_extract(e.data,'$.answer') "
            ")",
            since,
        )
        return {
            "staff_replies": replies,
            "ai_generated": gen,
            "ai_adopted": adopted,
            "ai_adopted_verbatim": verbatim,
            "ai_regenerated": regen,
            "adopt_rate": round(adopted / gen, 3) if gen else 0,
            "verbatim_rate": round(verbatim / replies, 3) if replies else 0,
            "rag_avg_score": round(float(rag_avg), 3) if rag_avg else 0,
            "rag_high_match_rate": round(rag_high / rag_total, 3) if rag_total else 0,
            "rag_total": rag_total,
        }

    today_data = block(today)
    week_data = block(week)

    # 本周家长最高频问的 Top 10 分类(按 RAG 命中的 category 聚合)
    top_categories = storage.conn.execute(
        "SELECT json_extract(data,'$.top_category') AS cat, COUNT(*) AS n "
        "FROM events WHERE event_type='rag_retrieve' AND created_at >= ? "
        "  AND json_extract(data,'$.top_category') IS NOT NULL "
        "GROUP BY cat ORDER BY n DESC LIMIT 10",
        (week,),
    ).fetchall()

    # 本周低命中 Top 20 query (< 0.7 分,知识库缺口)
    low_match = storage.conn.execute(
        "SELECT json_extract(data,'$.query') AS q, "
        "       CAST(json_extract(data,'$.top_similarity') AS REAL) AS sim, "
        "       MAX(created_at) AS last_t, "
        "       COUNT(*) AS times "
        "FROM events WHERE event_type='rag_retrieve' AND created_at >= ? "
        "  AND CAST(json_extract(data,'$.top_similarity') AS REAL) < 0.7 "
        "  AND json_extract(data,'$.query') IS NOT NULL "
        "GROUP BY q ORDER BY times DESC, last_t DESC LIMIT 20",
        (week,),
    ).fetchall()

    return {
        "today": today_data,
        "this_week": week_data,
        "top_categories": [{"category": c, "count": n} for c, n in top_categories if c],
        "low_match_queries": [
            {"query": q, "best_score": round(float(s), 3) if s else 0, "times": times, "last_at": last_t}
            for q, s, last_t, times in low_match if q
        ],
    }
