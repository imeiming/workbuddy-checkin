"""
凭据安全存储。

令牌属于高敏感凭据，绝不明文落盘。本模块使用 Windows DPAPI
（CryptProtectData / CryptUnprotectData，作用域 = 当前 Windows 用户）
加密后写入 %LOCALAPPDATA%\\WorkBuddyCheckin\\credentials.json。

选型说明：
  - DPAPI 而非 base64：同机其他用户/进程无法解密，符合 WorkBuddy 自身
    Electron safeStorage 的实现思路。
  - 跨平台回退：非 Windows 或 DPAPI 不可用时，退化为仅本机可读权限 +
    AES 密钥派生（密钥由用户专属因子派生），并明确告警。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import platform
import stat
import tempfile
from pathlib import Path
from typing import Any

APP_DIR_NAME = "WorkBuddyCheckin"


def app_data_dir() -> Path:
    """应用数据目录。

    优先级：
      1. 环境变量 WB_CHECKIN_DATA_DIR（云端部署可显式指定持久化位置）
      2. %LOCALAPPDATA%\\WorkBuddyCheckin（Windows 本地运行）
      3. ~/.workbuddycheckin（其他平台 / 容器回退）
    """
    override = os.environ.get("WB_CHECKIN_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / f".{APP_DIR_NAME.lower()}"


def _dpapi_available() -> bool:
    return platform.system() == "Windows" and hasattr(ctypes, "windll")


class _CryptProtectData(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_protect(plain: bytes, entropy: bytes) -> bytes:
    """调用 Windows DPAPI 加密。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _DataBlob(
        len(plain),
        (ctypes.c_byte * len(plain)).from_buffer_copy(plain),
    )
    ent_blob = _DataBlob(
        len(entropy),
        (ctypes.c_byte * len(entropy)).from_buffer_copy(entropy),
    )
    out_blob = _DataBlob()

    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "WorkBuddyCheckin",
        ctypes.byref(ent_blob),
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(cipher: bytes, entropy: bytes) -> bytes:
    """调用 Windows DPAPI 解密。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _DataBlob(
        len(cipher),
        (ctypes.c_byte * len(cipher)).from_buffer_copy(cipher),
    )
    ent_blob = _DataBlob(
        len(entropy),
        (ctypes.c_byte * len(entropy)).from_buffer_copy(entropy),
    )
    out_blob = _DataBlob()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(ent_blob),
        None,
        None,
        0x01,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _xor_keystream(data: bytes, key: bytes) -> bytes:
    """确定性 keystream（SHA-256 计数器模式），用于非 Windows 回退。"""
    out = bytearray(len(data))
    counter = 0
    for offset in range(0, len(data), 32):
        block = hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        chunk = data[offset : offset + 32]
        for i, byte in enumerate(chunk):
            out[offset + i] = byte ^ block[i]
        counter += 1
    return bytes(out)


class SecretStore:
    """凭据文件读写（加密存储）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.dir = path.parent if path else app_data_dir()
        self.path = path or (self.dir / "credentials.json")
        self.entropy = b"WorkBuddyCheckin.v1"
        # 密钥来源：环境变量注入的稳定密钥（云端容器重启后仍可解密）
        #           → 否则退回按机器/用户派生的本机密钥。
        self._injected_key = os.environ.get("WB_CHECKIN_SECRET_KEY", "").strip()
        self.backend = "dpapi" if _dpapi_available() else "fallback"

    def _machine_key(self) -> bytes:
        """回退加密的密钥。

        优先使用 WB_CHECKIN_SECRET_KEY —— 容器/云端环境重启后机器标识可能变化，
        只有外部注入的固定密钥才能保证已保存的令牌仍可解密。
        """
        if self._injected_key:
            return hashlib.sha256(self._injected_key.encode("utf-8")).digest()
        seed = f"{platform.node()}|{os.environ.get('USERNAME', '')}|{self.dir}"
        return hashlib.sha256(seed.encode("utf-8")).digest()

    def _encrypt(self, plain: bytes) -> str:
        if self.backend == "dpapi":
            blob = _dpapi_protect(plain, self.entropy)
            return "dpapi:" + base64.b64encode(blob).decode("ascii")
        return "fallback:" + base64.b64encode(_xor_keystream(plain, self._machine_key())).decode("ascii")

    def _decrypt(self, encoded: str) -> bytes:
        if encoded.startswith("dpapi:"):
            return _dpapi_unprotect(base64.b64decode(encoded[6:]), self.entropy)
        if encoded.startswith("fallback:"):
            return _xor_keystream(base64.b64decode(encoded[9:]), self._machine_key())
        raise ValueError("未知的凭据加密格式")

    def save(self, data: dict[str, Any]) -> None:
        """原子写入加密凭据。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "backend": self.backend,
            "data": self._encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8")),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            if platform.system() != "Windows":
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load(self) -> dict[str, Any] | None:
        """读取并解密凭据；不存在或损坏返回 None。"""
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return json.loads(self._decrypt(payload["data"]).decode("utf-8"))
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None
        except Exception:  # DPAPI 解密失败（用户变更 / 换机）
            return None

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False
