"""
应用状态层：令牌保管 + 签到历史 + 通知队列。

设计要点
--------
1. 令牌永不明文落盘：交给 secret_store.SecretStore 加密后存储。
2. 状态文件与凭据文件分离：状态可安全地明文 JSON（无敏感字段），
   凭据走加密通道。
3. 进程内互斥 + 文件原子写：多请求并发下不产生半截文件。
4. 通知队列：结果既进内存环形队列（页面读取），也可外发 Webhook。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from secret_store import SecretStore, app_data_dir
from wb_client import AuthToken, Date

CST = timezone(timedelta(hours=8))

MAX_HISTORY = 120
MAX_NOTIFICATIONS = 50


def now_ms() -> int:
    return Date.now()


def today_str() -> str:
    """北京时间日期（YYYY-MM-DD）。签到按自然日判断，必须用东八区。"""
    return datetime.now(CST).strftime("%Y-%m-%d")


def now_str() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class AppState:
    """签到助手的完整运行时状态。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.dir = data_dir or app_data_dir()
        self.state_path = self.dir / "state.json"
        # 备用凭据副本放在应用目录：云端容器的数据目录被重置时仍有机会免重新扫码。
        mirror_dir = Path(__file__).resolve().parent
        mirror = None if os.environ.get("WB_CHECKIN_NO_MIRROR") else mirror_dir / ".credentials.json"
        self.secret = SecretStore(self.dir / "credentials.json", mirror=mirror)
        self._lock = threading.RLock()
        self.notifications: deque[dict[str, Any]] = deque(maxlen=MAX_NOTIFICATIONS)
        self._load()

    # ---------- 状态持久化 ----------

    def _default_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "account": {},          # { uid, nickname, enterpriseId, type }
            "history": [],          # 签到历史（倒序）
            "settings": {
                "autoCheckin": True,
                "checkinTime": "09:10",
                "webhookUrl": "",
                "webhookEnabled": False,
            },
            "lastCheckinDate": "",
            "lastCheckinResult": None,
        }

    def _load(self) -> None:
        with self._lock:
            data = self._default_state()
            if self.state_path.exists():
                try:
                    stored = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if isinstance(stored, dict):
                        data.update(stored)
                        settings = self._default_state()["settings"]
                        settings.update(stored.get("settings") or {})
                        data["settings"] = settings
                except (json.JSONDecodeError, OSError):
                    pass
            self.data = data

    def _save(self) -> None:
        _atomic_write(self.state_path, json.dumps(self.data, ensure_ascii=False, indent=2))

    def flush(self) -> None:
        with self._lock:
            self._save()

    # ---------- 令牌 ----------

    def save_token(self, token: AuthToken) -> None:
        with self._lock:
            self.secret.save(token.to_dict())

    def load_token(self) -> AuthToken | None:
        with self._lock:
            raw = self.secret.load()
        if not raw or not raw.get("accessToken"):
            return None
        return AuthToken.from_dict(raw)

    def clear_token(self) -> None:
        with self._lock:
            self.secret.clear()
            self.data["account"] = {}

    def has_token(self) -> bool:
        return self.load_token() is not None

    def credential_status(self) -> dict[str, Any]:
        """凭据自检结果（供 /api/diag 与前端提示使用）。"""
        return self.secret.probe()

    def verify_credentials(self) -> bool:
        """保存凭据后立即读回校验。

        云端容器若把密钥/文件写在不稳定的位置，这里会立刻暴露
        「刚登录完就读不出来」，而不是等到下次请求才发现掉登录。
        """
        return self.secret.load() is not None

    # ---------- 账号 ----------

    def set_account(self, account: dict[str, Any]) -> None:
        with self._lock:
            self.data["account"] = {
                "uid": account.get("uid", ""),
                "nickname": account.get("nickname") or account.get("displayName") or "",
                "enterpriseId": account.get("enterpriseId", "") or "",
                "type": account.get("type", "personal"),
            }
            self._save()

    def account(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data.get("account") or {})

    def account_id(self) -> tuple[str, str]:
        """返回 (uid, enterpriseId)。"""
        acct = self.account()
        return acct.get("uid", ""), acct.get("enterpriseId", "")

    # ---------- 设置 ----------

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data["settings"])

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            allowed = {"autoCheckin", "checkinTime", "webhookUrl", "webhookEnabled"}
            for key, value in patch.items():
                if key in allowed:
                    self.data["settings"][key] = value
            self._save()
            return dict(self.data["settings"])

    # ---------- 签到历史 ----------

    def record_checkin(self, result: dict[str, Any]) -> None:
        with self._lock:
            entry = {"date": today_str(), "time": now_str(), **result}
            self.data["lastCheckinDate"] = entry["date"]
            self.data["lastCheckinResult"] = entry
            history = [h for h in self.data.get("history", []) if h.get("date") != entry["date"]]
            history.insert(0, entry)
            self.data["history"] = history[:MAX_HISTORY]
            self._save()

    def history(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.data.get("history", []))[:limit]

    def checked_in_today(self) -> bool:
        with self._lock:
            return self.data.get("lastCheckinDate") == today_str()

    def last_result(self) -> dict[str, Any] | None:
        with self._lock:
            return self.data.get("lastCheckinResult")

    def stats(self) -> dict[str, Any]:
        with self._lock:
            history = self.data.get("history", [])
            successes = [h for h in history if h.get("claimed")]
            credits = sum(int(h.get("credit") or 0) for h in successes)
            return {
                "totalDays": len(history),
                "successDays": len(successes),
                "totalCredits": credits,
                "currentStreak": (history[0].get("streakDays", 0) if history else 0),
            }

    # ---------- 通知 ----------

    def push_notification(self, level: str, title: str, detail: str = "") -> dict[str, Any]:
        item = {"level": level, "title": title, "detail": detail, "time": now_str()}
        with self._lock:
            self.notifications.appendleft(item)
        return item
