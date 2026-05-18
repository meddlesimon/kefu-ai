# 话术挖掘 · 答案侧去重 + 合并审核改造

- 创建日期：2026-05-18
- 涉及系统：教培 AI 客服助手（kefu-backend，118.25.186.95:8098，kefu.sunyeupupup.com）
- 状态：设计阶段 / 待用户最终确认 → 进入实施计划

---

## 1. 背景与问题

当前话术挖掘流程（`backend/app/candidate_miner.py` + `main.py:43` 启动的 daily cron）每天凌晨 5 点扫描前一天客服回复，LLM 评估后入 `candidate_phrases` 待人工审核。**实际运行后审核员每天面对 200~300 条待审，且大量待审"换个问法的同类问题"（如 "账号怎么领" / "账号咋使用" / "账号的操作步骤是什么"），需要反复手写几乎相同的答案，主观体验是"越用越累，AI 没有帮上忙"。**

根因（在当前代码中实际定位）：

| 现有去重逻辑 | 位置 | 问题 |
|---|---|---|
| 客服回复完全文本相同（SHA1 前 200 字） | `candidate_miner.py:99-100, 133-136` | 只挡一字不差的复制粘贴 |
| 家长 query embedding 检索 `rag_entries` 已采纳变体，相似度 ≥ 0.85 跳过 | `candidate_miner.py:167-174` | 阈值对 Qwen3-Embedding 太高；只比"问句"不比"答案"；不检查 pending 同伴 |
| AI 一字不差被采纳（±60s） | `candidate_miner.py:177-187` | 只挡 AI 草稿被复用的情况 |

LLM 评估生成的 `suggested_variants`（每条 4 个问法变体）存了但未参与任何去重判断，**完全浪费**。

---

## 2. 目标

降低审核员每日待审压力，从 200~300 条降到 30~80 条量级；其中大部分待审应能通过**一键"合并到已有话术"**完成，无需重写答案。

不目标：
- 不做家长侧问句的"自动合并"（保持人工审核 gate）
- 不引入新的 AI 自动触发（本次纯算 embedding + 复用已有 LLM 评估）
- 不修改家长侧主流程（家长发问 → 客服 sidebar 拉草稿）的现有行为

---

## 3. 核心思路

**新增"答案侧相似度"作为去重信号。**

库里每条已采纳话术（`rag_entries`）都有一个 `best_answer`。我们给所有 `best_answer` 也做 embedding，存入新的向量库 `rag_answer_vectors`。

每条客服回复扫描时：
1. embed `staff_reply`
2. 跟所有 `rag_answer_vectors` 求余弦相似度
3. 若 max 相似度 ≥ **严阈值**（初始 0.92，env 可调）→ 跳过 LLM 评估，直接写候选并标记 `suggested_merge_entry_id`
4. 否则走原 LLM 评估路径

审核 UI 改造：每张待审卡片显示"建议合并到 #XX"（如有）+ Top 3 最像的老问题，提供一键"合并到此问题"按钮，仅把家长 query 作为新 variant 追加到该 entry，不触碰原 best_answer。

---

## 4. 数据模型变更

### 4.1 新表 `rag_answer_vectors`

```sql
CREATE TABLE IF NOT EXISTS rag_answer_vectors (
    entry_id     INTEGER PRIMARY KEY,
    vector       BLOB NOT NULL,
    embedded_at  REAL DEFAULT (strftime('%s','now'))
);
```

存放 `rag_entries.best_answer` 的 1024 维 float32 向量。与 `rag_variants` 完全隔离（不污染家长侧检索）。`entry_id` 同时是主键和逻辑 FK（删 entry 时手动级联清理，与 `rag_variants` 一致风格）。

### 4.2 扩展 `candidate_phrases`（追加列，老库 ALTER 兼容）

```sql
ALTER TABLE candidate_phrases ADD COLUMN suggested_merge_entry_id INTEGER;
ALTER TABLE candidate_phrases ADD COLUMN answer_match_similarity  REAL;
ALTER TABLE candidate_phrases ADD COLUMN similar_top_n_cached     TEXT;
```

- `suggested_merge_entry_id`：若扫描时答案侧命中阈值，写入对应 entry id；UI 用它决定是否高亮"建议合并"块。
- `answer_match_similarity`：诊断用，记录命中时的相似度（便于事后调阈值）。
- `similar_top_n_cached`：JSON 数组，缓存"家长侧 Top 3 相似已有话术"的快照（结构见 § 5.3.3）。**在 insert 时和 rescan-pending 时计算并写入**，列表接口直接读，避免每次刷页都重新 embed 200+ parent_query。允许轻微过期，影响仅是 UI 上展示的相似项可能少了一条新近采纳的（不影响判断正确性）。

### 4.3 状态值扩展

`candidate_phrases.status` 现有 `pending | adopted | ignored`，新增 `merged`：表示已合并到某个已有 entry（不创建新 entry）。在 `rag_entry_id` 字段写入目标 entry id。

---

## 5. 代码模块变更

### 5.1 新文件 `backend/app/rag/answer_store.py`

结构对齐 `rag/store.py`，但独立缓存、独立表：

- `add_answer_vector(entry_id, vector)` —— 单条/批量 upsert
- `delete_answer_vector(entry_id)`
- `search_answer(query_vec, top_k=5)` → `[{entry_id, _similarity}, ...]`
- `count()` —— 统计行数（用于启动时判断是否需要自动 backfill）
- 模块级 numpy 缓存 + 锁，invalidate 策略同 `store.py`

### 5.2 `candidate_miner.py` 流程改造

`scan(since, until)` 在现有"配对家长 query"成功后，**插入新的答案侧匹配分支**：

```
... 已有: 取客服回复, len 检查, hash 去重, 找家长 query ...

# NEW: 答案侧匹配
reply_vec = await rag_embed.embed_one(content)
matches = await answer_store.search_answer(reply_vec, top_k=1)
similar_top_n = await rag_retrieve.retrieve(parent_query, top_k=3, customer_id="")
similar_top_n_json = json.dumps([_compact(x) for x in similar_top_n], ensure_ascii=False)

if matches and matches[0]["_similarity"] >= ANSWER_MATCH_THRESHOLD:
    # 高度相似 → 写"合并建议"候选,跳过 LLM 评估
    insert into candidate_phrases(
      parent_query, staff_reply,
      cleaned_reply = content,                              # 占位,合并时不会用到
      suggested_category = "(待合并)",
      suggested_variants = json([parent_query]),
      llm_score = matches[0]["_similarity"] * 10,           # 凑数,用相似度 ×10
      llm_reason = "客服回复与已采纳话术答案高度相似",
      suggested_merge_entry_id = matches[0]["entry_id"],
      answer_match_similarity  = matches[0]["_similarity"],
      similar_top_n_cached     = similar_top_n_json,
      ...
    )
    stats["merge_suggested"] += 1
    continue

# 否则走现有流程: 家长侧 RAG 检索 → LLM 评估 → score 阈值 → 入候选
# 入候选时同样写入 similar_top_n_cached (与上面的 retrieve 复用)
```

新增 `stats["merge_suggested"]` 维度，便于日志观测。

阈值 `ANSWER_MATCH_THRESHOLD` 从 `app.config.settings` 读，默认 0.92，env 名 `CANDIDATE_ANSWER_MATCH_THRESHOLD`。

`_compact()` 把 retrieve 返回的完整字典裁剪为 UI 需要的最小字段：`{entry_id, category, best_answer_preview(前 80 字), similarity}`。

### 5.3 `api_admin.py` 接口变更

#### 5.3.1 修改 `POST /api/admin/candidates/{cid}/adopt`

候选采纳并创建新 entry 后，额外把 `best_answer` embed → 写入 `rag_answer_vectors`：

```python
answer_vec = await rag_embed.embed_one(answer)
await answer_store.add_answer_vector(entry_id, answer_vec)
```

#### 5.3.2 新增 `POST /api/admin/candidates/{cid}/merge`

请求体：
```json
{ "entry_id": 87 }
```

行为：
1. 校验候选 status = 'pending'
2. 校验 entry 存在
3. 把候选的 `parent_query` 作为新 variant embed → 追加到 `rag_variants`（entry_id = 目标 entry）
4. 候选 status='merged'，`rag_entry_id` = entry_id，记录 reviewer / reviewed_at
5. 返回 `{merged_into: entry_id, added_variants: 1}`

错误码：
- 404 候选不存在 / entry 不存在
- 400 候选已处理 / parent_query 为空
- 409 该 variant 文本已存在于该 entry（同 entry 同 variant_text 视为重复）

#### 5.3.3 修改 `GET /api/admin/candidates`

返回项追加：
```json
{
  ...原有字段...,
  "suggested_merge": {
    "entry_id": 87,
    "category": "账号使用",
    "best_answer_preview": "登录学习机后,点击「我的-我的账号」...",
    "similarity": 0.934
  } | null,
  "similar_top_n": [
    { "entry_id": 92, "category": "账号激活", "best_answer_preview": "...", "similarity": 0.87 },
    ...up to 3...
  ]
}
```

**两个字段都来自候选自身的已缓存数据，列表接口不做实时 embed：**
- `suggested_merge` —— 由 `suggested_merge_entry_id` JOIN `rag_entries` 拿 category + best_answer 前 80 字，相似度从 `answer_match_similarity` 取
- `similar_top_n` —— 直接读 `similar_top_n_cached` JSON 列（在扫描时 / rescan-pending 时已计算并写入）

这样 200+ 候选的列表加载只是单次 SQL，不发任何外部 API。

#### 5.3.4 新增 `POST /api/admin/rag/backfill-answer-vectors`

一次性回填历史 `rag_entries.best_answer` 的 embedding。幂等：跳过已存在 `rag_answer_vectors.entry_id`。返回 `{embedded: N, skipped: M}`。

#### 5.3.5 新增 `POST /api/admin/candidates/rescan-pending`

遍历所有 `status='pending'` 的候选，**分批跑 embed**（每批 25 条上限按阿里 API 限制）：

每批内：
1. 拿 25 条 `staff_reply` 批量 embed → 25 个回复向量
2. 拿 25 条 `parent_query` 批量 embed → 25 个问句向量
3. 对每条逐个：答案库匹配 → 若命中阈值更新 `suggested_merge_entry_id` + `answer_match_similarity`；用问句向量跑 `similar_top_n` 检索 → 更新 `similar_top_n_cached`

返回 `{total: N, suggested: K, refreshed_similar: M, errors: E}`。

可选参数 `?force=true`：覆盖已有 `suggested_merge_entry_id`（默认 false，便于多次跑只补新结果）。无论 force 与否都会刷新 `similar_top_n_cached`（这个字段总应保持最新）。

预计 200 条候选 ≈ 8 批 × (回复 embed 1 + 问句 embed 1) = 16 次 API 调用，30 秒内完成。

### 5.4 `main.py` 启动钩子

`lifespan` 启动时增加幂等懒回填：

```python
if rag_entries 行数 > 0 and rag_answer_vectors 行数 == 0:
    logger.info("检测到答案向量库为空,启动后台 backfill 任务...")
    asyncio.create_task(_backfill_answer_vectors_bg())
```

这样首次部署不需要手动跑 backfill 接口。后续 entry 增删自动同步（在 adopt 接口里加，未来若有 entry 编辑接口同样要带）。

### 5.5 `static/admin.html` UI 改造

候选卡片（`refreshCandidates` 渲染函数，约第 1474 行）改动：

1. 在「家长问 / 客服原回复」下方、「LLM 脱敏建议版本」上方，插入"📚 相似已有话术"区块：
   - 若 `suggested_merge` 存在：突出展示该 entry（绿色 / 高亮边框），主按钮 "✅ 合并到此问题"
   - `similar_top_n` 列出最多 3 条，每条灰底卡，附 "合并到此问题" 次按钮
2. 现有「采纳入库」按钮文案改为「📝 另存为全新问题」（语义更清晰）
3. 现有「忽略」保持
4. 当 `suggested_merge` 存在时，默认折叠「LLM 脱敏建议版本」/「入库分类 + 变体」编辑区（节省视觉空间，需要展开时才看）

合并按钮点击行为：调 `/api/admin/candidates/{id}/merge`，body `{entry_id}`，成功后 toast + 刷新列表。

---

## 6. 阈值与可观测性

- 默认 `ANSWER_MATCH_THRESHOLD = 0.92`（严）
- env 覆盖：`CANDIDATE_ANSWER_MATCH_THRESHOLD`
- 每次扫描日志记 `stats["merge_suggested"]` 数量
- `rescan-pending` 返回详细统计，便于第一次跑完看效果决定是否调阈值
- 候选表的 `answer_match_similarity` 字段保留诊断数据，未来可加查询接口

---

## 7. 上线步骤（按依赖顺序）

1. **改 `storage.py`**：新表 + ALTER 候选表
   - 重启容器后 DB 自动迁移
2. **新建 `rag/answer_store.py`** + **改 `candidate_miner.py`** + **改 `api_admin.py`**
   - 重建容器，验证启动日志无报错
3. **手动跑 `POST /api/admin/rag/backfill-answer-vectors`**（或等启动钩子自动跑完）
   - 验证：`SELECT COUNT(*) FROM rag_answer_vectors = SELECT COUNT(*) FROM rag_entries`
4. **改 `static/admin.html`**
   - 部署后硬刷新前端，看新卡片布局
5. **跑 `POST /api/admin/candidates/rescan-pending`**
   - 验证：pending 候选中 `suggested_merge_entry_id` 非空的占比
   - 进 admin UI 验证候选卡片正确显示建议合并块
6. **审核两天**
   - 看 merge / new / ignore 三种操作的实际比例
   - 调 `CANDIDATE_ANSWER_MATCH_THRESHOLD`（建议步进 0.02）
7. 后续若每日 cron 跑完发现"建议合并"占比 > 70%，可考虑跳过 LLM 评估省 doubao 调用费（已内置）

回滚：所有改动新增、不破坏老数据；ALTER 列 nullable；新表删了即可；UI 改动局部。回退只需 git revert。

---

## 8. 安全 / 边界

- 合并接口必须 `Depends(get_admin)` 鉴权（同现有 adopt/ignore）
- 合并时 `parent_query` 已存在该 entry 的 variants 中 → 409，不重复 embed 不重复入库
- 答案 embedding 调用失败（阿里 API 限流/网络）→ 降级走原 LLM 评估路径（fail-open，不阻断扫描）
- `rescan-pending` 跑时若候选数量极大（> 1000），分批 embed 控制并发（阿里 25 条/批限制已在 `rag/embed.py:17` 处理）

---

## 9. 工作量评估

| 任务 | 工作量 |
|---|---|
| DB schema + answer_store.py | 0.5h |
| candidate_miner.py 改造 | 0.5h |
| 4 个新增/修改 API | 1.5h |
| admin.html UI 改造 | 1.5h |
| 启动钩子 + 配置 | 0.3h |
| 测试 + 部署 + 调阈值 | 1.5h |
| **合计** | **~6h** |

---

## 10. 未列入本期的优化（备忘）

- 客服侧 sidebar 显示"系统判断这条家长问题与库内 entry-X 答案相似"的实时提示
- candidate_phrases 之间互相去重（同一天若两条候选同时建议合并到同一 entry，UI 自动并卡）
- entry 编辑/删除接口（目前无此接口，若未来加要同步维护 `rag_answer_vectors`）
- 合并历史的回溯查询（哪些 variant 是从哪些 candidate 合并来的）
