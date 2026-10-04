"""
WorkBuddy 签到助手 —— 应用服务端。

为什么必须有服务端
------------------
WorkBuddy 网关（www.workbuddy.cn）不返回 Access-Control-Allow-Origin，
浏览器前端无法直连（已实测：OPTIONS 预检 404、POST 响应无 CORS 头）。
因此所有网关调用都在本服务端完成，前端只与同源的 /api/* 通信。

运行：监听 $PORT，绑定 0.0.0.0（WorkBuddy 应用托管要求）。
仅使用 Python 标准库。
"""

from __future__ import annotations

import collections
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from service import CheckinService
from state import AppState, now_str
from wb_client import WBError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

state = AppState()
service = CheckinService(state)

# 登录轮询在后台线程执行，前端轮询 /api/login/status 拿结果。
# 只保留最近若干条，避免长时间运行后无限增长。
_login_threads: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_login_lock = threading.Lock()
_MAX_LOGIN_SESSIONS = 20


def _remember_login(login_state: str, info: dict[str, Any]) -> None:
    with _login_lock:
        _login_threads[login_state] = info
        _login_threads.move_to_end(login_state)
        while len(_login_threads) > _MAX_LOGIN_SESSIONS:
            _login_threads.popitem(last=False)


def _start_login_polling(login_state: str) -> None:
    _remember_login(login_state, {"status": "pending", "message": "等待微信扫码"})

    def worker() -> None:
        try:
            info = service.poll_login(login_state)
            _remember_login(login_state, {"status": "success", **info})
        except WBError as exc:
            _remember_login(login_state, {"status": "failed", "message": exc.message})
        except Exception as exc:  # noqa: BLE001
            _remember_login(login_state, {"status": "failed", "message": str(exc)})

    threading.Thread(target=worker, daemon=True, name=f"login-{login_state[:8]}").start()


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkBuddyCheckin/1.0"

    # 静默默认的逐请求日志
    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("WB_CHECKIN_VERBOSE"):
            super().log_message(fmt, *args)

    # ---------- 响应工具 ----------

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "message": message}, status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _serve_static(self, rel_path: str) -> None:
        target = (STATIC_DIR / rel_path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/") :])
        if path.startswith("/api/"):
            return self._handle_api_get(path, query)
        return self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            return self._handle_api_post(path, self._read_json())
        return self.send_error(404)

    # ---------- API: GET ----------

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            return self._send_json({"ok": True, "time": now_str()})

        if path == "/api/overview":
            return self._send_json(self._overview())

        if path == "/api/history":
            limit = int((query.get("limit") or ["30"])[0])
            return self._send_json({"ok": True, "history": state.history(limit)})

        if path == "/api/notifications":
            return self._send_json({"ok": True, "notifications": list(state.notifications)})

        if path == "/api/login/status":
            login_state = (query.get("state") or [""])[0]
            with _login_lock:
                info = _login_threads.get(login_state)
            if not info:
                return self._send_error_json("登录会话不存在或已过期", 404)
            return self._send_json({"ok": True, **info})

        if path == "/api/status":
            try:
                return self._send_json(service.query_status())
            except WBError as exc:
                return self._send_error_json(exc.message, 401 if not state.has_token() else 400)

        return self.send_error(404)

    # ---------- API: POST ----------

    def _handle_api_post(self, path: str, body: dict[str, Any]) -> None:
        if path == "/api/login/start":
            try:
                info = service.begin_login()
            except WBError as exc:
                return self._send_error_json(exc.message)
            _start_login_polling(info["state"])
            return self._send_json({"ok": True, **info})

        if path == "/api/login/cancel":
            service.cancel_login()
            return self._send_json({"ok": True})

        if path == "/api/logout":
            state.clear_token()
            return self._send_json({"ok": True, "message": "已清除本地登录凭证"})

        if path == "/api/checkin":
            result = service.do_checkin(source="manual")
            if not result.get("ok"):
                return self._send_error_json(result.get("message", "签到失败"))
            return self._send_json(result)

        if path == "/api/settings":
            settings = state.update_settings(body)
            return self._send_json({"ok": True, "settings": settings})

        if path == "/api/refresh":
            try:
                token, uid, enterprise_id = service.ensure_token()
                return self._send_json(
                    {
                        "ok": True,
                        "uid": uid,
                        "hasRefreshToken": bool(token.refresh_token),
                        "accessExpiresAt": token.expires_at,
                    }
                )
            except WBError as exc:
                return self._send_error_json(exc.message, 401)

        return self.send_error(404)

    # ---------- 汇总 ----------

    def _overview(self) -> dict[str, Any]:
        account = state.account()
        settings = state.settings()
        payload: dict[str, Any] = {
            "ok": True,
            "serverTime": now_str(),
            "loggedIn": state.has_token(),
            "account": account,
            "settings": settings,
            "checkedInToday": state.checked_in_today(),
            "lastResult": state.last_result(),
            "stats": state.stats(),
            "notifications": list(state.notifications)[:10],
        }
        # 已登录时顺带拉一次实时状态，失败不阻塞页面
        if payload["loggedIn"]:
            try:
                token, uid, enterprise_id = service.ensure_token()
                payload["status"] = service.client.checkin_status(token, uid, enterprise_id)
            except WBError as exc:
                payload["statusError"] = exc.message
            except Exception as exc:  # noqa: BLE001
                payload["statusError"] = str(exc)
        return payload


def main() -> None:
    port = int(os.environ.get("PORT", "3000"))
    host = os.environ.get("HOST", "0.0.0.0")
    service.start(on_error=lambda msg: state.push_notification("error", "定时签到异常", msg))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[wb-checkin] listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        service.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
