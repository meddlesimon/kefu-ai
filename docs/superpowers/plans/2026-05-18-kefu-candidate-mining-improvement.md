# 话术挖掘改造 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让话术挖掘的每日待审从 200~300 条降到 30~80 条，大部分通过"一键合并到已有话术"完成，无需重写答案。

**Architecture:** 在现有 candidate_miner 流程上新增"答案侧 embedding 相似度匹配"分支：客服回复 embed → 跟所有已采纳 entry 的 best_answer 向量比 → 高度相似（默认 0.92）就标记 `suggested_merge_entry_id`、跳过 LLM 评估。审核 UI 显示"建议合并 + Top 3 相似老问题"，新增 `/candidates/{id}/merge` 接口把家长 query 作为新 variant 追加到目标 entry。

**Tech Stack:** Python 3.11 / FastAPI / SQLite / 阿里 Qwen3-Embedding (text-embedding-v4, 1024-dim) / numpy 向量检索 / Vanilla JS + HTML 后台

**Spec:** `docs/superpowers/specs/2026-05-18-kefu-candidate-mining-improvement-design.md`

**测试策略：** 本项目无 pytest 基础设施且无测试文化。每个任务的"完成判据"用**可粘贴的 curl 命令 / SQLite 查询 / 日志检查 / 浏览器手验**完成，不引入 pytest 框架（YAGNI）。

---

## File Structure

**新建：**
- `backend/app/rag/answer_store.py` — 答案侧向量存取，结构对齐 `rag/store.py`

**修改：**
- `backend/app/storage.py` — 新表 `rag_answer_vectors` + ALTER `candidate_phrases` 加 3 列
- `backend/app/config.py` — 新增 `candidate_answer_match_threshold` 配置项
- `backend/app/candidate_miner.py` — 新增答案侧匹配分支 + similar_top_n 缓存
- `backend/app/api_admin.py` — adopt 钩子 + merge / backfill-answer-vectors / rescan-pending 三个新接口 + GET candidates 返回结构扩展
- `backend/app/main.py` — lifespan 加启动时懒回填
- `backend/static/admin.html` — 候选卡片渲染 + 点击处理 + CSS

每个文件单一职责：`storage.py` 只管 schema、`answer_store.py` 只管答案向量、`candidate_miner.py` 只管挖掘逻辑、`api_admin.py` 处理审核接口、`admin.html` 处理审核 UI。

---

## 部署目标

- 服务器：**118.25.186.95**（ubuntu，SSH 由用户掌握）
- 容器：`kefu-backend`
- DB 持久化：`/data/kefu-ai/chat.db`（容器内 `/data/...`）
- 域名：`https://kefu.sunyeupupup.com`
- 部署方式：`docker compose up -d --build --force-recreate`（per memory：env 变更必须 force-recreate）

---

## Task 0：清理基线（commit 现有未提交改动）

**Files:**
- Commit: `backend/app/candidate_miner.py`（新增 `daily_scheduler`）
- Commit: `backend/app/main.py`（启动时挂 `candidate_cron_task`）

**说明：** 这两个文件目前有未 commit 改动，恰好就是 spec § 1 描述的"现有每日 5 点 cron"。先 commit 掉，让后续所有 diff 都从干净基线出发。

- [ ] **Step 1: 看一下完整未提交 diff，确认就是 cron 那段**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git diff backend/app/candidate_miner.py backend/app/main.py
```

预期：candidate_miner.py 末尾追加了 `daily_scheduler`，main.py lifespan 里挂了 `candidate_cron_task`。

- [ ] **Step 2: 单独 add 这两个文件并 commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/candidate_miner.py backend/app/main.py
git commit -m "$(cat <<'EOF'
feat(candidate-miner): 启动时挂每日 5 点候选挖掘 cron

每天北京时间 05:00 自动扫描前一天客服回复,跑 LLM 评估写入 candidate_phrases
等待人工审核。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: 验证 working tree 干净**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git status
```

预期：`nothing to commit, working tree clean`

---

## Task 1：DB schema —— 新表 + ALTER candidate_phrases

**Files:**
- Modify: `backend/app/storage.py:20-138`（`_init_schema` 函数）

- [ ] **Step 1: 修改 `_init_schema`，在 executescript 末尾追加新表 + ALTER**

打开 `backend/app/storage.py`，在 `_init_schema` 的 `executescript` 字符串里、`CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidate_phrases(status);` 这一行后面追加：

```sql
            CREATE TABLE IF NOT EXISTS rag_answer_vectors (
                entry_id     INTEGER PRIMARY KEY,
                vector       BLOB NOT NULL,
                embedded_at  REAL DEFAULT (strftime('%s','now'))
            );
```

然后在 `_init_schema` 函数末尾（`try: ALTER TABLE ai_drafts ...` 那块附近，**`self._seed_admins()` 调用之前**），添加候选表 3 个新列的兼容性 ALTER：

```python
        # 老库兼容: candidate_phrases 加 3 列(忽略已存在错误)
        for col_sql in (
            "ALTER TABLE candidate_phrases ADD COLUMN suggested_merge_entry_id INTEGER",
            "ALTER TABLE candidate_phrases ADD COLUMN answer_match_similarity  REAL",
            "ALTER TABLE candidate_phrases ADD COLUMN similar_top_n_cached     TEXT",
        ):
            try:
                self.conn.execute(col_sql)
            except Exception:
                pass
```

- [ ] **Step 2: 本地起一个临时 DB 验证 schema 加得对**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "
from app.storage import Storage
import tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
tmp.close()
s = Storage(tmp.name)
# 1. 新表存在
r = s.conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='rag_answer_vectors'\").fetchone()
print('rag_answer_vectors:', r)
# 2. 候选表 3 个新列存在
cols = [r[1] for r in s.conn.execute('PRAGMA table_info(candidate_phrases)').fetchall()]
print('candidate_phrases new cols present:', all(c in cols for c in ['suggested_merge_entry_id','answer_match_similarity','similar_top_n_cached']))
os.unlink(tmp.name)
"
```

预期输出：
```
rag_answer_vectors: ('rag_answer_vectors',)
candidate_phrases new cols present: True
```

- [ ] **Step 3: 验证再跑一次也不报错（幂等）**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "
from app.storage import Storage
import tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
tmp.close()
Storage(tmp.name)
Storage(tmp.name)  # 第二次应不报错
print('OK')
os.unlink(tmp.name)
"
```

预期：`OK`（无 Traceback）

- [ ] **Step 4: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/storage.py
git commit -m "$(cat <<'EOF'
feat(storage): 新增 rag_answer_vectors 表 + candidate_phrases 3 列

为答案侧去重和合并审核做准备:
- rag_answer_vectors: 已采纳话术 best_answer 的 1024 维向量
- candidate_phrases.suggested_merge_entry_id: 答案侧匹配命中的 entry id
- candidate_phrases.answer_match_similarity: 命中相似度(诊断用)
- candidate_phrases.similar_top_n_cached: 家长侧 Top 3 相似快照(JSON)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2：配置项 —— 答案匹配阈值

**Files:**
- Modify: `backend/app/config.py`

- [ ] **Step 1: 看一下 config.py 现有结构**

```bash
cat "/Users/a1-6/Desktop/所有代码/智能客服/backend/app/config.py" | head -50
```

确认它用的是 pydantic Settings。

- [ ] **Step 2: 在 settings 类里加一行**

在 `aliyun_embed_dim: int = 1024` 那一行后面追加：

```python
    candidate_answer_match_threshold: float = 0.92  # 答案侧相似度阈值,>= 触发"建议合并"
```

环境变量名自动是 `CANDIDATE_ANSWER_MATCH_THRESHOLD`（pydantic Settings 默认全大写）。

- [ ] **Step 3: 验证 import 不报错**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app.config import settings; print('threshold =', settings.candidate_answer_match_threshold)"
```

预期：`threshold = 0.92`

- [ ] **Step 4: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/config.py
git commit -m "$(cat <<'EOF'
feat(config): 新增 CANDIDATE_ANSWER_MATCH_THRESHOLD (默认 0.92)

答案侧 embedding 相似度的"严匹配"阈值,env 可覆盖。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3：新建 answer_store 模块

**Files:**
- Create: `backend/app/rag/answer_store.py`

**说明：** 结构对齐 `rag/store.py`（同样 numpy 矩阵 + 线程锁 + 失效缓存），只是表换成 `rag_answer_vectors`。

- [ ] **Step 1: 新建文件，写完整内容**

文件 `backend/app/rag/answer_store.py`：

```python
"""答案侧向量存取 —— 与 rag/store.py 结构对齐,操作 rag_answer_vectors 表。"""
import asyncio
import threading
from typing import List

import numpy as np

from app.config import settings
from app.storage import get_storage


_lock = threading.Lock()
_cache_vectors: np.ndarray | None = None
_cache_entry_ids: list | None = None  # 与 _cache_vectors 同序的 entry_id 列表


def _vec_to_bytes(vec: List[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _bytes_to_vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def _invalidate_cache():
    global _cache_vectors, _cache_entry_ids
    with _lock:
        _cache_vectors = None
        _cache_entry_ids = None


def _load_cache():
    global _cache_vectors, _cache_entry_ids
    if _cache_vectors is not None:
        return _cache_vectors, _cache_entry_ids
    with _lock:
        if _cache_vectors is not None:
            return _cache_vectors, _cache_entry_ids
        storage = get_storage()
        rows = storage.conn.execute(
            "SELECT entry_id, vector FROM rag_answer_vectors"
        ).fetchall()
        if not rows:
            _cache_vectors = np.zeros((0, settings.aliyun_embed_dim), dtype=np.float32)
            _cache_entry_ids = []
        else:
            _cache_entry_ids = [r[0] for r in rows]
            _cache_vectors = np.stack([_bytes_to_vec(r[1]) for r in rows])
        return _cache_vectors, _cache_entry_ids


def _add_sync(entry_id: int, vector: List[float]):
    storage = get_storage()
    storage.conn.execute(
        "INSERT OR REPLACE INTO rag_answer_vectors (entry_id, vector, embedded_at) "
        "VALUES (?, ?, strftime('%s','now'))",
        (int(entry_id), _vec_to_bytes(vector)),
    )
    _invalidate_cache()


async def add_answer_vector(entry_id: int, vector: List[float]):
    await asyncio.to_thread(_add_sync, entry_id, vector)


def _delete_sync(entry_id: int):
    storage = get_storage()
    storage.conn.execute("DELETE FROM rag_answer_vectors WHERE entry_id = ?", (int(entry_id),))
    _invalidate_cache()


async def delete_answer_vector(entry_id: int):
    await asyncio.to_thread(_delete_sync, entry_id)


def _search_sync(query_vec: List[float], top_k: int) -> List[dict]:
    vectors, entry_ids = _load_cache()
    if len(entry_ids) == 0:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0:
        return []
    v_norms = np.linalg.norm(vectors, axis=1)
    v_norms = np.where(v_norms == 0, 1.0, v_norms)
    sims = (vectors @ q) / (q_norm * v_norms)
    k = min(top_k, len(sims))
    if k == len(sims):
        idx = np.argsort(-sims)
    else:
        idx = np.argpartition(-sims, k)[:k]
        idx = idx[np.argsort(-sims[idx])]
    return [{
        "entry_id": entry_ids[i],
        "_similarity": float(sims[i]),
    } for i in idx]


async def search_answer(query_vec: List[float], top_k: int = 5) -> List[dict]:
    return await asyncio.to_thread(_search_sync, query_vec, top_k)


def _count_sync() -> int:
    storage = get_storage()
    row = storage.conn.execute("SELECT COUNT(*) FROM rag_answer_vectors").fetchone()
    return row[0] if row else 0


async def count() -> int:
    return await asyncio.to_thread(_count_sync)
```

- [ ] **Step 2: 跑一段验证脚本，确认 add/search/count 流水正确**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "
import asyncio
from app.storage import Storage
import app.storage as storage_mod
import tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False); tmp.close()
storage_mod._storage_instance = Storage(tmp.name)
from app.rag import answer_store

async def main():
    print('count before:', await answer_store.count())
    # 加 3 条假向量
    import numpy as np
    v1 = np.random.rand(1024).tolist()
    v2 = np.random.rand(1024).tolist()
    v3 = v1.copy()  # 跟 v1 完全一样
    await answer_store.add_answer_vector(1, v1)
    await answer_store.add_answer_vector(2, v2)
    await answer_store.add_answer_vector(3, v3)
    print('count after:', await answer_store.count())
    # 用 v1 搜,应该 entry_id=1 或 3 排第一,相似度接近 1.0
    r = await answer_store.search_answer(v1, top_k=3)
    print('search results:')
    for x in r:
        print(' ', x)
    # 删 entry 1,再搜
    await answer_store.delete_answer_vector(1)
    print('count after delete:', await answer_store.count())
    r = await answer_store.search_answer(v1, top_k=3)
    print('after delete top-1 entry_id:', r[0]['entry_id'] if r else None)

asyncio.run(main())
os.unlink(tmp.name)
"
```

预期：
- `count before: 0`
- `count after: 3`
- search results 里 entry 1 和 3 的 `_similarity` ≈ 1.0（同向量），entry 2 显著低
- `count after delete: 2`
- delete 后 top-1 entry_id = 3（剩下的相同向量）

- [ ] **Step 3: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/rag/answer_store.py
git commit -m "$(cat <<'EOF'
feat(rag): 新增 answer_store 模块,管理已采纳话术答案向量

提供 add_answer_vector / delete_answer_vector / search_answer / count,
结构对齐 rag/store.py,但独立缓存独立表(rag_answer_vectors)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4：候选采纳时同步写答案向量

**Files:**
- Modify: `backend/app/api_admin.py:346-397`（`candidates_adopt` 函数）

- [ ] **Step 1: 在 api_admin.py 顶部 import answer_store**

找到 `from app.rag import embed as rag_embed` 那行（约第 10 行），改成：

```python
from app.rag import embed as rag_embed, store as rag_store, answer_store
```

如果 `rag_store` 已经 import 过就不要重复，只补 `answer_store`。

确认下 grep：

```bash
grep -n "from app.rag" "/Users/a1-6/Desktop/所有代码/智能客服/backend/app/api_admin.py"
```

照实际 import 情况调整。

- [ ] **Step 2: 在 `candidates_adopt` 函数里、`add_variants` 之后插入答案向量写入**

定位到约 390 行：
```python
    vectors = await rag_embed.embed(uniq_vars)
    await rag_store.add_variants(entry_id, uniq_vars, vectors)
```

在 `add_variants` 那一行后面追加：

```python
    # 答案侧向量(答案去重用)
    try:
        answer_vec = await rag_embed.embed_one(answer)
        await answer_store.add_answer_vector(entry_id, answer_vec)
    except Exception:
        logger.exception("写答案向量失败 entry_id=%s (不阻断 adopt)", entry_id)
```

`logger` 应当已经在文件顶部存在；如果没有，在文件头部加 `import logging; logger = logging.getLogger(__name__)`。

- [ ] **Step 3: 启动一次 dev server，触发一次 adopt，看日志**

由于本地起 dev server 还要配企微，简化为：直接拿单元级脚本验证 import 不破：

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app import api_admin; print('OK import')"
```

预期：`OK import`，无 Traceback。

- [ ] **Step 4: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/api_admin.py
git commit -m "$(cat <<'EOF'
feat(api-admin): adopt 候选时同步写答案向量到 rag_answer_vectors

为后续答案侧去重做储备。embed 失败仅记日志不阻断 adopt。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5：新接口 —— 一次性回填答案向量

**Files:**
- Modify: `backend/app/api_admin.py`（紧接 `candidates_scan` 区段后）

- [ ] **Step 1: 在 api_admin.py 候选区段后追加新路由**

找到 `# ========== Prompts ==========` 这一行（约第 435 行），**在它之前**插入：

```python
# ========== RAG 答案向量回填 ==========

@router.post("/rag/backfill-answer-vectors")
async def rag_backfill_answer_vectors(admin: dict = Depends(get_admin)):
    """一次性回填历史 rag_entries.best_answer 的 embedding 到 rag_answer_vectors。

    幂等:跳过 rag_answer_vectors 中已存在的 entry_id。
    """
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT id, best_answer FROM rag_entries "
        "WHERE id NOT IN (SELECT entry_id FROM rag_answer_vectors)"
    ).fetchall()
    if not rows:
        return {"embedded": 0, "skipped": 0, "msg": "no entries need backfill"}

    embedded = 0
    errors = 0
    # 阿里 embed 一次最多 25 条
    BATCH = 25
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i+BATCH]
        try:
            vectors = await rag_embed.embed([r[1] for r in batch])
            for (entry_id, _), vec in zip(batch, vectors):
                await answer_store.add_answer_vector(entry_id, vec)
                embedded += 1
        except Exception:
            logger.exception("backfill batch failed [%d:%d]", i, i+BATCH)
            errors += len(batch)
    skipped_count = storage.conn.execute(
        "SELECT COUNT(*) FROM rag_entries WHERE id IN (SELECT entry_id FROM rag_answer_vectors)"
    ).fetchone()[0] - embedded
    return {"embedded": embedded, "skipped": max(0, skipped_count), "errors": errors}
```

- [ ] **Step 2: 验证 import 不破**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app import api_admin; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/api_admin.py
git commit -m "$(cat <<'EOF'
feat(api-admin): 新增 POST /api/admin/rag/backfill-answer-vectors

一次性把历史 rag_entries.best_answer 全部 embed 入 rag_answer_vectors,
幂等(跳过已存在 entry_id),分批(25/批)。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6：启动钩子 —— 懒回填

**Files:**
- Modify: `backend/app/main.py:30-52`（`lifespan` 函数）

- [ ] **Step 1: 在 lifespan 里、`candidate_cron_task` 创建之后追加懒回填**

定位到 main.py 约 43 行：
```python
    candidate_cron_task = asyncio.create_task(candidate_miner.daily_scheduler(hour_local=5))
```

下面追加：

```python
    # 启动时检测答案向量库是否为空,空就异步回填(幂等)
    async def _maybe_backfill_answer_vectors():
        from app.rag import answer_store, embed as rag_embed
        try:
            n_answers = await answer_store.count()
            n_entries = storage.conn.execute("SELECT COUNT(*) FROM rag_entries").fetchone()[0]
            if n_entries > 0 and n_answers == 0:
                logger.info("[answer-backfill] 检测到 %d 条 entry 但答案向量空,开始回填...", n_entries)
                rows = storage.conn.execute("SELECT id, best_answer FROM rag_entries").fetchall()
                BATCH = 25
                done = 0
                for i in range(0, len(rows), BATCH):
                    batch = rows[i:i+BATCH]
                    try:
                        vectors = await rag_embed.embed([r[1] for r in batch])
                        for (eid, _), vec in zip(batch, vectors):
                            await answer_store.add_answer_vector(eid, vec)
                        done += len(batch)
                    except Exception:
                        logger.exception("[answer-backfill] batch %d:%d failed", i, i+BATCH)
                logger.info("[answer-backfill] 完成 %d/%d", done, len(rows))
            else:
                logger.info("[answer-backfill] 跳过(entries=%d, answers=%d)", n_entries, n_answers)
        except Exception:
            logger.exception("[answer-backfill] 异常")

    answer_backfill_task = asyncio.create_task(_maybe_backfill_answer_vectors())
```

并在 finally 块的 cancel 列表里追加：
```python
        answer_backfill_task.cancel()
```

- [ ] **Step 2: 验证 import 不破**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app import main; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/main.py
git commit -m "$(cat <<'EOF'
feat(main): 启动时若答案向量库为空则异步回填

幂等:仅当 rag_entries 非空但 rag_answer_vectors 为空时触发。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7：candidate_miner 答案侧匹配分支

**Files:**
- Modify: `backend/app/candidate_miner.py:103-220`（`scan` 函数）

- [ ] **Step 1: 顶部 import answer_store**

定位到 candidate_miner.py 第 18-22 行：
```python
import httpx

from app.config import settings
from app.storage import get_storage
```

改成：
```python
import httpx

from app.config import settings
from app.rag import answer_store, embed as rag_embed
from app.storage import get_storage
```

- [ ] **Step 2: 在 scan 函数顶部，stats 字典里增加新字段**

定位到约 115 行：
```python
    stats = {"total": len(rows), "skip_short": 0, "skip_dup": 0, "skip_no_query": 0,
             "skip_rag_match": 0, "skip_was_ai_adopt": 0, "skip_low_score": 0,
             "scored": 0, "added": 0, "errors": 0}
```

改成：
```python
    stats = {"total": len(rows), "skip_short": 0, "skip_dup": 0, "skip_no_query": 0,
             "skip_rag_match": 0, "skip_was_ai_adopt": 0, "skip_low_score": 0,
             "scored": 0, "added": 0, "merge_suggested": 0, "errors": 0}
```

- [ ] **Step 3: 加一个内部辅助函数 `_compact_similar`**

在 scan 函数定义之前（约 102 行，在 `_reply_hash` 函数后面）插入：

```python
def _compact_similar(item: dict) -> dict:
    """裁剪 rag_retrieve.retrieve 返回的字典为 UI 最小字段。"""
    ans = (item.get("answer") or "")
    return {
        "entry_id": item.get("id"),
        "category": item.get("category"),
        "best_answer_preview": ans[:80] + ("…" if len(ans) > 80 else ""),
        "similarity": item.get("similarity"),
    }
```

- [ ] **Step 4: 在 scan 函数里，**已有"已有相似话术"那块（约 168 行）之后、LLM 评估之前，插入答案侧匹配分支**

定位到这段代码（约 167-187 行）：
```python
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
            ...
        ).fetchone()
        if ai_adopt:
            stats["skip_was_ai_adopt"] += 1
            continue
```

**整段替换为：**

```python
        # 算 similar_top_n (家长侧 Top 3) —— 候选卡片 UI 用,同时也复用做老的家长侧严匹配
        similar_top_n = []
        try:
            similar_top_n_raw = await rag_retrieve.retrieve(parent_query, top_k=3, customer_id="")
            similar_top_n = [_compact_similar(x) for x in similar_top_n_raw]
            # 原有的家长侧 0.85 严匹配保留(已存在该问法 → 跳过)
            if similar_top_n_raw and similar_top_n_raw[0]["similarity"] >= 0.85:
                stats["skip_rag_match"] += 1
                continue
        except Exception:
            logger.exception("similar_top_n 检索失败 seq=%s (降级继续)", seq)

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

        # NEW: 答案侧匹配 —— 客服回复跟已采纳 best_answer 高度相似就直接走"建议合并"
        suggested_merge_entry_id = None
        answer_match_similarity = None
        try:
            reply_vec = await rag_embed.embed_one(content)
            ans_matches = await answer_store.search_answer(reply_vec, top_k=1)
            if ans_matches and ans_matches[0]["_similarity"] >= settings.candidate_answer_match_threshold:
                suggested_merge_entry_id = ans_matches[0]["entry_id"]
                answer_match_similarity = ans_matches[0]["_similarity"]
        except Exception:
            logger.exception("答案侧匹配失败 seq=%s (降级走 LLM 评估)", seq)

        if suggested_merge_entry_id is not None:
            # 高度相似 → 跳过 LLM 评估,直接写"建议合并"候选
            try:
                storage.conn.execute(
                    "INSERT INTO candidate_phrases "
                    "(parent_query, staff_reply, cleaned_reply, suggested_category, "
                    " suggested_variants, llm_score, llm_reason, source_seq, customer_id, reply_hash, "
                    " suggested_merge_entry_id, answer_match_similarity, similar_top_n_cached) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        parent_query, content, content,
                        "(待合并)", json.dumps([parent_query], ensure_ascii=False),
                        round(answer_match_similarity * 10, 2),
                        "客服回复与已采纳话术答案高度相似",
                        seq, customer_id, rh,
                        suggested_merge_entry_id, answer_match_similarity,
                        json.dumps(similar_top_n, ensure_ascii=False),
                    ),
                )
                stats["merge_suggested"] += 1
                stats["added"] += 1
            except Exception:
                logger.exception("写合并建议候选失败 seq=%s", seq)
                stats["errors"] += 1
            continue
```

- [ ] **Step 5: 修改原有"LLM 评估通过后写候选"那段，把 similar_top_n_cached 一起写进去**

定位到约 202-216 行：
```python
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
```

替换为：

```python
        try:
            storage.conn.execute(
                "INSERT INTO candidate_phrases "
                "(parent_query, staff_reply, cleaned_reply, suggested_category, "
                " suggested_variants, llm_score, llm_reason, source_seq, customer_id, reply_hash, "
                " similar_top_n_cached) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    parent_query, content, ev["cleaned"],
                    ev["category"], json.dumps(ev["variants"], ensure_ascii=False),
                    ev["score"], ev["reason"],
                    seq, customer_id, rh,
                    json.dumps(similar_top_n, ensure_ascii=False),
                ),
            )
            stats["added"] += 1
        except Exception:
            logger.exception("写候选失败 seq=%s", seq)
            stats["errors"] += 1
```

- [ ] **Step 6: 验证 import 不破**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app import candidate_miner; print('OK')"
```

- [ ] **Step 7: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/candidate_miner.py
git commit -m "$(cat <<'EOF'
feat(candidate-miner): 新增答案侧匹配分支 + similar_top_n 缓存

scan 流程在家长侧严匹配通过后,新增一步:
- 客服回复 embed → 跟 rag_answer_vectors 比
- 若 ≥ candidate_answer_match_threshold(默认 0.92): 跳过 LLM 评估,
  直接写"建议合并"候选(suggested_merge_entry_id 标记)
- 同时把家长侧 Top 3 相似项缓存到 similar_top_n_cached 列

stats 新增 merge_suggested 维度。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8：新接口 —— 合并候选到已有 entry

**Files:**
- Modify: `backend/app/api_admin.py`（紧接 `candidates_adopt` 之后）

- [ ] **Step 1: 在 `candidates_adopt` 函数定义之后插入新路由**

定位到 `candidates_adopt` 函数结束（约第 397 行 `return ...`）之后、`@router.post("/candidates/{cid}/ignore")` 之前，插入：

```python
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

    # 检查该 variant 文本是否已存在于该 entry (避免重复 embed)
    existed = storage.conn.execute(
        "SELECT 1 FROM rag_variants WHERE entry_id = ? AND variant_text = ?",
        (req.entry_id, parent_q.strip()),
    ).fetchone()
    if existed:
        # 不报错,直接标 merged
        storage.conn.execute(
            "UPDATE candidate_phrases SET status='merged', reviewed_by=?, reviewed_at=?, rag_entry_id=? "
            "WHERE id = ?",
            (admin["username"], _time.time(), req.entry_id, cid),
        )
        return {"merged_into": req.entry_id, "added_variants": 0, "note": "variant 已存在,直接标记"}

    # 新 variant: embed + 入向量库
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
```

- [ ] **Step 2: 验证 import 不破**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app import api_admin; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/api_admin.py
git commit -m "$(cat <<'EOF'
feat(api-admin): 新增 POST /api/admin/candidates/{cid}/merge

把候选的 parent_query 作为新 variant 追加到指定 entry,
不修改 best_answer。候选 status='merged'。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9：GET /candidates 返回结构扩展

**Files:**
- Modify: `backend/app/api_admin.py:311-337`（`candidates_list` 函数）

- [ ] **Step 1: 改 SELECT 字段 + 返回字典**

定位到 `candidates_list` 函数，整段替换为：

```python
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

    # 一次性查所有被建议的 entry,免得 N+1
    entry_ids = {r[15] for r in rows if r[15]}
    entry_map = {}
    if entry_ids:
        ph = ",".join("?" * len(entry_ids))
        for eid, cat, ans in storage.conn.execute(
            f"SELECT id, category, best_answer FROM rag_entries WHERE id IN ({ph})",
            tuple(entry_ids),
        ).fetchall():
            entry_map[eid] = {
                "entry_id": eid,
                "category": cat,
                "best_answer_preview": (ans or "")[:80] + ("…" if len(ans or "") > 80 else ""),
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
```

- [ ] **Step 2: 验证 import 不破**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app import api_admin; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/api_admin.py
git commit -m "$(cat <<'EOF'
feat(api-admin): GET /api/admin/candidates 返回 suggested_merge + similar_top_n

直接读候选自身的 suggested_merge_entry_id / similar_top_n_cached 列,
不在列表接口里跑实时 embed。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10：新接口 —— rescan-pending（存量回扫）

**Files:**
- Modify: `backend/app/api_admin.py`（紧接 `rag_backfill_answer_vectors` 后）

- [ ] **Step 1: 紧跟在 backfill-answer-vectors 后追加路由**

```python
@router.post("/candidates/rescan-pending")
async def candidates_rescan_pending(force: bool = False, admin: dict = Depends(get_admin)):
    """对所有 status='pending' 候选重新做答案侧匹配,刷新 suggested_merge_entry_id
    和 similar_top_n_cached。

    - force=False(默认): 已有 suggested_merge_entry_id 的不动
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

    from app.rag import retrieve as rag_retrieve

    suggested = 0
    refreshed = 0
    errors = 0
    BATCH = 25
    threshold = settings.candidate_answer_match_threshold

    for i in range(0, len(rows), BATCH):
        batch = rows[i:i+BATCH]
        try:
            reply_vecs = await rag_embed.embed([r[2] for r in batch])
            # 家长侧 retrieve 内部已 embed,这里就不批量 embed parent_query 了(retrieve 是单条 API,但单批 25 条仍可接受)
        except Exception:
            logger.exception("rescan batch embed reply failed [%d:%d]", i, i+BATCH)
            errors += len(batch)
            continue

        for (cid, parent_q, _staff_reply, existing_merge_id), rvec in zip(batch, reply_vecs):
            try:
                # 1) 答案侧匹配
                if force or existing_merge_id is None:
                    ans_matches = await answer_store.search_answer(rvec, top_k=1)
                    if ans_matches and ans_matches[0]["_similarity"] >= threshold:
                        storage.conn.execute(
                            "UPDATE candidate_phrases SET suggested_merge_entry_id=?, answer_match_similarity=? WHERE id=?",
                            (ans_matches[0]["entry_id"], ans_matches[0]["_similarity"], cid),
                        )
                        suggested += 1

                # 2) similar_top_n 刷新(总是刷新)
                sim_top_n_raw = await rag_retrieve.retrieve(parent_q or "", top_k=3, customer_id="")
                sim_top_n = []
                for x in sim_top_n_raw:
                    ans = (x.get("answer") or "")
                    sim_top_n.append({
                        "entry_id": x.get("id"),
                        "category": x.get("category"),
                        "best_answer_preview": ans[:80] + ("…" if len(ans) > 80 else ""),
                        "similarity": x.get("similarity"),
                    })
                storage.conn.execute(
                    "UPDATE candidate_phrases SET similar_top_n_cached=? WHERE id=?",
                    (json.dumps(sim_top_n, ensure_ascii=False), cid),
                )
                refreshed += 1
            except Exception:
                logger.exception("rescan candidate %s failed", cid)
                errors += 1

    return {"total": len(rows), "suggested": suggested, "refreshed_similar": refreshed, "errors": errors}
```

- [ ] **Step 2: 验证 import 不破**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "from app import api_admin; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/app/api_admin.py
git commit -m "$(cat <<'EOF'
feat(api-admin): 新增 POST /api/admin/candidates/rescan-pending

存量回扫: 给所有 pending 候选重做答案侧匹配并刷新 similar_top_n。
默认 force=false 跳过已标记的;force=true 全部重算。
分批 25 条/批,带详细 stats 返回。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11：admin.html UI 改造

**Files:**
- Modify: `backend/static/admin.html`（候选卡片渲染 1474-1518 行 + 点击处理 1524-1561 行 + CSS）

- [ ] **Step 1: 在 CSS 区段（约 444 行附近 `话术挖掘候选卡` 注释下面）追加新样式**

定位到 admin.html 第 444 行附近 `/* 话术挖掘候选卡 */`，找到该区段末尾后追加：

```css
.cand-merge-block {
  background: #ecfdf5;
  border: 2px solid #10b981;
  border-radius: 8px;
  padding: 10px 12px;
  margin: 10px 0;
}
.cand-merge-block .cand-merge-title {
  font-size: 12px;
  color: #059669;
  font-weight: 600;
  margin-bottom: 6px;
}
.cand-merge-block .cand-merge-entry {
  font-size: 13px;
  color: var(--text-1);
  line-height: 1.5;
}
.cand-merge-block .cand-merge-preview {
  font-size: 12px;
  color: var(--text-2);
  margin: 4px 0 8px;
  max-height: 3.6em;
  overflow: hidden;
}
.cand-similar-list {
  background: #f9fafb;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  margin: 8px 0;
}
.cand-similar-item {
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
}
.cand-similar-item:last-child { border-bottom: none; }
.cand-similar-text { flex: 1; color: var(--text-2); }
.cand-similar-sim { font-size: 11px; color: var(--text-3); }
.cand-edit-collapsed { display: none; }
```

- [ ] **Step 2: 修改 `refreshCandidates` 渲染逻辑**

定位到 `wrap.innerHTML = list.map(c => {` 那一行（约第 1474 行）。整段替换为：

```javascript
    wrap.innerHTML = list.map(c => {
      const variants = c.suggested_variants || [];
      const isPending = c.status === 'pending';
      const sm = c.suggested_merge;
      const similar = c.similar_top_n || [];
      const editCollapsed = !!sm; // 有建议合并时折叠编辑区
      const mergeBlockHtml = sm ? `
        <div class="cand-merge-block">
          <div class="cand-merge-title">📚 系统判断:这跟下面这条已有话术答案高度相似(相似度 ${(sm.similarity*100).toFixed(1)}%)</div>
          <div class="cand-merge-entry">#${sm.entry_id} · ${escapeHTML(sm.category||'未分类')}</div>
          <div class="cand-merge-preview">${escapeHTML(sm.best_answer_preview||'')}</div>
          ${isPending ? `<button class="btn btn-sm btn-primary" data-cand-action="merge" data-id="${c.id}" data-entry="${sm.entry_id}">✅ 合并到此问题</button>` : ''}
        </div>
      ` : '';
      const similarListHtml = similar.length ? `
        <div class="cand-similar-list">
          <div style="font-size:11px;color:var(--text-3);margin-bottom:4px;">📋 其他比较像的老问题(参考):</div>
          ${similar.filter(s => !sm || s.entry_id !== sm.entry_id).slice(0, 3).map(s => `
            <div class="cand-similar-item">
              <span class="cand-similar-text">#${s.entry_id} · ${escapeHTML(s.category||'')} · ${escapeHTML(s.best_answer_preview||'')}</span>
              <span class="cand-similar-sim">${(s.similarity*100).toFixed(0)}%</span>
              ${isPending ? `<button class="btn btn-sm" data-cand-action="merge" data-id="${c.id}" data-entry="${s.entry_id}">合并到此</button>` : ''}
            </div>
          `).join('')}
        </div>
      ` : '';
      return `
        <div class="card cand-card" data-id="${c.id}">
          <div class="cand-head">
            <span class="badge ${c.llm_score >= 9 ? 'badge-green' : 'badge-amber'}">${c.llm_score} 分</span>
            <span class="cand-cat">${escapeHTML(c.suggested_category||'未分类')}</span>
            <span class="spacer"></span>
            <span class="cand-meta">${escapeHTML(c.llm_reason||'')} · ${tsToTimeAgo(c.created_at)}</span>
          </div>
          <div class="cand-block">
            <div class="cand-label">家长问</div>
            <div class="cand-text">${escapeHTML(c.parent_query)}</div>
          </div>
          <div class="cand-block">
            <div class="cand-label">客服原回复</div>
            <div class="cand-text">${escapeHTML(c.staff_reply)}</div>
          </div>
          ${mergeBlockHtml}
          ${similarListHtml}
          <div class="${editCollapsed ? 'cand-edit-collapsed' : ''}" data-edit-area="${c.id}">
            <div class="cand-block cand-cleaned">
              <div class="cand-label">📝 LLM 脱敏建议版本(默认入库用这个)</div>
              <textarea class="textarea cand-edit-answer" rows="4" data-id="${c.id}" ${isPending?'':'disabled'}>${escapeHTML(c.cleaned_reply||c.staff_reply)}</textarea>
            </div>
            <div class="cand-block">
              <div class="cand-label">📌 入库分类 + 变体(可改)</div>
              <input type="text" class="input cand-edit-cat" data-id="${c.id}" value="${escapeHTML(c.suggested_category||'')}" ${isPending?'':'disabled'}>
              <textarea class="textarea cand-edit-vars" rows="3" data-id="${c.id}" ${isPending?'':'disabled'} placeholder="每行一条变体">${variants.map(v=>escapeHTML(v)).join('\n')}\n${escapeHTML(c.parent_query)}</textarea>
              <div style="font-size:10px;color:var(--text-3);margin-top:4px;">默认会把家长原问 + LLM 推的 4 个变体入库,你可改</div>
            </div>
          </div>
          ${isPending ? `
            <div class="row" style="margin-top:10px;">
              <button class="btn btn-sm btn-danger" data-cand-action="ignore" data-id="${c.id}">丢弃</button>
              ${editCollapsed ? `<button class="btn btn-sm" data-cand-action="expand-edit" data-id="${c.id}">展开"另存为全新问题"</button>` : ''}
              <span class="spacer"></span>
              <button class="btn btn-sm ${editCollapsed?'':'btn-primary'}" data-cand-action="adopt" data-id="${c.id}">📝 另存为全新问题</button>
            </div>
          ` : `
            <div class="row" style="margin-top:10px;">
              <span style="font-size:11px;color:var(--text-3);">
                状态: ${c.status} · 由 ${escapeHTML(c.reviewed_by||'')} 于 ${tsToTimeAgo(c.reviewed_at)} 处理
                ${c.rag_entry_id ? ` · RAG entry #${c.rag_entry_id}` : ''}
              </span>
            </div>
          `}
        </div>`;
    }).join('');
```

- [ ] **Step 3: 修改点击处理（addEventListener 那块约 1524 行）追加 merge 和 expand-edit 分支**

定位到 `$('candidates-list').addEventListener('click', async (e) => {` 块（约第 1524 行），整段替换为：

```javascript
$('candidates-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-cand-action]');
  if (!btn) return;
  const id = btn.dataset.id;
  const action = btn.dataset.candAction;
  if (action === 'ignore') {
    btn.disabled = true;
    try {
      await api('/api/admin/candidates/' + id + '/ignore', {method: 'POST'});
      showToast('已丢弃');
      refreshCandidates();
    } catch (err) { showToast('失败: ' + err.message, 'error'); btn.disabled = false; }
  } else if (action === 'merge') {
    const entryId = parseInt(btn.dataset.entry, 10);
    btn.disabled = true; btn.textContent = '合并中…';
    try {
      const r = await api('/api/admin/candidates/' + id + '/merge', {
        method: 'POST',
        body: { entry_id: entryId },
      });
      showToast(`✓ 已合并到 #${r.merged_into}` + (r.added_variants ? '(新加 1 个问法)' : '(问法已存在)'));
      refreshCandidates();
      refreshStats();
    } catch (err) { showToast('合并失败: ' + err.message, 'error'); btn.disabled = false; }
  } else if (action === 'expand-edit') {
    const area = document.querySelector(`[data-edit-area="${id}"]`);
    if (area) area.classList.remove('cand-edit-collapsed');
    btn.remove();
  } else if (action === 'adopt') {
    btn.disabled = true; btn.textContent = '入库中…';
    const card = btn.closest('.cand-card');
    const answer = card.querySelector('.cand-edit-answer').value;
    const category = card.querySelector('.cand-edit-cat').value.trim();
    const variants = card.querySelector('.cand-edit-vars').value
      .split('\n').map(s => s.trim()).filter(Boolean);
    if (!answer.trim() || !category) {
      showToast('分类和答案不能空', 'error');
      btn.disabled = false; btn.textContent = '📝 另存为全新问题'; return;
    }
    if (variants.length < 1) {
      showToast('至少 1 个变体', 'error');
      btn.disabled = false; btn.textContent = '📝 另存为全新问题'; return;
    }
    try {
      const r = await api('/api/admin/candidates/' + id + '/adopt', {
        method: 'POST',
        body: { category, answer, variants },
      });
      showToast(`✓ 已入库 RAG #${r.rag_entry_id}`);
      refreshCandidates();
      refreshStats();
    } catch (err) { showToast('失败: ' + err.message, 'error'); btn.disabled = false; btn.textContent = '📝 另存为全新问题'; }
  }
});
```

- [ ] **Step 4: 把状态筛选区的"已忽略"标签也改成"已丢弃"保持一致**

定位到约 792 行 `<label class="checkbox"><input type="radio" name="cand-status" value="ignored"> 已忽略</label>`，**保留 value="ignored"**（兼容历史数据），但显示文案如果要改：

```html
        <label class="checkbox"><input type="radio" name="cand-status" value="ignored"> 已丢弃</label>
```

并补一个新选项让用户看 merged：

```html
        <label class="checkbox"><input type="radio" name="cand-status" value="merged"> 已合并</label>
```

- [ ] **Step 5: cand-stats 显示也加上 merged 计数**

定位到约 1469 行：
```javascript
    $('cand-stats').textContent = `待审${stats.pending||0} · 已纳${stats.adopted||0} · 已忽${stats.ignored||0}`;
```

替换为：

```javascript
    $('cand-stats').textContent = `待审${stats.pending||0} · 已合并${stats.merged||0} · 已新增${stats.adopted||0} · 已丢弃${stats.ignored||0}`;
```

- [ ] **Step 6: 验证 HTML 没语法破（用 python 简单 parse）**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服/backend"
python3 -c "
from html.parser import HTMLParser
class P(HTMLParser): pass
p = P()
p.feed(open('static/admin.html').read())
print('OK parse')"
```

- [ ] **Step 7: Commit**

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git add backend/static/admin.html
git commit -m "$(cat <<'EOF'
feat(admin-ui): 候选卡片改造 - 建议合并 + Top 3 相似老问题

- 卡片显示建议合并块(绿色边框)+ Top 3 参考相似项
- 新增"合并到此问题"按钮 → POST /candidates/{id}/merge
- 有建议合并时默认折叠编辑区,点"展开另存为全新问题"再编辑
- "采纳入库" → "另存为全新问题", "忽略" → "丢弃"
- 状态筛选 + 顶部计数加入 merged 维度

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12：部署到生产 + 存量回扫

**说明：** 服务器 SSH 由用户掌握，本任务的步骤是**用户在他自己的终端粘贴执行**。每步都给可粘贴的命令 + 预期输出。

- [ ] **Step 1: 在本机 push 到远端**（如果有 git remote）

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git log --oneline -15  # 看一下要 push 的 commit
git push  # 若有远端
```

如无远端：用 rsync 把变更同步到服务器（用户根据自己习惯）。

- [ ] **Step 2: SSH 登录服务器，pull 最新代码**

用户在服务器执行：
```bash
ssh ubuntu@118.25.186.95
cd ~/kefu-ai  # 或实际路径
git pull
git log --oneline -15  # 确认 commit 已就位
```

- [ ] **Step 3: 重建容器**

```bash
cd ~/kefu-ai
docker compose up -d --build --force-recreate
docker compose logs --tail 100 kefu-backend
```

**关注日志关键字：**
- `[answer-backfill] 检测到 N 条 entry 但答案向量空,开始回填...` → 启动钩子触发
- `[answer-backfill] 完成 X/N` → 回填完成

如果日志说 `跳过(entries=N, answers=N)` 表示已经回填过，跳过。

- [ ] **Step 4: 拿 admin cookie 跑存量回扫**

用户在 admin 后台登录后，在 Chrome DevTools 拿到 cookie，或直接在 admin 页面打开 DevTools Console 跑：

```javascript
// 在 admin.html 页面 Console 跑
fetch('/api/admin/candidates/rescan-pending', {method:'POST', credentials:'include'}).then(r=>r.json()).then(console.log)
```

或服务器内 curl（需要拿到 admin token）：

```bash
curl -s -X POST -b "session=YOUR_SESSION" https://kefu.sunyeupupup.com/api/admin/candidates/rescan-pending | python3 -m json.tool
```

预期返回：
```json
{
  "total": 200+,
  "suggested": 100+,
  "refreshed_similar": 200+,
  "errors": 0
}
```

`suggested` 数字告诉你有多少 pending 候选被打上了"建议合并"标签。

- [ ] **Step 5: 浏览器进 admin → 话术挖掘 tab → 验证**

预期看到：
- 顶部计数：`待审 X · 已合并 0 · 已新增 Y · 已丢弃 Z`
- 候选卡片：相当一部分卡片头上有绿色的"📚 系统判断 ... 高度相似"块和"✅ 合并到此问题"按钮
- 点一个"合并到此问题" → toast 显示成功 → 卡片消失（pending 变 merged）
- 切到"已合并" tab → 看到刚才合并的那张

- [ ] **Step 6: 跑一次手动扫描，看新候选的行为**

仍在 admin 页面 Console：
```javascript
fetch('/api/admin/candidates/scan', {method:'POST', credentials:'include'}).then(r=>r.json()).then(console.log)
```

预期返回（关注 `merge_suggested` 字段）：
```json
{
  "total": ...,
  "merge_suggested": K,  // 新增字段
  ...
}
```

- [ ] **Step 7: 调阈值（看跑完两天后决定）**

如果发现合并建议过多（误归并） → 阈值调高：
```bash
# server 上修改 .env 或 docker-compose env
CANDIDATE_ANSWER_MATCH_THRESHOLD=0.94
docker compose up -d --force-recreate  # 必须 force-recreate,memory 已记录该坑
```

如果发现合并建议太少（漏归并） → 阈值调低到 0.88。

---

## Self-Review（写给我自己看的检查清单）

完成所有 Task 后逐项核对：

- [ ] **Spec § 4.1 rag_answer_vectors 表** ✓ Task 1
- [ ] **Spec § 4.2 candidate_phrases 3 个新列** ✓ Task 1
- [ ] **Spec § 4.3 status 新增 merged** ✓ Task 8（merge 接口里写 status='merged'）
- [ ] **Spec § 5.1 answer_store.py 新模块** ✓ Task 3
- [ ] **Spec § 5.2 candidate_miner 答案侧匹配** ✓ Task 7
- [ ] **Spec § 5.3.1 adopt 后写答案向量** ✓ Task 4
- [ ] **Spec § 5.3.2 merge 接口** ✓ Task 8
- [ ] **Spec § 5.3.3 GET candidates 扩展** ✓ Task 9
- [ ] **Spec § 5.3.4 backfill-answer-vectors** ✓ Task 5
- [ ] **Spec § 5.3.5 rescan-pending** ✓ Task 10
- [ ] **Spec § 5.4 启动懒回填** ✓ Task 6
- [ ] **Spec § 5.5 admin.html 改造** ✓ Task 11
- [ ] **Spec § 6 阈值 + env** ✓ Task 2
- [ ] **Spec § 7 部署步骤** ✓ Task 12

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 阿里 embedding API 限流/挂掉 | scan/rescan 单条 embed 失败降级走原流程,不阻断 |
| schema 迁移失败 | 所有 ADD COLUMN 用 try/except 包；新表 IF NOT EXISTS |
| 阈值默认 0.92 过于激进 | env 可改,改完 force-recreate 30 秒生效 |
| 误合并 | "严"匹配 + 人工点确认双保险,所有 merged 可在"已合并"tab 回溯 |
| 现有 candidate_miner 改动覆盖了 daily_scheduler | Task 0 先 commit 基线,Task 7 改动只在 scan 函数内部 |

**整体回滚：** 所有 commit 都可 git revert，新增的表和列对老代码不可见。

回滚命令模板（用户万一要回退）：

```bash
cd "/Users/a1-6/Desktop/所有代码/智能客服"
git log --oneline -20  # 找到 Task 0 之前的 commit
git revert <从 Task 1 到 Task 12 的每个 commit>
git push  # 视情况
# 服务器 git pull + docker compose up -d --build --force-recreate
```

---

## 工作量复核（实际）

预计实施 ≈6h，分配：
- Task 0：5 min
- Task 1：20 min
- Task 2：10 min
- Task 3：40 min（含验证脚本）
- Task 4：20 min
- Task 5：30 min
- Task 6：30 min
- Task 7：60 min（candidate_miner 改动最大，需要仔细对照原代码）
- Task 8：30 min
- Task 9：30 min
- Task 10：40 min
- Task 11：60 min（UI 改动 + CSS）
- Task 12：30 min（部署 + 跑回扫 + 浏览器手验，等 docker build 时间不算）

合计 ≈ 6h 实际编码 + 30 min 调阈值。
