"""管理员后台 API: RAG CRUD + 用户管理。"""
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.admin_auth import get_admin, hash_password
from app.rag import answer_store
from app.rag import embed as rag_embed
from app.rag import expand as rag_expand
from app.rag import parser as rag_parser
from app.rag import store as rag_store
from app.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")


# ========== RAG ==========

class IngestRequest(BaseModel):
    text: str
    auto_expand: bool = True
    source: Optional[str] = "ingest"
    mode: str = "structured"  # "structured" (分类|问法+答案) | "smart" (只贴答案,LLM 自动生成)


class IngestResponse(BaseModel):
    parsed_count: int
    inserted_count: int
    total_variants_in_db: int
    inserted_entries: List[dict] = []
    errors: List[str] = []


def _parse_smart_text(text: str) -> List[str]:
    """智能模式: 把粘贴文本按空行拆成多段答案。"""
    text = (text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return []
    # 用一个或多个空行分块
    blocks = []
    cur = []
    for line in text.split("\n"):
        if line.strip() == "":
            if cur:
                blocks.append("\n".join(cur).strip())
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur).strip())
    return [b for b in blocks if b]


@router.post("/rag/ingest", response_model=IngestResponse)
async def rag_ingest(req: IngestRequest, admin: dict = Depends(get_admin)):
    """粘贴文本 → 解析 → (可选 LLM 扩 variants) → embed → 入库。

    mode:
      - structured: 第一行 "分类|问法1|问法2", 之后行是答案 (现有格式)
      - smart: 整段就是答案, LLM 自动推分类 + 5 个问法
    """
    if req.mode == "smart":
        answers = _parse_smart_text(req.text)
        if not answers:
            raise HTTPException(status_code=400, detail="解析为 0 条,请粘贴答案文本(多条用空行分隔)")
        entries = []
        smart_errors = []
        for ans in answers:
            try:
                gen = await rag_expand.auto_categorize(ans, target_count=5)
                entries.append({
                    "category": gen["category"],
                    "variants": gen["variants"],
                    "best_answer": ans,
                })
            except Exception as e:
                logger.exception("smart 模式 LLM 失败: %s", ans[:50])
                smart_errors.append(f"答案前 30 字 [{ans[:30]}…]: {e}")
        if not entries:
            return IngestResponse(
                parsed_count=len(answers),
                inserted_count=0,
                total_variants_in_db=await rag_store.count(),
                inserted_entries=[],
                errors=smart_errors,
            )
    else:
        entries = rag_parser.parse_ingest_text(req.text)
        smart_errors = []
        if not entries:
            raise HTTPException(status_code=400, detail="解析为 0 条,请检查格式(分类|问法1|问法2 + 答案)")

    storage = get_storage()
    inserted = 0
    errors = list(smart_errors)
    inserted_entries = []
    for entry in entries:
        try:
            variants = entry["variants"]
            if req.mode != "smart" and req.auto_expand and len(variants) < 5:
                variants = await rag_expand.expand_variants(
                    variants, entry["best_answer"], target_count=5
                )

            cur = storage.conn.execute(
                "INSERT INTO rag_entries (category, best_answer, tags, source, created_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry["category"],
                    entry["best_answer"],
                    json.dumps([], ensure_ascii=False),
                    req.source or "ingest",
                    admin["username"],
                ),
            )
            entry_id = cur.lastrowid

            vectors = await rag_embed.embed(variants)
            await rag_store.add_variants(entry_id, variants, vectors)
            inserted += 1
            inserted_entries.append({
                "id": entry_id,
                "category": entry["category"],
                "variant_count": len(variants),
                "expanded": len(variants) > len(entry["variants"]),
            })
        except Exception as e:
            logger.exception("ingest entry 失败: %s", entry.get("category"))
            errors.append(f"{entry.get('category', '???')}: {e}")

    total = await rag_store.count()
    return IngestResponse(
        parsed_count=len(entries),
        inserted_count=inserted,
        total_variants_in_db=total,
        inserted_entries=inserted_entries,
        errors=errors,
    )


@router.get("/rag/list")
async def rag_list(admin: dict = Depends(get_admin)):
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT e.id, e.category, e.best_answer, e.tags, e.source, e.created_by, e.created_at, "
        "(SELECT COUNT(*) FROM rag_variants v WHERE v.entry_id = e.id) AS variant_count "
        "FROM rag_entries e ORDER BY e.id DESC"
    ).fetchall()
    return [{
        "id": r[0],
        "category": r[1],
        "best_answer": r[2],
        "tags": json.loads(r[3]) if r[3] else [],
        "source": r[4],
        "created_by": r[5],
        "created_at": r[6],
        "variant_count": r[7],
    } for r in rows]


@router.get("/rag/{entry_id}")
async def rag_get(entry_id: int, admin: dict = Depends(get_admin)):
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT id, category, best_answer, tags, source, created_by, created_at "
        "FROM rag_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "条目不存在")
    variants = storage.conn.execute(
        "SELECT variant_text FROM rag_variants WHERE entry_id = ? ORDER BY rowid",
        (entry_id,),
    ).fetchall()
    return {
        "id": row[0],
        "category": row[1],
        "best_answer": row[2],
        "tags": json.loads(row[3]) if row[3] else [],
        "source": row[4],
        "created_by": row[5],
        "created_at": row[6],
        "variants": [v[0] for v in variants],
    }


class UpdateEntryRequest(BaseModel):
    category: str
    best_answer: str
    variants: List[str]


@router.put("/rag/{entry_id}")
async def rag_update(entry_id: int, req: UpdateEntryRequest, admin: dict = Depends(get_admin)):
    category = req.category.strip()
    best_answer = req.best_answer.strip()
    # 去空白行 + 去重保序
    seen = set()
    variants = []
    for v in req.variants:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)
    if not category:
        raise HTTPException(400, "分类不能为空")
    if not best_answer:
        raise HTTPException(400, "答案不能为空")
    if not variants:
        raise HTTPException(400, "至少要有一个问法变体")

    storage = get_storage()
    row = storage.conn.execute("SELECT id FROM rag_entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "条目不存在")

    storage.conn.execute(
        "UPDATE rag_entries SET category = ?, best_answer = ? WHERE id = ?",
        (category, best_answer, entry_id),
    )
    # 全量替换 variants:删旧 + 重新 embed + 写新
    await rag_store.delete_by_entry(entry_id)
    vectors = await rag_embed.embed(variants)
    await rag_store.add_variants(entry_id, variants, vectors)

    return {
        "id": entry_id,
        "category": category,
        "best_answer": best_answer,
        "variants": variants,
    }


@router.delete("/rag/{entry_id}")
async def rag_delete(entry_id: int, admin: dict = Depends(get_admin)):
    storage = get_storage()
    row = storage.conn.execute("SELECT id FROM rag_entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "条目不存在")
    storage.conn.execute("DELETE FROM rag_entries WHERE id = ?", (entry_id,))
    await rag_store.delete_by_entry(entry_id)
    return {"deleted": entry_id}


@router.get("/rag/stats")
async def rag_stats(admin: dict = Depends(get_admin)):
    storage = get_storage()
    entry_count = storage.conn.execute("SELECT COUNT(*) FROM rag_entries").fetchone()[0]
    variant_count = await rag_store.count()
    return {"entry_count": entry_count, "variant_count": variant_count}


# ========== Users ==========

class CreateUserRequest(BaseModel):
    username: str
    password: str


@router.get("/users")
async def users_list(admin: dict = Depends(get_admin)):
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT username, role, created_at, created_by FROM admins ORDER BY role DESC, username"
    ).fetchall()
    return [{
        "username": r[0],
        "role": r[1],
        "created_at": r[2],
        "created_by": r[3],
    } for r in rows]


@router.post("/users")
async def users_create(req: CreateUserRequest, admin: dict = Depends(get_admin)):
    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(400, "用户名和密码不能为空")
    storage = get_storage()
    if storage.conn.execute("SELECT 1 FROM admins WHERE username = ?", (username,)).fetchone():
        raise HTTPException(409, "用户名已存在")
    storage.conn.execute(
        "INSERT INTO admins (username, password_hash, role, created_by) VALUES (?, ?, ?, ?)",
        (username, hash_password(req.password), "normal", admin["username"]),
    )
    return {"created": username}


# ========== 话术挖掘 候选 CRUD ==========

@router.post("/candidates/scan")
async def candidates_scan(admin: dict = Depends(get_admin)):
    """手动触发扫描今日客服回复,挖掘有价值话术放进候选池。"""
    import time
    from app import candidate_miner
    from app.api_events import _today_start_ts
    since = _today_start_ts()
    until = time.time()
    try:
        stats = await candidate_miner.scan(since, until)
        return stats
    except Exception as e:
        logger.exception("扫描候选失败")
        raise HTTPException(500, f"扫描失败: {e}")


@router.get("/candidates")
async def candidates_list(status: str = "pending", admin: dict = Depends(get_admin)):
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT id, parent_query, staff_reply, cleaned_reply, suggested_category, "
        "       suggested_variants, llm_score, llm_reason, source_seq, customer_id, "
        "       status, reviewed_by, reviewed_at, rag_entry_id, created_at, "
        "       suggested_merge_entry_id, answer_match_similarity, similar_top_n_cached "
        "FROM candidate_phrases WHERE status = ? ORDER BY llm_score DESC, created_at DESC",
        (status,),
    ).fetchall()

    # 一次性查所有被建议合并的 entry,免得 N+1
    entry_ids = {r[15] for r in rows if r[15]}
    entry_map = {}
    if entry_ids:
        ph = ",".join("?" * len(entry_ids))
        for eid, cat, ans in storage.conn.execute(
            f"SELECT id, category, best_answer FROM rag_entries WHERE id IN ({ph})",
            tuple(entry_ids),
        ).fetchall():
            preview = (ans or "")
            if len(preview) > 80:
                preview = preview[:80] + "…"
            entry_map[eid] = {
                "entry_id": eid,
                "category": cat,
                "best_answer_preview": preview,
            }

    out = []
    for r in rows:
        sug_merge = None
        if r[15] and r[15] in entry_map:
            sug_merge = dict(entry_map[r[15]])
            sug_merge["similarity"] = r[16]
        sim_top_n = []
        if r[17]:
            try:
                sim_top_n = json.loads(r[17])
            except Exception:
                sim_top_n = []
        out.append({
            "id": r[0],
            "parent_query": r[1],
            "staff_reply": r[2],
            "cleaned_reply": r[3],
            "suggested_category": r[4],
            "suggested_variants": json.loads(r[5]) if r[5] else [],
            "llm_score": r[6],
            "llm_reason": r[7],
            "source_seq": r[8],
            "customer_id": r[9],
            "status": r[10],
            "reviewed_by": r[11],
            "reviewed_at": r[12],
            "rag_entry_id": r[13],
            "created_at": r[14],
            "suggested_merge": sug_merge,
            "similar_top_n": sim_top_n,
        })
    return out


class AdoptCandidateRequest(BaseModel):
    category: str | None = None    # 允许 admin 改
    answer: str | None = None      # 允许 admin 改 (默认用 cleaned_reply)
    variants: List[str] | None = None


@router.post("/candidates/{cid}/adopt")
async def candidates_adopt(cid: int, req: AdoptCandidateRequest, admin: dict = Depends(get_admin)):
    """采纳候选 → 写入 RAG 库。"""
    import time as _time
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT parent_query, staff_reply, cleaned_reply, suggested_category, "
        "       suggested_variants, status FROM candidate_phrases WHERE id = ?",
        (cid,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "候选不存在")
    if row[5] != "pending":
        raise HTTPException(400, f"已处理过(status={row[5]})")
    parent_q, raw_reply, cleaned, sug_cat, sug_vars_json, _ = row

    category = (req.category or sug_cat or "未分类").strip()
    answer = (req.answer or cleaned or raw_reply).strip()
    if req.variants is not None:
        variants = [v.strip() for v in req.variants if v and v.strip()]
    else:
        # 默认: 把家长原 query + LLM 推的 4 个变体都加进去
        sug_vars = json.loads(sug_vars_json) if sug_vars_json else []
        variants = [parent_q] + sug_vars
    # 去重
    seen = set()
    uniq_vars = []
    for v in variants:
        k = v.lower()
        if k not in seen and v:
            seen.add(k)
            uniq_vars.append(v)
    if not uniq_vars or not answer:
        raise HTTPException(400, "answer 或 variants 为空")

    # 写入 RAG entry + variants
    cur = storage.conn.execute(
        "INSERT INTO rag_entries (category, best_answer, tags, source, created_by) VALUES (?, ?, ?, ?, ?)",
        (category, answer, json.dumps([], ensure_ascii=False), "candidate-mining", admin["username"]),
    )
    entry_id = cur.lastrowid

    # 跑 embedding 入向量库
    vectors = await rag_embed.embed(uniq_vars)
    await rag_store.add_variants(entry_id, uniq_vars, vectors)

    # 答案侧向量(答案去重用,失败不阻断 adopt)
    try:
        answer_vec = await rag_embed.embed_one(answer)
        await answer_store.add_answer_vector(entry_id, answer_vec)
    except Exception:
        logger.exception("写答案向量失败 entry_id=%s (不阻断 adopt)", entry_id)

    # 标记候选为 adopted
    storage.conn.execute(
        "UPDATE candidate_phrases SET status='adopted', reviewed_by=?, reviewed_at=?, rag_entry_id=? WHERE id = ?",
        (admin["username"], _time.time(), entry_id, cid),
    )
    return {"adopted": cid, "rag_entry_id": entry_id, "category": category, "variants_count": len(uniq_vars)}


@router.post("/candidates/{cid}/ignore")
async def candidates_ignore(cid: int, admin: dict = Depends(get_admin)):
    import time as _time
    storage = get_storage()
    row = storage.conn.execute("SELECT status FROM candidate_phrases WHERE id = ?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "候选不存在")
    storage.conn.execute(
        "UPDATE candidate_phrases SET status='ignored', reviewed_by=?, reviewed_at=? WHERE id = ?",
        (admin["username"], _time.time(), cid),
    )
    return {"ignored": cid}


class MergeCandidateRequest(BaseModel):
    entry_id: int


@router.post("/candidates/{cid}/merge")
async def candidates_merge(cid: int, req: MergeCandidateRequest, admin: dict = Depends(get_admin)):
    """把候选的 parent_query 作为新 variant 追加到已存在 entry,候选标记 merged。"""
    import time as _time
    storage = get_storage()
    cand = storage.conn.execute(
        "SELECT parent_query, status FROM candidate_phrases WHERE id = ?", (cid,)
    ).fetchone()
    if not cand:
        raise HTTPException(404, "候选不存在")
    parent_q, status = cand
    if status != "pending":
        raise HTTPException(400, f"候选已处理(status={status})")
    if not parent_q or not parent_q.strip():
        raise HTTPException(400, "parent_query 为空,无法作为 variant")

    entry = storage.conn.execute(
        "SELECT id FROM rag_entries WHERE id = ?", (req.entry_id,)
    ).fetchone()
    if not entry:
        raise HTTPException(404, "目标 entry 不存在")

    # 检查该 variant 文本是否已存在于该 entry
    existed = storage.conn.execute(
        "SELECT 1 FROM rag_variants WHERE entry_id = ? AND variant_text = ?",
        (req.entry_id, parent_q.strip()),
    ).fetchone()
    if existed:
        storage.conn.execute(
            "UPDATE candidate_phrases SET status='merged', reviewed_by=?, reviewed_at=?, rag_entry_id=? "
            "WHERE id = ?",
            (admin["username"], _time.time(), req.entry_id, cid),
        )
        return {"merged_into": req.entry_id, "added_variants": 0, "note": "variant 已存在"}

    try:
        vec = await rag_embed.embed_one(parent_q.strip())
    except Exception as e:
        raise HTTPException(500, f"embedding 失败: {e}")
    await rag_store.add_variants(req.entry_id, [parent_q.strip()], [vec])

    storage.conn.execute(
        "UPDATE candidate_phrases SET status='merged', reviewed_by=?, reviewed_at=?, rag_entry_id=? "
        "WHERE id = ?",
        (admin["username"], _time.time(), req.entry_id, cid),
    )
    return {"merged_into": req.entry_id, "added_variants": 1}


@router.post("/rag/backfill-answer-vectors")
async def rag_backfill_answer_vectors(admin: dict = Depends(get_admin)):
    """一次性回填历史 rag_entries.best_answer 的 embedding 到 rag_answer_vectors。幂等。"""
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT id, best_answer FROM rag_entries "
        "WHERE id NOT IN (SELECT entry_id FROM rag_answer_vectors)"
    ).fetchall()
    if not rows:
        return {"embedded": 0, "skipped_existing": await answer_store.count(), "msg": "no entries need backfill"}

    embedded = 0
    errors = 0
    BATCH = 10
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        try:
            vectors = await rag_embed.embed([r[1] for r in batch])
            for (entry_id, _), vec in zip(batch, vectors):
                await answer_store.add_answer_vector(entry_id, vec)
                embedded += 1
        except Exception:
            logger.exception("backfill batch failed [%d:%d]", i, i + BATCH)
            errors += len(batch)
    return {"embedded": embedded, "errors": errors, "total_now": await answer_store.count()}


@router.post("/candidates/rescan-pending")
async def candidates_rescan_pending(force: bool = False, admin: dict = Depends(get_admin)):
    """对所有 status='pending' 候选重做答案侧匹配 + 刷新家长侧 Top 3 相似快照。

    - force=False(默认): 已有 suggested_merge_entry_id 的不动答案匹配标记,但 similar_top_n 仍会刷
    - force=True: 全部重算
    """
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT id, parent_query, staff_reply, suggested_merge_entry_id "
        "FROM candidate_phrases WHERE status='pending' "
        "ORDER BY id"
    ).fetchall()
    if not rows:
        return {"total": 0, "suggested": 0, "refreshed_similar": 0, "errors": 0}

    from app.candidate_miner import ANSWER_MATCH_THRESHOLD, _compact_similar
    from app.rag import retrieve as rag_retrieve

    suggested = 0
    refreshed = 0
    errors = 0
    BATCH = 10
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        # 答案侧 embed: 需要的子集
        if not force:
            batch_for_ans = [r for r in batch if r[3] is None]
        else:
            batch_for_ans = list(batch)
        if batch_for_ans:
            try:
                reply_vecs = await rag_embed.embed([r[2] for r in batch_for_ans])
            except Exception:
                logger.exception("rescan batch reply-embed failed [%d:%d]", i, i + BATCH)
                errors += len(batch_for_ans)
                reply_vecs = None
            if reply_vecs:
                for (cid, _, _, _), rvec in zip(batch_for_ans, reply_vecs):
                    try:
                        ans_matches = await answer_store.search_answer(rvec, top_k=1)
                        if ans_matches and ans_matches[0]["_similarity"] >= ANSWER_MATCH_THRESHOLD:
                            storage.conn.execute(
                                "UPDATE candidate_phrases SET suggested_merge_entry_id=?, answer_match_similarity=? WHERE id=?",
                                (ans_matches[0]["entry_id"], ans_matches[0]["_similarity"], cid),
                            )
                            suggested += 1
                    except Exception:
                        logger.exception("rescan candidate %s (answer side) failed", cid)
                        errors += 1
        # 家长侧 Top 3 刷新(总是刷)
        for cid, parent_q, _, _ in batch:
            try:
                sim_raw = await rag_retrieve.retrieve(parent_q or "", top_k=3, customer_id="")
                sim_json = json.dumps([_compact_similar(x) for x in sim_raw], ensure_ascii=False)
                storage.conn.execute(
                    "UPDATE candidate_phrases SET similar_top_n_cached=? WHERE id=?",
                    (sim_json, cid),
                )
                refreshed += 1
            except Exception:
                logger.exception("rescan candidate %s (similar) failed", cid)
                errors += 1
    return {"total": len(rows), "suggested": suggested, "refreshed_similar": refreshed, "errors": errors}


@router.get("/candidates/stats")
async def candidates_stats(admin: dict = Depends(get_admin)):
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT status, COUNT(*) FROM candidate_phrases GROUP BY status"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


@router.delete("/users/{username}")
async def users_delete(username: str, admin: dict = Depends(get_admin)):
    storage = get_storage()
    row = storage.conn.execute("SELECT role FROM admins WHERE username = ?", (username,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    if row[0] == "super":
        raise HTTPException(403, "超管账号不可删除")
    storage.conn.execute("DELETE FROM admins WHERE username = ?", (username,))
    return {"deleted": username}


# ========== Prompts ==========
import re as _re


class CreatePromptRequest(BaseModel):
    name: str
    content: str


class UpdatePromptRequest(BaseModel):
    content: str


def _validate_prompt_content(content: str):
    if not content or not content.strip():
        raise HTTPException(400, "prompt 内容不能为空")
    if "{query}" not in content:
        raise HTTPException(400, "prompt 必须包含 {query} 占位符")


def _validate_prompt_name(name: str):
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "prompt 名称不能为空")
    if not _re.match(r"^[a-z0-9_]+$", name):
        raise HTTPException(400, "prompt 名称只能用小写字母、数字、下划线")
    return name


@router.get("/prompts")
async def prompts_list(admin: dict = Depends(get_admin)):
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT id, name, content, is_default, updated_at, updated_by FROM prompts "
        "ORDER BY is_default DESC, name"
    ).fetchall()
    return [{
        "id": r[0],
        "name": r[1],
        "content": r[2],
        "is_default": bool(r[3]),
        "updated_at": r[4],
        "updated_by": r[5],
        "char_count": len(r[2] or ""),
    } for r in rows]


@router.get("/prompts/{name}")
async def prompts_get(name: str, admin: dict = Depends(get_admin)):
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT id, name, content, is_default, updated_at, updated_by "
        "FROM prompts WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "prompt 不存在")
    return {
        "id": row[0],
        "name": row[1],
        "content": row[2],
        "is_default": bool(row[3]),
        "updated_at": row[4],
        "updated_by": row[5],
    }


@router.post("/prompts")
async def prompts_create(req: CreatePromptRequest, admin: dict = Depends(get_admin)):
    name = _validate_prompt_name(req.name)
    _validate_prompt_content(req.content)
    storage = get_storage()
    if storage.conn.execute("SELECT 1 FROM prompts WHERE name = ?", (name,)).fetchone():
        raise HTTPException(409, "同名 prompt 已存在")
    storage.conn.execute(
        "INSERT INTO prompts (name, content, is_default, updated_by) VALUES (?, ?, 0, ?)",
        (name, req.content, admin["username"]),
    )
    _invalidate_prompt_cache(name)
    return {"created": name}


@router.put("/prompts/{name}")
async def prompts_update(name: str, req: UpdatePromptRequest, admin: dict = Depends(get_admin)):
    _validate_prompt_content(req.content)
    storage = get_storage()
    row = storage.conn.execute("SELECT id FROM prompts WHERE name = ?", (name,)).fetchone()
    if not row:
        raise HTTPException(404, "prompt 不存在")
    storage.conn.execute(
        "UPDATE prompts SET content = ?, updated_at = strftime('%s','now'), updated_by = ? WHERE name = ?",
        (req.content, admin["username"], name),
    )
    _invalidate_prompt_cache(name)
    return {"updated": name}


@router.delete("/prompts/{name}")
async def prompts_delete(name: str, admin: dict = Depends(get_admin)):
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT id, is_default FROM prompts WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "prompt 不存在")
    if row[1]:
        raise HTTPException(403, "内置 prompt 不可删除(可恢复默认值)")
    storage.conn.execute("DELETE FROM prompts WHERE name = ?", (name,))
    _invalidate_prompt_cache(name)
    return {"deleted": name}


@router.post("/prompts/{name}/reset")
async def prompts_reset(name: str, admin: dict = Depends(get_admin)):
    """把 is_default=1 的 prompt 内容重置为代码里的默认值。"""
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT id, is_default FROM prompts WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "prompt 不存在")
    if not row[1]:
        raise HTTPException(400, "只有内置 prompt 可以恢复默认值")
    if name == "ai_draft":
        from app.prompts_default import AI_DRAFT_DEFAULT_PROMPT
        default = AI_DRAFT_DEFAULT_PROMPT
    else:
        raise HTTPException(400, f"未找到 {name} 的默认值")
    storage.conn.execute(
        "UPDATE prompts SET content = ?, updated_at = strftime('%s','now'), updated_by = ? WHERE name = ?",
        (default, admin["username"], name),
    )
    _invalidate_prompt_cache(name)
    return {"reset": name}


# ========== prompt 缓存 (供 ai 调用读最新 prompt) ==========
_prompt_cache: dict = {}


def _invalidate_prompt_cache(name: str):
    _prompt_cache.pop(name, None)


# ========== LLM 模型切换 ==========

class LLMConfigRequest(BaseModel):
    provider: str
    model: str


@router.get("/llm-config")
async def llm_config_get(admin: dict = Depends(get_admin)):
    from app import llm
    active = llm.get_active_model()
    return {
        "active": active,
        "available": llm.SUPPORTED_MODELS,
    }


@router.put("/llm-config")
async def llm_config_set(req: LLMConfigRequest, admin: dict = Depends(get_admin)):
    from app import llm
    # 校验存在
    valid = any(
        m["provider"] == req.provider and m["model"] == req.model
        for m in llm.SUPPORTED_MODELS
    )
    if not valid:
        raise HTTPException(400, f"未知 provider/model: {req.provider}/{req.model}")
    storage = get_storage()
    payload = json.dumps({"provider": req.provider, "model": req.model}, ensure_ascii=False)
    storage.conn.execute(
        "INSERT INTO kv_settings (key, value, updated_by) VALUES ('ai_model', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=strftime('%s','now'), updated_by=excluded.updated_by",
        (payload, admin["username"]),
    )
    return {"active": {"provider": req.provider, "model": req.model}}


def get_prompt_content(name: str) -> str | None:
    """供 api_ai 用,从缓存或 DB 拿 prompt 内容。"""
    if name in _prompt_cache:
        return _prompt_cache[name]
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT content FROM prompts WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        return None
    _prompt_cache[name] = row[0]
    return row[0]
