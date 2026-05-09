"""sidebar 用的接口: 拉客户最新消息 + 检索候选话术(真实向量 + Mock fallback)。"""
import json
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.rag import embed as rag_embed
from app.rag import retrieve as rag_retrieve
from app.rag import store as rag_store
from app.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter()


class RetrieveRequest(BaseModel):
    text: str


def _extract_text(msg_type: str, raw: dict) -> str:
    if msg_type == "text":
        return raw.get("text", {}).get("content", "[空文本]")
    if msg_type == "voice":
        return "[语音消息]"
    if msg_type == "image":
        return "[图片]"
    if msg_type == "sphfeed":
        feed = raw.get("sphfeed", {}) or {}
        return f"[视频号 {feed.get('sph_name', '')}] {feed.get('feed_desc', '')[:80]}"
    if msg_type == "link":
        link = raw.get("link", {}) or {}
        return f"[链接 {link.get('title', '')}]"
    if msg_type == "video":
        return "[视频]"
    if msg_type == "file":
        return "[文件]"
    return f"[{msg_type}]"


@router.get("/api/customer/{external_userid}/latest_message")
async def customer_latest_message(external_userid: str, to_user: str = ""):
    """单条版本(向后兼容)。"""
    storage = get_storage()
    if to_user:
        sql = (
            "SELECT seq, msgid, received_at, from_user, msg_type, raw_json "
            "FROM chat_messages "
            "WHERE from_user = ? AND to_users LIKE ? "
            "ORDER BY seq DESC LIMIT 1"
        )
        params = (external_userid, f'%"{to_user}"%')
    else:
        sql = (
            "SELECT seq, msgid, received_at, from_user, msg_type, raw_json "
            "FROM chat_messages "
            "WHERE from_user = ? "
            "ORDER BY seq DESC LIMIT 1"
        )
        params = (external_userid,)
    row = storage.conn.execute(sql, params).fetchone()
    if not row:
        return {"found": False}
    raw = json.loads(row[5])
    msg_type = row[4]
    return {
        "found": True,
        "seq": row[0],
        "msgid": row[1],
        "received_at": row[2],
        "msg_type": msg_type,
        "text": _extract_text(msg_type, raw),
    }


def compute_rounds(
    external_userid: str,
    to_user: str = "",
    max_rounds: int = 10,
    history_window: int = 200,
) -> list[dict]:
    """切分某客户跟客服的对话为"轮次",返回 rounds list。

    一个"轮次"= 客户连续发的一组消息 + 之后客服的回复(如有)。
    rounds[0] = 最新轮次(可能未回复),rounds[1] = 上一轮(已回复)...
    供 sidebar API 和后台自动 AI 草稿任务复用。
    """
    storage = get_storage()
    if not to_user:
        sql = (
            "SELECT seq, received_at, from_user, msg_type, raw_json "
            "FROM chat_messages WHERE from_user = ? ORDER BY seq DESC LIMIT ?"
        )
        rows = storage.conn.execute(sql, (external_userid, history_window)).fetchall()
    else:
        sql = (
            "SELECT seq, received_at, from_user, msg_type, raw_json "
            "FROM chat_messages "
            "WHERE (from_user = ? AND to_users LIKE ?) "
            "   OR (from_user = ? AND to_users LIKE ?) "
            "ORDER BY seq DESC LIMIT ?"
        )
        rows = storage.conn.execute(
            sql,
            (external_userid, f'%"{to_user}"%', to_user, f'%"{external_userid}"%', history_window),
        ).fetchall()
    if not rows:
        return []
    msgs = list(reversed(rows))
    rounds = []
    current_parent_msgs = []

    def flush_round(responded: bool):
        if not current_parent_msgs:
            return
        merged = "\n".join(m["text"] for m in current_parent_msgs)
        rounds.append({
            "messages": current_parent_msgs.copy(),
            "merged_text": merged,
            "first_at": current_parent_msgs[0]["received_at"],
            "last_at": current_parent_msgs[-1]["received_at"],
            "first_seq": current_parent_msgs[0]["seq"],
            "last_seq": current_parent_msgs[-1]["seq"],
            "count": len(current_parent_msgs),
            "responded": responded,
        })

    for row in msgs:
        seq, received_at, from_user, msg_type, raw_json = row
        raw = json.loads(raw_json)
        text = _extract_text(msg_type, raw)
        if from_user == external_userid:
            current_parent_msgs.append({
                "seq": seq,
                "received_at": received_at,
                "msg_type": msg_type,
                "text": text,
            })
        else:
            flush_round(responded=True)
            current_parent_msgs = []
    flush_round(responded=False)
    rounds.reverse()
    return rounds[:max_rounds]


@router.get("/api/customer/{external_userid}/conversation")
async def customer_conversation(
    external_userid: str,
    to_user: str = "",
    max_rounds: int = 10,
    history_window: int = 200,
):
    """按"轮次"切分客户和指定客服的对话,顺带返回 ai_draft (如果对应最新轮次)。"""
    rounds = compute_rounds(external_userid, to_user, max_rounds, history_window)
    ai_draft = None
    if rounds:
        storage = get_storage()
        row = storage.conn.execute(
            "SELECT query, answer, last_seq, updated_at FROM ai_drafts WHERE customer_id = ?",
            (external_userid,),
        ).fetchone()
        if row and row[1]:  # 草稿存在 - 总是返回,前端按 stale 自己显示
            current_last = rounds[0].get("last_seq", 0)
            ai_draft = {
                "query": row[0],
                "answer": row[1],
                "last_seq": row[2] or 0,
                "updated_at": row[3],
                "stale": (row[2] or 0) != current_last,
                "current_last_seq": current_last,
            }
    return {"rounds": rounds, "total_rounds": len(rounds), "ai_draft": ai_draft}


# ========== Mock 候选 (Session 2 替换为真向量检索) ==========

_MOCK_TEMPLATES = [
    {
        "id": "tpl_refund",
        "category": "退费咨询",
        "answer": "家长您好~ 关于退费政策,我们的规则是开课 7 天内可全额退费,超过 7 天则按已学课时扣除后退余款。您可以告诉我下您的报名时间和已上课时,我帮您具体核算下哈。",
        "tags": ["退费", "售后"],
    },
    {
        "id": "tpl_renew",
        "category": "续费咨询",
        "answer": "亲~ 续费有专属优惠哦,老学员续报享 9 折,推荐 1 位新学员还可以再减 200 元。您是想续多少课时呢?我给您算下最划算的方案。",
        "tags": ["续费", "优惠"],
    },
    {
        "id": "tpl_emotion",
        "category": "孩子状态",
        "answer": "理解您的担心~ 孩子出现这种情绪在学习过程中其实是常见的,我们老师课后也会重点关注一下。建议这周末可以来咱们机构参加一次免费的家长答疑会,老师会跟您详细聊聊孩子最近的状态和应对方法。",
        "tags": ["家校沟通"],
    },
    {
        "id": "tpl_schedule",
        "category": "课程时间",
        "answer": "好的~ 这周末的课时间是周六上午 10:00-11:30,周日下午 14:00-15:30。地址还是咱们 XX 校区。如有变动我会提前告知您哈。",
        "tags": ["课表"],
    },
    {
        "id": "tpl_leave",
        "category": "请假",
        "answer": "好的~ 已记录孩子本次请假。请假课时可以在课程结束前补回(线上或线下都可),不影响课程进度。希望孩子早日恢复哈,有任何变化随时联系我。",
        "tags": ["请假"],
    },
]


@router.post("/api/retrieve_mock")
async def retrieve_mock(req: RetrieveRequest):
    return {
        "query": req.text,
        "is_mock": True,
        "candidates": [
            {**_MOCK_TEMPLATES[0], "similarity": 0.92},
            {**_MOCK_TEMPLATES[1], "similarity": 0.85},
            {**_MOCK_TEMPLATES[2], "similarity": 0.74},
        ],
    }


@router.post("/api/retrieve")
async def retrieve(req: RetrieveRequest):
    """真实向量检索。库为空就 fallback 到 mock,避免 sidebar 显示空。"""
    try:
        vec_count = await rag_store.count()
    except Exception as e:
        logger.exception("LanceDB 查询失败")
        vec_count = 0

    if vec_count == 0:
        # 知识库为空,降级到 mock,提示客服去管理后台录入
        return {
            "query": req.text,
            "is_mock": True,
            "is_empty_db": True,
            "candidates": [
                {**_MOCK_TEMPLATES[0], "similarity": 0.92},
                {**_MOCK_TEMPLATES[1], "similarity": 0.85},
                {**_MOCK_TEMPLATES[2], "similarity": 0.74},
            ],
        }

    try:
        candidates = await rag_retrieve.retrieve(req.text, top_k=4, customer_id=getattr(req, "customer_id", "") or "")
    except Exception as e:
        logger.exception("RAG 检索失败")
        return {
            "query": req.text,
            "is_mock": False,
            "error": str(e),
            "candidates": [],
        }

    return {
        "query": req.text,
        "is_mock": False,
        "candidates": candidates,
    }


# ========== Dev: 一键塞 mock 数据进真实 RAG 库(用于 Stage A 联调) ==========


@router.post("/api/dev/seed_mock_rag")
async def dev_seed_mock_rag():
    """把 5 条 mock 当作 RAG entry 入库,带 variants。仅供 Stage A 验证使用。"""
    storage = get_storage()
    inserted = []

    seeds = [
        {
            "category": "退费咨询",
            "best_answer": _MOCK_TEMPLATES[0]["answer"],
            "tags": ["退费", "售后"],
            "variants": ["想退费", "能退钱吗", "申请退课", "我要退费", "想退课"],
        },
        {
            "category": "续费咨询",
            "best_answer": _MOCK_TEMPLATES[1]["answer"],
            "tags": ["续费", "优惠"],
            "variants": ["想续课", "续费有什么优惠", "继续报名", "续费多少钱"],
        },
        {
            "category": "孩子状态",
            "best_answer": _MOCK_TEMPLATES[2]["answer"],
            "tags": ["家校沟通"],
            "variants": ["孩子最近不想上课", "孩子有点抗拒", "孩子情绪不好", "孩子哭闹"],
        },
        {
            "category": "课程时间",
            "best_answer": _MOCK_TEMPLATES[3]["answer"],
            "tags": ["课表"],
            "variants": ["这周课什么时间", "课表是什么", "几点上课", "周末有课吗"],
        },
        {
            "category": "请假",
            "best_answer": _MOCK_TEMPLATES[4]["answer"],
            "tags": ["请假"],
            "variants": ["孩子今天请假", "孩子病了来不了", "今天不能上课"],
        },
    ]

    for seed in seeds:
        cur = storage.conn.execute(
            "INSERT INTO rag_entries (category, best_answer, tags, source, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                seed["category"],
                seed["best_answer"],
                json.dumps(seed["tags"], ensure_ascii=False),
                "dev_seed",
                "system",
            ),
        )
        entry_id = cur.lastrowid
        # embed 所有 variants
        try:
            vectors = await rag_embed.embed(seed["variants"])
            await rag_store.add_variants(entry_id, seed["variants"], vectors)
            inserted.append({"entry_id": entry_id, "variant_count": len(seed["variants"])})
        except Exception as e:
            logger.exception("seed entry %s 失败", entry_id)
            inserted.append({"entry_id": entry_id, "error": str(e)})

    total_vectors = await rag_store.count()
    return {
        "inserted": inserted,
        "total_vectors_in_db": total_vectors,
    }
