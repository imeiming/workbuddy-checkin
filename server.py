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
from urllib.request import build_opener, ProxyHandler

from service import CheckinService
from state import AppState, now_str, today_str
from wb_client import WBError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

state = AppState()
service = CheckinService(state)

START_TIME = time.time()

# ---------- 容器保活（自心跳）----------
#
# 云端容器在无访问时会休眠，休眠期间进程不存在，内部调度线程无法执行定时任务。
# 解决思路：进程自己定时访问一次自己的公网地址，制造持续的活动流量，
# 让平台判定应用「在线」而不再回收。整个过程发生在云端，不依赖任何本地设备开机。
#
# 公网地址优先取环境变量 WB_CHECKIN_PUBLIC_URL；未设置时从首个请求的 Host 头推断。
# 云端托管分配的固定公网地址。仅当环境变量未配置、且请求头也无法推断出公网域名时兜底，
# 避免容器内部 Host（127.0.0.1 / 内网地址）导致自心跳打到错误目标。
FALLBACK_PUBLIC_URL = "https://wb-checkin-77303.app.workbuddy.host"

SELF_PING_ENABLED = os.environ.get("WB_CHECKIN_SELFPING", "1").strip() != "0"
# 实测平台的空闲回收阈值远小于 3 分钟（180s 间隔时容器在首次心跳前就被回收），
# 因此默认压到 30 秒：宁可多几次轻量请求，也要保住进程不被回收。
SELF_PING_INTERVAL_S = max(15, int(os.environ.get("WB_CHECKIN_SELFPING_INTERVAL", "30") or 30))
SELF_PING_PATH = "/api/health"

_self_ping_lock = threading.Lock()
_self_ping_state: dict[str, Any] = {
    "url": os.environ.get("WB_CHECKIN_PUBLIC_URL", "").strip().rstrip("/"),
    "runs": 0,
    "fails": 0,
    "lastOkAt": 0.0,
    "lastError": "",
}

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


def _remember_public_url(headers: Any) -> None:
    """从请求头推断自身公网地址（仅在未显式配置时执行一次）。"""
    with _self_ping_lock:
        if _self_ping_state["url"]:
            return
        host = (headers.get("X-Forwarded-Host") or headers.get("Host") or "").strip()
        if not host or not _is_public_entry_host(host):
            # 容器内部地址 / 沙箱内网域名都不能作为自心跳目标，交给兜底常量
            return
        proto = (headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip()
        _self_ping_state["url"] = f"{proto}://{host}"


def _is_public_entry_host(host: str) -> bool:
    """是否是可从公网访问的入口域名。

    实测云端容器的 Host 头是沙箱内网域名（*.sandbox.cloudstudio.club），
    从容器自身访问它并不能证明公网入口可用，因此只认 workbuddy.host 入口域名。
    """
    name = host.split(":")[0].lower()
    if not name or _is_private_host(host):
        return False
    return name.endswith(".workbuddy.host") or name.endswith(".workbuddy.cn")


def _is_private_host(host: str) -> bool:
    """判断 Host 是否为本地/内网地址（无法从公网访问）。"""
    name = host.split(":")[0].lower()
    if name in ("localhost", "::1", "0.0.0.0"):
        return True
    parts = name.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        if parts[0] == "127" or parts[0] == "10":
            return True
        if parts[0] == "192" and parts[1] == "168":
            return True
        if parts[0] == "172" and 16 <= int(parts[1]) <= 31:
            return True
    return False


def _self_ping_loop() -> None:
    """定时访问自身公网地址，避免容器因空闲被平台回收。

    启动后先短等（服务监听就绪）立即打一次，既能快速暴露「容器是否访问得到
    自己的公网入口」，也能让刚被唤醒的容器立刻产生活动记录。
    """
    time.sleep(5)
    while True:
        _self_ping_once()
        time.sleep(SELF_PING_INTERVAL_S)


def _self_ping_once() -> None:
    """执行一次自心跳并更新统计。"""
    if not SELF_PING_ENABLED:
        return
    with _self_ping_lock:
        url = str(_self_ping_state["url"]) or FALLBACK_PUBLIC_URL
    if not url:
        return
    try:
        # 同样绕过环境代理，直接走公网回环访问自己
        opener = build_opener(ProxyHandler({}))
        with opener.open(url + SELF_PING_PATH, timeout=15) as resp:
            resp.read()
        with _self_ping_lock:
            _self_ping_state["runs"] += 1
            _self_ping_state["lastOkAt"] = time.time()
            _self_ping_state["lastError"] = ""
    except Exception as exc:  # noqa: BLE001
        with _self_ping_lock:
            _self_ping_state["runs"] += 1
            _self_ping_state["fails"] += 1
            _self_ping_state["lastError"] = str(exc)


def _self_ping_snapshot() -> dict[str, Any]:
    with _self_ping_lock:
        state = dict(_self_ping_state)
    last_ok = state["lastOkAt"]
    return {
        "enabled": SELF_PING_ENABLED,
        "intervalSeconds": SELF_PING_INTERVAL_S,
        "url": state["url"],
        "runs": state["runs"],
        "fails": state["fails"],
        "lastOkSecondsAgo": int(time.time() - last_ok) if last_ok else None,
        "lastError": state["lastError"],
    }


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
        _remember_public_url(self.headers)
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
        _remember_public_url(self.headers)
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            return self._handle_api_post(path, self._read_json())
        return self.send_error(404)

    # ---------- API: GET ----------

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            return self._send_json({"ok": True, "time": now_str()})

        if path == "/api/diag":
            return self._send_json(self._diag())

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
            # 前端要把结果写入云数据库，需要服务端按东八区补齐日期，
            # 避免用浏览器本地时区算错自然日。
            record = result.get("result")
            if isinstance(record, dict):
                result["result"] = {**record, "date": today_str(), "time": now_str()}
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

    def _diag(self) -> dict[str, Any]:
        """登录态诊断（不含任何令牌/邮箱等敏感内容）。

        用途：定位「为什么又要重新登录」——区分凭据文件丢失、密钥变化导致解不开、
        令牌到期、以及进程重启（容器被回收会让 uptime 归零）。
        """
        token = state.load_token()
        return {
            "ok": True,
            "serverTime": now_str(),
            "uptimeSeconds": int(time.time() - START_TIME),
            "selfPing": _self_ping_snapshot(),
            "scheduler": service.scheduler_snapshot(),
            "credential": state.credential_status(),
            "token": {
                "present": token is not None,
                "hasRefreshToken": bool(token.refresh_token) if token else False,
                "accessExpiresAt": token.expires_at if token else 0,
                "refreshExpiresAt": token.refresh_expires_at if token else 0,
            },
        }

    def _overview(self) -> dict[str, Any]:
        account = state.account()
        settings = state.settings()
        payload: dict[str, Any] = {
            "ok": True,
            "serverTime": now_str(),
            "loggedIn": state.has_token(),
            "credential": state.credential_status(),
            "account": account,
            "settings": settings,
            "checkedInToday": state.checked_in_today(),
            "lastResult": state.last_result(),
            "stats": state.stats(),
            "notifications": list(state.notifications)[:10],
        }
        # 容器休眠/冷启动后进程才刚起来，内部调度线程此前并不存在；
        # 这里在每次被访问时补一次判定：已过设定时刻且今日未签到就立即签到，
        # 让「打开页面即已签到」，而不是等下一个巡检窗口。
        payload["autoTriggered"] = False
        if payload["loggedIn"] and not payload["checkedInToday"]:
            payload["autoTriggered"] = service.run_due_checkin(reason="request")
            if payload["autoTriggered"]:
                # 补签刚落地，立即反映到本次响应，避免前端再等一轮刷新
                payload["checkedInToday"] = state.checked_in_today()
                payload["lastResult"] = state.last_result()
                payload["stats"] = state.stats()
                payload["notifications"] = list(state.notifications)[:10]

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
    if SELF_PING_ENABLED:
        threading.Thread(target=_self_ping_loop, daemon=True, name="self-ping").start()
        print(f"[wb-checkin] self-ping enabled, interval {SELF_PING_INTERVAL_S}s", flush=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[wb-checkin] listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        service.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
