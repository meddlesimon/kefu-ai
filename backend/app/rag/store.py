"""向量存取 — 用 SQLite BLOB 存 float32 序列 + 内存 numpy 矩阵做余弦检索。

为什么不用 lancedb: 它的 C 扩展和企微 SDK (libWeWorkFinanceSdk_C.so) 共用一个进程时,
glibc malloc 报 free(): invalid pointer,容器崩溃。numpy 纯算够用 (几百条 1024 维余弦
搜索 < 5ms)。
"""
import asyncio
import threading
import uuid
from typing import List

import numpy as np

from app.config import settings
from app.storage import get_storage


_lock = threading.Lock()
_cache_vectors: np.ndarray | None = None
_cache_meta: list | None = None


def _ensure_schema():
    storage = get_storage()
    storage.conn.execute("""
        CREATE TABLE IF NOT EXISTS rag_variants (
            variant_id   TEXT PRIMARY KEY,
            entry_id     INTEGER NOT NULL,
            variant_text TEXT NOT NULL,
            vector       BLOB NOT NULL
        )
    """)
    storage.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_variants_entry ON rag_variants(entry_id)"
    )


def _vec_to_bytes(vec: List[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _bytes_to_vec(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def _invalidate_cache():
    global _cache_vectors, _cache_meta
    with _lock:
        _cache_vectors = None
        _cache_meta = None


def _load_cache():
    global _cache_vectors, _cache_meta
    if _cache_vectors is not None:
        return _cache_vectors, _cache_meta
    with _lock:
        if _cache_vectors is not None:
            return _cache_vectors, _cache_meta
        _ensure_schema()
        storage = get_storage()
        rows = storage.conn.execute(
            "SELECT variant_id, entry_id, variant_text, vector FROM rag_variants"
        ).fetchall()
        if not rows:
            _cache_vectors = np.zeros((0, settings.aliyun_embed_dim), dtype=np.float32)
            _cache_meta = []
        else:
            _cache_meta = [
                {"variant_id": r[0], "entry_id": r[1], "variant_text": r[2]}
                for r in rows
            ]
            _cache_vectors = np.stack([_bytes_to_vec(r[3]) for r in rows])
        return _cache_vectors, _cache_meta


def _add_sync(rows: List[dict]):
    _ensure_schema()
    storage = get_storage()
    for row in rows:
        storage.conn.execute(
            "INSERT OR REPLACE INTO rag_variants "
            "(variant_id, entry_id, variant_text, vector) VALUES (?, ?, ?, ?)",
            (row["variant_id"], row["entry_id"], row["variant_text"], _vec_to_bytes(row["vector"])),
        )
    _invalidate_cache()


async def add_variants(entry_id: int, variant_texts: List[str], vectors: List[List[float]]):
    if len(variant_texts) != len(vectors):
        raise ValueError("variant_texts 和 vectors 长度不一致")
    if not variant_texts:
        return
    rows = [{
        "variant_id": str(uuid.uuid4()),
        "entry_id": int(entry_id),
        "variant_text": text,
        "vector": list(vec),
    } for text, vec in zip(variant_texts, vectors)]
    await asyncio.to_thread(_add_sync, rows)


def _search_sync(query_vec: List[float], top_k: int) -> List[dict]:
    vectors, meta = _load_cache()
    if len(meta) == 0:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0:
        return []
    v_norms = np.linalg.norm(vectors, axis=1)
    v_norms = np.where(v_norms == 0, 1.0, v_norms)
    sims = (vectors @ q) / (q_norm * v_norms)  # 余弦相似度 [-1, 1]
    k = min(top_k, len(sims))
    # 取 top_k 索引(降序)
    if k == len(sims):
        idx = np.argsort(-sims)
    else:
        idx = np.argpartition(-sims, k)[:k]
        idx = idx[np.argsort(-sims[idx])]
    return [{
        **meta[i],
        "_distance": float(2.0 * (1.0 - sims[i])),  # 转 L2² 兼容 (越小越好)
        "_similarity": float(sims[i]),  # 直接给余弦相似度也行
    } for i in idx]


async def search(query_vec: List[float], top_k: int = 12) -> List[dict]:
    return await asyncio.to_thread(_search_sync, query_vec, top_k)


def _delete_sync(entry_id: int):
    _ensure_schema()
    storage = get_storage()
    storage.conn.execute("DELETE FROM rag_variants WHERE entry_id = ?", (int(entry_id),))
    _invalidate_cache()


async def delete_by_entry(entry_id: int):
    await asyncio.to_thread(_delete_sync, entry_id)


def _count_sync() -> int:
    _ensure_schema()
    storage = get_storage()
    row = storage.conn.execute("SELECT COUNT(*) FROM rag_variants").fetchone()
    return row[0] if row else 0


async def count() -> int:
    return await asyncio.to_thread(_count_sync)
