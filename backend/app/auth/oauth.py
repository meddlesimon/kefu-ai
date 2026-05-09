import logging
import time

import httpx
import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth.access_token import get_access_token
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_GETUSERINFO_URL = "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo"


class OAuthRequest(BaseModel):
    code: str


class OAuthResponse(BaseModel):
    user_id: str
    jwt: str
    expires_at: int


@router.post("/api/auth/oauth_callback", response_model=OAuthResponse)
async def oauth_callback(req: OAuthRequest):
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _GETUSERINFO_URL,
            params={"access_token": token, "code": req.code},
        )
        data = resp.json()

    if data.get("errcode") != 0:
        logger.warning("OAuth getuserinfo 失败: %s", data)
        raise HTTPException(status_code=401, detail=f"OAuth 失败: {data}")

    # 没有 userid 字段意味着是外部联系人(家长扫码),不是企业内部成员
    user_id = data.get("userid")
    if not user_id:
        raise HTTPException(status_code=403, detail="非企业内部成员,禁止访问")

    expires_at = int(time.time()) + settings.jwt_ttl_seconds
    payload = {
        "user_id": user_id,
        "exp": expires_at,
        "iss": "kefu-backend",
    }
    token_str = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")

    logger.info("客服 %s 登录成功", user_id)
    return OAuthResponse(user_id=user_id, jwt=token_str, expires_at=expires_at)
