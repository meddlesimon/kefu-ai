import hashlib
import secrets
import time

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.auth.access_token import get_agent_jsapi_ticket, get_jsapi_ticket
from app.config import settings

router = APIRouter()


class JsapiSignature(BaseModel):
    appId: str
    timestamp: int
    nonceStr: str
    signature: str


class AgentSignature(BaseModel):
    corpid: str
    agentid: str
    timestamp: int
    nonceStr: str
    signature: str


def _sign(ticket: str, url: str) -> tuple[str, int, str]:
    nonce_str = secrets.token_hex(8)
    timestamp = int(time.time())
    # 企微规则: 字段按字典序拼接 + SHA1
    raw = f"jsapi_ticket={ticket}&noncestr={nonce_str}&timestamp={timestamp}&url={url}"
    signature = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return nonce_str, timestamp, signature


@router.get("/api/auth/jsapi_signature", response_model=JsapiSignature)
async def jsapi_signature(url: str = Query(..., description="当前页 URL,不含 #hash")):
    """企业级签名,用于 wx.config(...)。"""
    ticket = await get_jsapi_ticket()
    nonce_str, timestamp, signature = _sign(ticket, url)
    return JsapiSignature(
        appId=settings.wxwork_corpid,
        timestamp=timestamp,
        nonceStr=nonce_str,
        signature=signature,
    )


@router.get("/api/auth/jsapi_signature_agent", response_model=AgentSignature)
async def jsapi_signature_agent(url: str = Query(..., description="当前页 URL,不含 #hash")):
    """应用级签名,用于 wx.agentConfig(...);之后才能调用 getCurExternalContact 等接口。"""
    ticket = await get_agent_jsapi_ticket()
    nonce_str, timestamp, signature = _sign(ticket, url)
    return AgentSignature(
        corpid=settings.wxwork_corpid,
        agentid=settings.wxwork_agentid,
        timestamp=timestamp,
        nonceStr=nonce_str,
        signature=signature,
    )
