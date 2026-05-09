"""企微会话存档 C++ SDK (libWeWorkFinanceSdk_C.so) 的 ctypes 包装。

只暴露我们用到的: NewSdk / Init / GetChatData / DecryptData / DestroySdk。
SDK 不接受 RSA 私钥 — 调用方先用 RSA 私钥解 encrypt_random_key 拿到 AES key 字符串,
再传给 DecryptData 配合 encrypt_chat_msg 拿明文。
"""
import ctypes
import json
import threading
from typing import List, Dict

from app.config import settings


_lib = None
_lib_lock = threading.Lock()


def _load_lib():
    global _lib
    if _lib is not None:
        return _lib
    with _lib_lock:
        if _lib is not None:
            return _lib

        lib = ctypes.CDLL(settings.sdk_lib_path)

        lib.NewSdk.restype = ctypes.c_void_p
        lib.NewSdk.argtypes = []

        lib.Init.restype = ctypes.c_int
        lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]

        lib.GetChatData.restype = ctypes.c_int
        lib.GetChatData.argtypes = [
            ctypes.c_void_p,        # sdk
            ctypes.c_ulonglong,     # seq
            ctypes.c_uint,          # limit
            ctypes.c_char_p,        # proxy
            ctypes.c_char_p,        # passwd
            ctypes.c_int,           # timeout
            ctypes.c_void_p,        # *Slice_t
        ]

        lib.DecryptData.restype = ctypes.c_int
        lib.DecryptData.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]

        lib.DestroySdk.restype = None
        lib.DestroySdk.argtypes = [ctypes.c_void_p]

        lib.NewSlice.restype = ctypes.c_void_p
        lib.NewSlice.argtypes = []

        lib.FreeSlice.restype = None
        lib.FreeSlice.argtypes = [ctypes.c_void_p]

        lib.GetContentFromSlice.restype = ctypes.c_void_p
        lib.GetContentFromSlice.argtypes = [ctypes.c_void_p]

        lib.GetSliceLen.restype = ctypes.c_int
        lib.GetSliceLen.argtypes = [ctypes.c_void_p]

        _lib = lib
        return lib


class SdkError(Exception):
    pass


class WxworkSdk:
    def __init__(self, corpid: str, secret: str):
        self._lib = _load_lib()
        self._sdk = self._lib.NewSdk()
        if not self._sdk:
            raise SdkError("NewSdk 返回 NULL")
        ret = self._lib.Init(self._sdk, corpid.encode("utf-8"), secret.encode("utf-8"))
        if ret != 0:
            self._lib.DestroySdk(self._sdk)
            self._sdk = None
            raise SdkError(f"SDK Init 失败 errcode={ret}")

    def _slice_to_bytes(self, slice_ptr) -> bytes:
        content = self._lib.GetContentFromSlice(slice_ptr)
        length = self._lib.GetSliceLen(slice_ptr)
        if not content or length == 0:
            return b""
        return ctypes.string_at(content, length)

    def fetch_chat(self, seq: int, limit: int = 1000, timeout: int = 10) -> List[Dict]:
        slice_ptr = self._lib.NewSlice()
        try:
            ret = self._lib.GetChatData(
                self._sdk, seq, limit, None, None, timeout, slice_ptr
            )
            if ret != 0:
                raise SdkError(f"GetChatData errcode={ret}")
            raw = self._slice_to_bytes(slice_ptr)
            if not raw:
                return []
            data = json.loads(raw.decode("utf-8"))
            if data.get("errcode") != 0:
                raise SdkError(
                    f"GetChatData biz_errcode={data.get('errcode')}: {data.get('errmsg')}"
                )
            return data.get("chatdata", [])
        finally:
            self._lib.FreeSlice(slice_ptr)

    def decrypt(self, aes_key_str: str, encrypt_chat_msg: str) -> dict:
        """SDK AES 解密。aes_key_str 是 RSA 私钥解 encrypt_random_key 后的明文字符串。"""
        slice_ptr = self._lib.NewSlice()
        try:
            ret = self._lib.DecryptData(
                aes_key_str.encode("utf-8"),
                encrypt_chat_msg.encode("utf-8"),
                slice_ptr,
            )
            if ret != 0:
                raise SdkError(f"DecryptData errcode={ret}")
            raw = self._slice_to_bytes(slice_ptr)
            return json.loads(raw.decode("utf-8"))
        finally:
            self._lib.FreeSlice(slice_ptr)

    def close(self):
        if self._sdk:
            self._lib.DestroySdk(self._sdk)
            self._sdk = None

    def __del__(self):
        self.close()
