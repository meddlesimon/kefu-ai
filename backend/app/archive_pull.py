"""会话存档拉取循环。

启动后每 settings.pull_interval_seconds 秒调一次 SDK GetChatData,
解密后存进 SQLite, 推进 cursor。

解密 = Python 用 RSA 私钥解 encrypt_random_key -> 拿到 AES key 字符串
   -> SDK DecryptData(AES key, encrypt_chat_msg) -> 明文 JSON。
"""
import asyncio
import base64
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding

from app import auto_ai_draft
from app.config import settings
from app.storage import get_storage
from app.wxwork_sdk import SdkError, WxworkSdk

logger = logging.getLogger(__name__)


_sdk: Optional[WxworkSdk] = None


def get_sdk() -> WxworkSdk:
    global _sdk
    if _sdk is None:
        _sdk = WxworkSdk(
            corpid=settings.wxwork_corpid,
            secret=settings.wxwork_archive_secret,
        )
    return _sdk


@lru_cache(maxsize=1)
def _load_private_key():
    pem = Path(settings.wxwork_private_key_path).read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def _rsa_decrypt_aes_key(encrypt_random_key: str) -> str:
    """RSA 私钥解 encrypt_random_key,返回 AES key 字符串(供 SDK DecryptData 用)。"""
    key = _load_private_key()
    cipher = base64.b64decode(encrypt_random_key)
    plain = key.decrypt(cipher, asymmetric_padding.PKCS1v15())
    # 企微 SDK 期望 char* — RSA 解密结果本身就是 ASCII 字符串(base64 之类)
    return plain.decode("utf-8")


def _pull_batch_sync(sdk: WxworkSdk, seq: int) -> list:
    """阻塞拉一批,返回 chat_data 列表(SDK 调用,不能 await)。"""
    return sdk.fetch_chat(seq=seq, limit=1000, timeout=10)


async def pull_once() -> int:
    """拉一次,返回成功保存的消息数。"""
    storage = get_storage()
    sdk = get_sdk()

    cursor = storage.get_cursor()
    chat_data = await asyncio.to_thread(_pull_batch_sync, sdk, cursor)

    if not chat_data:
        return 0

    saved = 0
    for item in chat_data:
        seq = item.get("seq")
        msgid = item.get("msgid")
        encrypt_random_key = item.get("encrypt_random_key")
        encrypt_chat_msg = item.get("encrypt_chat_msg")
        if not (seq is not None and msgid and encrypt_random_key and encrypt_chat_msg):
            logger.warning("会话存档项缺字段, 跳过: %s", item)
            continue

        try:
            aes_key_str = _rsa_decrypt_aes_key(encrypt_random_key)
            msg = await asyncio.to_thread(sdk.decrypt, aes_key_str, encrypt_chat_msg)
        except Exception:
            logger.exception("解密 seq=%s 失败,停止本批拉取(下次重试)", seq)
            # 不推进 cursor — 下次重试这条
            break

        is_new = storage.save_message(seq, msgid, msg)
        storage.set_cursor(seq)
        if is_new:
            saved += 1
            # 家长(外部联系人)发的文本消息 → 调度自动 AI 草稿生成
            from_user = msg.get("from") or ""
            msg_type = msg.get("msgtype")
            if from_user.startswith("wm") and msg_type == "text":
                tolist = msg.get("tolist") or []
                to_user = tolist[0] if tolist else None
                try:
                    logger.info("[auto-draft] 调度 seq=%s customer=%s to=%s", seq, from_user, to_user)
                    await auto_ai_draft.schedule_for_customer(from_user, to_user)
                except Exception:
                    logger.exception("[auto-draft] 调度失败")

    return saved


async def pull_loop():
    """后台循环。失败采用指数退避(从 interval 起,翻倍到 max 5 分钟)。"""
    interval = settings.pull_interval_seconds
    backoff = interval
    max_backoff = 300

    logger.info("会话存档拉取循环启动, 间隔 %d 秒", interval)

    while True:
        try:
            count = await pull_once()
            if count > 0:
                logger.info("拉到 %d 条新消息(累计 %d)", count, get_storage().total_count())
            backoff = interval
            await asyncio.sleep(interval)
        except SdkError as e:
            logger.error("会话存档 SDK 失败: %s, 退避 %d 秒", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        except asyncio.CancelledError:
            logger.info("会话存档拉取循环停止")
            raise
        except Exception:
            logger.exception("会话存档循环异常, 退避 %d 秒", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
