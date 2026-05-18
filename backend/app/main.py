import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import api_admin, api_ai, api_attachments, api_events, api_sidebar, archive_pull, candidate_miner, wxwork_media
from app.auth import access_token, admin_auth, jsapi, oauth
from app.config import settings
from app.storage import get_storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("教培 AI 客服助手 · 后端启动中...")
    logger.info("corpid=%s agentid=%s", settings.wxwork_corpid, settings.wxwork_agentid)

    await access_token.get_access_token()

    storage = get_storage()
    logger.info("已加载消息存储, 当前累计 %d 条", storage.total_count())

    refresh_task = asyncio.create_task(access_token.refresh_loop())
    pull_task = asyncio.create_task(archive_pull.pull_loop())
    media_refresh_task = asyncio.create_task(wxwork_media.refresh_loop())
    candidate_cron_task = asyncio.create_task(candidate_miner.daily_scheduler(hour_local=5))

    try:
        yield
    finally:
        refresh_task.cancel()
        pull_task.cancel()
        media_refresh_task.cancel()
        candidate_cron_task.cancel()
        logger.info("后端关闭")


app = FastAPI(
    title="教培 AI 客服助手 · 认证服务",
    version="0.5.0",
    lifespan=lifespan,
    redirect_slashes=False,  # 关键: 避免 /sidebar/ 被 307 重定向到 http://(协议降级触发企微 Windows 客户端外部网页拦截)
)

app.include_router(oauth.router)
app.include_router(jsapi.router)
app.include_router(api_sidebar.router)
app.include_router(admin_auth.router)
app.include_router(api_admin.router)
app.include_router(api_ai.router)
app.include_router(api_attachments.router)
app.include_router(api_events.router)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/sidebar")
@app.get("/sidebar/")
async def sidebar_page():
    return FileResponse(_STATIC_DIR / "sidebar.html", headers=_NO_CACHE)


@app.get("/admin")
async def admin_page():
    return FileResponse(_STATIC_DIR / "admin.html", headers=_NO_CACHE)


@app.get("/health")
async def health():
    return {"status": "ok", "messages_total": get_storage().total_count()}


@app.get("/api/messages/latest")
async def latest_messages(limit: int = 10):
    rows = get_storage().latest_messages(limit=min(limit, 100))
    return [
        {"seq": r[0], "msgid": r[1], "received_at": r[2], "from": r[3], "msg_type": r[4], "raw_json": r[5]}
        for r in rows
    ]


@app.get("/api/auth/me")
async def auth_me(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return {"user_id": payload["user_id"], "exp": payload["exp"]}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
