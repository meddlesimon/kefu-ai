"""解析管理员粘贴的 RAG 文本(飞书复制粘贴格式)。

格式约定:
    分类 | 问法1 | 问法2 | 问法3
    答案文本(可多行)

    分类 | 问法1 | 问法2
    答案

每条 entry 之间用**空行**分隔。
"""
from typing import List, Dict


def parse_ingest_text(text: str) -> List[Dict]:
    """返回 [{category, variants:[...], best_answer}, ...]"""
    text = (text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return []

    # 用一个或多个空行分块
    raw_blocks = []
    cur = []
    for line in text.split("\n"):
        if line.strip() == "":
            if cur:
                raw_blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        raw_blocks.append("\n".join(cur))

    entries = []
    for block in raw_blocks:
        lines = block.split("\n")
        if not lines:
            continue
        header = lines[0].strip()
        # 必须含 | (没有 | 视为旧式无变体格式,跳过)
        if "|" not in header:
            continue
        parts = [p.strip() for p in header.split("|")]
        # 过滤空字段
        parts = [p for p in parts if p]
        if len(parts) < 2:
            continue
        category = parts[0]
        variants = parts[1:]
        answer = "\n".join(lines[1:]).strip()
        if not answer:
            continue
        entries.append({
            "category": category,
            "variants": variants,
            "best_answer": answer,
        })
    return entries
