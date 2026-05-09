import asyncio
import logging
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_GET_TICKET_URL = "https://qyapi.weixin.qq.com/cgi-bin/get_jsapi_ticket"          # corp 级
_GET_AGENT_TICKET_URL = "https://qyapi.weixin.qq.com/cgi-bin/ticket/get"           # agent 级

_EARLY_EXPIRE_BUFFER = 600
_REFRESH_INTERVAL = 90 * 60


class _Cache:
    access_token: Optional[str] = None
    access_token_expires_at: float = 0
    jsapi_ticket: Optional[str] = None
    jsapi_ticket_expires_at: float = 0
    agent_jsapi_ticket: Optional[str] = None
    agent_jsapi_ticket_expires_at: float = 0


_cache = _Cache()


async def _fetch_access_token() -> tuple[str, int]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _GET_TOKEN_URL,
            params={
                "corpid": settings.wxwork_corpid,
                "corpsecret": settings.wxwork_app_secret,
            },
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        return data["access_token"], data.get("expires_in", 7200)


async def _fetch_jsapi_ticket(access_token: str) -> tuple[str, int]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(_GET_TICKET_URL, params={"access_token": access_token})
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 jsapi_ticket(corp) 失败: {data}")
        return data["ticket"], data.get("expires_in", 7200)


async def _fetch_agent_jsapi_ticket(access_token: str) -> tuple[str, int]:
    """agent_config 专用 ticket — wx.agentConfig 必需。"""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _GET_AGENT_TICKET_URL,
            params={"access_token": access_token, "type": "agent_config"},
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 agent_jsapi_ticket 失败: {data}")
        return data["ticket"], data.get("expires_in", 7200)


async def get_access_token() -> str:
    if _cache.access_token and time.time() < _cache.access_token_expires_at:
        return _cache.access_token
    token, expires_in = await _fetch_access_token()
    _cache.access_token = token
    _cache.access_token_expires_at = time.time() + expires_in - _EARLY_EXPIRE_BUFFER
    logger.info("access_token 已获取,过期时间 %ds", expires_in)
    return token


async def get_jsapi_ticket() -> str:
    if _cache.jsapi_ticket and time.time() < _cache.jsapi_ticket_expires_at:
        return _cache.jsapi_ticket
    token = await get_access_token()
    ticket, expires_in = await _fetch_jsapi_ticket(token)
    _cache.jsapi_ticket = ticket
    _cache.jsapi_ticket_expires_at = time.time() + expires_in - _EARLY_EXPIRE_BUFFER
    logger.info("jsapi_ticket(corp) 已获取,过期时间 %ds", expires_in)
    return ticket


async def get_agent_jsapi_ticket() -> str:
    if _cache.agent_jsapi_ticket and time.time() < _cache.agent_jsapi_ticket_expires_at:
        return _cache.agent_jsapi_ticket
    token = await get_access_token()
    ticket, expires_in = await _fetch_agent_jsapi_ticket(token)
    _cache.agent_jsapi_ticket = ticket
    _cache.agent_jsapi_ticket_expires_at = time.time() + expires_in - _EARLY_EXPIRE_BUFFER
    logger.info("jsapi_ticket(agent) 已获取,过期时间 %ds", expires_in)
    return ticket


async def refresh_loop():
    while True:
        try:
            await asyncio.sleep(_REFRESH_INTERVAL)
            _cache.access_token_expires_at = 0
            _cache.jsapi_ticket_expires_at = 0
            _cache.agent_jsapi_ticket_expires_at = 0
            await get_access_token()
            await get_jsapi_ticket()
            await get_agent_jsapi_ticket()
        except Exception:
            logger.exception("token / ticket 刷新失败")
