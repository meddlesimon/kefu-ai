"""企微临时素材上传 + mediaId 缓存。

企微的 sendChatMessage(image/video/file) 必须传 mediaid,而 mediaid 来自:
  POST /cgi-bin/media/upload?access_token=xxx&type=image|video|file
  返回的 media_id 临时有效 3 天。

策略:
- 客服点"发送" → 后端检查缓存
- 缓存内 (< 2 天 8 小时) 直接返回
- 否则现传 → 缓存 → 返回
"""
import asyncio
import logging
import time
from pathlib import Path

import httpx

from app.auth.access_token import get_access_token
from app.storage import get_storage

logger = logging.getLogger(__name__)

# 企微 3 天过期 (259200s),提前 18 小时刷新
EXPIRE_SECS = 3 * 24 * 3600
REFRESH_MARGIN = 18 * 3600
SAFE_TTL = EXPIRE_SECS - REFRESH_MARGIN  # 实际可用 ~2.25 天

# 企微临时素材类型限制 (官方文档):
#   image: 10MB, 支持 JPG/PNG
#   voice: 2MB
#   video: 10MB, 支持 MP4
#   file:  20MB
# 我们 admin 上传时限制更宽,实际发送时如果超过企微限制会失败,需要回退方案
WXWORK_LIMITS = {
    "image": 10 * 1024 * 1024,
    "video": 10 * 1024 * 1024,
    "file":  20 * 1024 * 1024,
}

_lock = asyncio.Lock()


async def _upload_to_wxwork(file_path: str, media_type: str) -> str:
    token = await get_access_token()
    url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type={media_type}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        with open(file_path, "rb") as f:
            files = {"media": (Path(file_path).name, f, "application/octet-stream")}
            resp = await client.post(url, files=files)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企微上传失败: {data}")
    return data["media_id"]


async def get_or_upload_media_id(attachment_id: int) -> dict:
    """返回 {media_id, kind, cached(bool)}。"""
    storage = get_storage()
    now = time.time()

    # 1) 看缓存
    row = storage.conn.execute(
        "SELECT media_id, uploaded_at FROM attachment_wxwork_media WHERE attachment_id = ?",
        (attachment_id,),
    ).fetchone()
    if row and (now - row[1]) < SAFE_TTL:
        # 顺带拿一下 kind
        att_row = storage.conn.execute(
            "SELECT kind FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        return {"media_id": row[0], "kind": att_row[0] if att_row else "image", "cached": True}

    # 2) 不命中,加锁防并发上传
    async with _lock:
        row = storage.conn.execute(
            "SELECT media_id, uploaded_at FROM attachment_wxwork_media WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        if row and (time.time() - row[1]) < SAFE_TTL:
            att_row = storage.conn.execute(
                "SELECT kind FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return {"media_id": row[0], "kind": att_row[0] if att_row else "image", "cached": True}

        # 3) 拿源文件 + 校验大小
        att = storage.conn.execute(
            "SELECT file_path, kind, size_bytes FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
        if not att:
            raise RuntimeError(f"attachment {attachment_id} 不存在")
        file_path, kind, size_bytes = att
        if not Path(file_path).exists():
            raise RuntimeError("源文件已丢失")

        wxwork_type = "video" if kind == "video" else "image"
        max_size = WXWORK_LIMITS.get(wxwork_type, 10 * 1024 * 1024)
        if size_bytes > max_size:
            raise RuntimeError(
                f"文件 {size_bytes // 1024 // 1024}MB 超过企微 {wxwork_type} {max_size // 1024 // 1024}MB 限制,无法用 mediaId 发送(请改用下载方案)"
            )

        # 4) 上传
        logger.info("上传 attachment %s 到企微 (type=%s, size=%.1fMB)",
                    attachment_id, wxwork_type, size_bytes / 1024 / 1024)
        media_id = await _upload_to_wxwork(file_path, wxwork_type)

        # 5) 写缓存
        storage.conn.execute(
            "INSERT INTO attachment_wxwork_media (attachment_id, media_id, uploaded_at) VALUES (?, ?, ?) "
            "ON CONFLICT(attachment_id) DO UPDATE SET media_id=excluded.media_id, uploaded_at=excluded.uploaded_at",
            (attachment_id, media_id, time.time()),
        )
        return {"media_id": media_id, "kind": kind, "cached": False}


# ========== 后台自动续期 ==========
# 企微 mediaId 3 天过期。我们每 12 小时扫一次,把超过 1.5 天没续过的全部重传一遍。
# 这样客服任何时刻点击,都拿到新鲜 mediaId,不会卡。
REFRESH_INTERVAL = 12 * 3600          # 每 12 小时扫一次
REFRESH_THRESHOLD = 1.5 * 24 * 3600   # 超过 1.5 天的就续期


async def refresh_loop():
    """后台续期循环,在 main.py lifespan 里启。"""
    # 启动时先等 1 分钟,避开容器启动尖峰
    await asyncio.sleep(60)
    while True:
        try:
            await _refresh_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mediaId 续期循环出错")
        try:
            await asyncio.sleep(REFRESH_INTERVAL)
        except asyncio.CancelledError:
            raise


async def _refresh_once():
    storage = get_storage()
    now = time.time()
    rows = storage.conn.execute(
        "SELECT m.attachment_id, a.file_path, a.kind "
        "FROM attachment_wxwork_media m JOIN attachments a ON m.attachment_id = a.id "
        "WHERE m.uploaded_at < ?",
        (now - REFRESH_THRESHOLD,),
    ).fetchall()
    if not rows:
        logger.info("mediaId 续期: 无待刷新条目")
        return
    logger.info("mediaId 续期: 发现 %d 条快过期,开始重传", len(rows))
    ok = 0
    for att_id, file_path, kind in rows:
        try:
            if not Path(file_path).exists():
                logger.warning("续期跳过 attachment %s: 源文件丢失 %s", att_id, file_path)
                continue
            wxwork_type = "video" if kind == "video" else "image"
            new_media_id = await _upload_to_wxwork(file_path, wxwork_type)
            storage.conn.execute(
                "UPDATE attachment_wxwork_media SET media_id=?, uploaded_at=? WHERE attachment_id=?",
                (new_media_id, time.time(), att_id),
            )
            ok += 1
        except Exception:
            logger.exception("续期 attachment %s 失败", att_id)
    logger.info("mediaId 续期: %d/%d 成功", ok, len(rows))
