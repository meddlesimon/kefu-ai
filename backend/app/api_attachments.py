"""RAG 话术附件: 图片 / 视频。

存储:
- 文件存到 /app/data/attachments/{uuid}.{ext}
- 元数据存 attachments 表

接口:
- POST /api/admin/rag/{entry_id}/attachments  - 上传 (admin auth)
- GET  /api/admin/rag/{entry_id}/attachments  - 列出 (admin auth)
- DELETE /api/admin/attachments/{att_id}      - 删除 (admin auth)
- GET  /api/attachments/{att_id}/file         - 下载文件 (无 auth, sidebar 客服直接访问)
"""
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.auth.admin_auth import get_admin
from app.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter()

ATTACHMENT_DIR = Path("/app/data/attachments")
MAX_IMAGE_SIZE = 10 * 1024 * 1024     # 10MB (企微临时素材硬限制)
MAX_VIDEO_SIZE = 10 * 1024 * 1024     # 10MB (企微临时素材硬限制)


def _ensure_dir():
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)


def _detect_kind(mime: str) -> str | None:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    return None


@router.post("/api/admin/rag/{entry_id}/attachments")
async def upload_attachment(
    entry_id: int,
    file: UploadFile = File(...),
    admin: dict = Depends(get_admin),
):
    storage = get_storage()
    if not storage.conn.execute("SELECT 1 FROM rag_entries WHERE id = ?", (entry_id,)).fetchone():
        raise HTTPException(404, "RAG 条目不存在")

    mime = (file.content_type or "application/octet-stream").lower()
    kind = _detect_kind(mime)
    if not kind:
        raise HTTPException(400, "只支持图片或视频文件")

    max_size = MAX_VIDEO_SIZE if kind == "video" else MAX_IMAGE_SIZE
    _ensure_dir()
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    file_id = str(uuid.uuid4())
    save_path = ATTACHMENT_DIR / f"{file_id}{ext}"
    size = 0
    try:
        with open(save_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise HTTPException(413, f"文件超过 {max_size // 1024 // 1024}MB 上限")
                f.write(chunk)
    except HTTPException:
        save_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        save_path.unlink(missing_ok=True)
        logger.exception("写入附件失败")
        raise HTTPException(500, f"写入失败: {e}")

    cur = storage.conn.execute(
        "INSERT INTO attachments (entry_id, file_path, mime_type, original_name, size_bytes, kind, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entry_id, str(save_path), mime, file.filename, size, kind, admin["username"]),
    )
    return {
        "id": cur.lastrowid,
        "entry_id": entry_id,
        "kind": kind,
        "mime": mime,
        "name": file.filename,
        "size": size,
    }


@router.get("/api/admin/rag/{entry_id}/attachments")
async def list_attachments(entry_id: int, admin: dict = Depends(get_admin)):
    storage = get_storage()
    rows = storage.conn.execute(
        "SELECT id, kind, mime_type, original_name, size_bytes, created_at "
        "FROM attachments WHERE entry_id = ? ORDER BY id",
        (entry_id,),
    ).fetchall()
    return [{
        "id": r[0], "kind": r[1], "mime": r[2], "name": r[3], "size": r[4], "created_at": r[5],
    } for r in rows]


@router.delete("/api/admin/attachments/{att_id}")
async def delete_attachment(att_id: int, admin: dict = Depends(get_admin)):
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT file_path FROM attachments WHERE id = ?", (att_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "附件不存在")
    Path(row[0]).unlink(missing_ok=True)
    storage.conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    return {"deleted": att_id}


@router.get("/api/attachments/{att_id}/wxwork_media_id")
async def get_wxwork_media_id(att_id: int):
    """sidebar 客服侧拿 mediaId,用于 wx.invoke('sendChatMessage', { msgtype: image|video, ... })。

    第一次或缓存过期时会同步上传到企微 (大视频可能要等 5-10s),
    后续调用从缓存返回 (毫秒级)。
    """
    from app import wxwork_media
    try:
        result = await wxwork_media.get_or_upload_media_id(att_id)
        return result
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("获取 mediaId 失败 attachment=%s", att_id)
        raise HTTPException(500, f"获取 mediaId 失败: {e}")


@router.get("/api/attachments/{att_id}/file")
async def get_attachment_file(att_id: int):
    """sidebar 客服直接访问,不需要 admin auth(知道 id 就能拿)。"""
    storage = get_storage()
    row = storage.conn.execute(
        "SELECT file_path, mime_type, original_name FROM attachments WHERE id = ?",
        (att_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "附件不存在")
    file_path, mime, name = row
    if not Path(file_path).exists():
        raise HTTPException(404, "源文件已丢失")
    return FileResponse(
        path=file_path,
        media_type=mime,
        filename=name or f"attachment_{att_id}",
        headers={"Cache-Control": "public, max-age=3600"},
    )
