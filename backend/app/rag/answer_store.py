"""答案侧向量存取 —— 与 rag/store.py 结构对齐,操作 rag_answer_vectors 表。

用途: 用客服回复 embed 跟已采纳 entry 的 best_answer 向量比对相似度,
作为话术挖掘的答案侧去重信号。
"""
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
