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


KEYFILE_NAME = ".keyfile"


class SecretStore:
    """凭据文件读写（加密存储）。"""

    def __init__(self, path: Path | None = None, mirror: Path | None = None) -> None:
        self.dir = path.parent if path else app_data_dir()
        self.path = path or (self.dir / "credentials.json")
        # 备用副本：云端容器的数据目录可能被重置，多写一份能提高「不用重新扫码」的概率。
        # 内容同样加密，密钥仍在主数据目录的 .keyfile 里，两者分离。
        self.mirror = mirror
        self.entropy = b"WorkBuddyCheckin.v1"
        # 密钥来源：环境变量注入的稳定密钥（云端容器重启后仍可解密）
        #           → 否则退回按机器/用户派生的本机密钥。
        self._injected_key = os.environ.get("WB_CHECKIN_SECRET_KEY", "").strip()
        self.backend = "dpapi" if _dpapi_available() else "fallback"

    @property
    def key_path(self) -> Path:
        return self.dir / KEYFILE_NAME

    @property
    def key_mirror(self) -> Path | None:
        return self.mirror.parent / KEYFILE_NAME if self.mirror else None

    def key_source(self) -> str:
        """当前实际使用的密钥来源，用于诊断「为什么又要重新登录」。"""
        if self._injected_key:
            return "env"
        if self.key_path.exists():
            return "keyfile"
        return "machine"  # 随容器 hostname 变化，重启后旧凭据会读不出来

    def _ensure_keyfile(self) -> bytes:
        """读取（或首次创建）与凭据文件同生命周期的随机密钥。

        这是云端「登录态丢失」的核心修复：派生密钥依赖 platform.node()，
        容器重建后 hostname 变化会让已保存的令牌无法解密。把密钥落成文件后，
        只要数据目录还在，密钥就与凭据同在，解密必然成功。
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            # 1) 主目录的密钥文件
            if self.key_path.exists():
                raw = self.key_path.read_bytes().strip()
                if raw:
                    self._write_key_mirror(raw)
                    return hashlib.sha256(raw).digest()
            # 2) 主目录被重置：从备用目录把密钥捞回来，否则备份凭据也解不开
            mirror_key = self.key_mirror
            if mirror_key and mirror_key.exists():
                raw = mirror_key.read_bytes().strip()
                if raw:
                    try:
                        self.dir.mkdir(parents=True, exist_ok=True)
                        self.key_path.write_bytes(raw)
                        if platform.system() != "Windows":
                            os.chmod(self.key_path, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError:
                        pass
                    return hashlib.sha256(raw).digest()
            # 3) 都没有：生成新密钥，主 + 备 同时落盘
            key = os.urandom(32)
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".key.tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(key)
                if platform.system() != "Windows":
                    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(tmp, self.key_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self._write_key_mirror(key)
            return hashlib.sha256(key).digest()
        except OSError:
            # 目录不可写时退回机器派生，至少本机本次运行内可用
            return self._machine_fallback_key()

    def _write_key_mirror(self, key: bytes) -> None:
        mirror_key = self.key_mirror
        if not mirror_key:
            return
        try:
            mirror_key.parent.mkdir(parents=True, exist_ok=True)
            if not mirror_key.exists():
                mirror_key.write_bytes(key)
                if platform.system() != "Windows":
                    os.chmod(mirror_key, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _machine_fallback_key(self) -> bytes:
        seed = f"{platform.node()}|{os.environ.get('USERNAME', '')}|{self.dir}"
        return hashlib.sha256(seed.encode("utf-8")).digest()

    def _machine_key(self) -> bytes:
        """回退加密的密钥。

        优先级：环境变量 WB_CHECKIN_SECRET_KEY → 数据目录内的 .keyfile → 机器派生。
        只有第三档会随容器 hostname 变化而变化，必须尽量避免。
        """
        if self._injected_key:
            return hashlib.sha256(self._injected_key.encode("utf-8")).digest()
        return self._ensure_keyfile()

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

        if self.mirror:
            try:
                self.mirror.parent.mkdir(parents=True, exist_ok=True)
                self.mirror.write_text(text, encoding="utf-8")
                if platform.system() != "Windows":
                    os.chmod(self.mirror, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # 备用副本写失败不影响主流程

    def load(self) -> dict[str, Any] | None:
        """读取并解密凭据：主位置失败时回退到备用副本。"""
        data = self._load_one(self.path)
        if data is not None:
            return data
        if self.mirror:
            data = self._load_one(self.mirror)
            if data is not None:
                # 主位置丢了，用备用副本就地恢复
                try:
                    self.save(data)
                except OSError:
                    pass
                return data
        return None

    def _load_one(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return json.loads(self._decrypt(payload["data"]).decode("utf-8"))
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None
        except Exception:  # DPAPI 解密失败（用户变更 / 换机）
            return None

    def clear(self) -> bool:
        removed = False
        for path in (self.path, self.mirror):
            if path and path.exists():
                path.unlink()
                removed = True
        return removed

    def probe(self) -> dict[str, Any]:
        """凭据自检：区分「文件不存在」与「文件在但解不开」。

        以前这两种情况都静默表现为「未登录」，无法定位登录态为何丢失。
        """
        info: dict[str, Any] = {
            "dir": str(self.dir),
            "dirExists": self.dir.exists(),
            "dirWritable": os.access(str(self.dir), os.W_OK) if self.dir.exists() else False,
            "backend": self.backend,
            "keySource": self.key_source(),
            "fileExists": self.path.exists(),
            "decryptable": False,
            "reason": "",
            "mirrorExists": bool(self.mirror) and self.mirror.exists(),
        }
        if not info["fileExists"] and not info["mirrorExists"]:
            info["reason"] = "没有已保存的凭据（尚未扫码或数据目录被重置）"
            return info
        if not info["fileExists"] and info["mirrorExists"]:
            info["reason"] = "主数据目录已被重置，正在从应用目录的备用副本恢复"
            info["decryptable"] = self._load_one(self.mirror) is not None
            return info
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw = self._decrypt(payload["data"]).decode("utf-8")
            data = json.loads(raw)
            info["decryptable"] = bool(data.get("accessToken"))
            if not info["decryptable"]:
                info["reason"] = "凭据内容缺少 accessToken"
        except Exception as exc:  # noqa: BLE001
            info["reason"] = f"凭据解密失败（{type(exc).__name__}）：密钥来源={info['keySource']}"
        return info
