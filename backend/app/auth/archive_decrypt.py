"""会话存档密文解密。

企微会话存档每条消息含两个 base64 字段:
  encrypt_random_key  -- 用我方 RSA 公钥加密的 AES key
  encrypt_chat_msg    -- 用 AES key 加密的消息正文

解密 = RSA 私钥解 random_key -> AES-256-CBC 解 chat_msg。
注: 拉取消息(GetChatData)依赖企微 C++ SDK,本模块只做解密。
"""
import base64
import json
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.config import settings


@lru_cache(maxsize=1)
def _load_private_key():
    pem = Path(settings.wxwork_private_key_path).read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def decrypt_chat_message(encrypt_random_key: str, encrypt_chat_msg: str) -> dict:
    key = _load_private_key()

    rsa_cipher = base64.b64decode(encrypt_random_key)
    aes_key = key.decrypt(rsa_cipher, asymmetric_padding.PKCS1v15())

    # 企微规定: AES key 前 16 字节作 IV
    aes_cipher_bytes = base64.b64decode(encrypt_chat_msg)
    iv = aes_key[:16]
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded_plaintext = decryptor.update(aes_cipher_bytes) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()

    return json.loads(plaintext.decode("utf-8"))
