"""
WorkBuddy 签到助手的底层 HTTP 客户端。

协议要点（逆向自 WorkBuddy Desktop app.asar，与官方客户端完全一致）：
  - 网关基址      : https://www.workbuddy.cn
  - 登录态探测    : POST /v2/plugin/auth/state?platform=workbuddy
                    -> { state, authUrl }，authUrl 即微信扫码页
  - 轮询取令牌    : GET  /v2/plugin/auth/token?state=<state>
                    -> 未完成时返回 code=11217（pending），完成后返回 { accessToken, refreshToken, ... }
  - 令牌刷新      : POST /v2/plugin/auth/token/refresh  (X-Refresh-Token 头)
  - 账号信息      : GET  /v2/plugin/account              (Authorization: Bearer)
  - 签到状态      : POST /v2/billing/meter/checkin-activity-status
  - 领取签到积分  : POST /v2/billing/meter/daily-checkin

鉴权头（官方 buildHeaders）：
  Accept: application/json
  Content-Type: application/json
  Authorization: Bearer <accessToken>
  X-User-Id: <uid>
  X-Enterprise-Id / X-Tenant-Id: <enterpriseId>   # 仅企业账号
  X-Domain: <auth.domain>                          # 可选

仅使用 Python 标准库实现，无第三方依赖。
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ENDPOINT = "https://www.workbuddy.cn"
PLATFORM = "workbuddy"

# 官方常量（app.asar）
LOGIN_PENDING_CODE = 11217
SIGN_IN_PENDING_TIMEOUT_MS = 300_000
SIGN_IN_FETCH_INTERVAL_MS = 1_000

# 签到业务码 -> 语义（官方 mapCheckinStatus）
CLAIM_CODE_MEANING = {
    0: "claimed",
    1001: "already_claimed",
    1002: "not_eligible",
    1003: "event_ended",
}

CODE_IP_LIMIT = 10081


class WBError(Exception):
    """WorkBuddy 接口调用异常。"""

    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


@dataclass
class AuthToken:
    """与官方 session.auth 同构的令牌对象。"""

    access_token: str
    refresh_token: str = ""
    domain: str = ""
    token_type: str = "Bearer"
    expires_at: int = 0
    refresh_expires_at: int = 0
    last_refresh_time: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def access_expired(self) -> bool:
        return bool(self.expires_at) and Date.now() >= self.expires_at

    @property
    def refresh_expired(self) -> bool:
        return bool(self.refresh_expires_at) and Date.now() >= self.refresh_expires_at

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AuthToken":
        now = int(time.time() * 1000)
        expires_in = _as_int(payload.get("expiresIn"))
        refresh_in = _as_int(payload.get("refreshExpiresIn"))
        return cls(
            access_token=str(payload.get("accessToken") or ""),
            refresh_token=str(payload.get("refreshToken") or ""),
            domain=str(payload.get("domain") or ""),
            token_type=str(payload.get("tokenType") or "Bearer"),
            expires_at=now + expires_in * 1000 if expires_in else _as_int(payload.get("expiresAt")),
            refresh_expires_at=now + refresh_in * 1000 if refresh_in else _as_int(payload.get("refreshExpiresAt")),
            last_refresh_time=now,
            raw=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "domain": self.domain,
            "tokenType": self.token_type,
            "expiresAt": self.expires_at,
            "refreshExpiresAt": self.refresh_expires_at,
            "lastRefreshTime": self.last_refresh_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthToken":
        return cls(
            access_token=str(data.get("accessToken") or ""),
            refresh_token=str(data.get("refreshToken") or ""),
            domain=str(data.get("domain") or ""),
            token_type=str(data.get("tokenType") or "Bearer"),
            expires_at=_as_int(data.get("expiresAt")),
            refresh_expires_at=_as_int(data.get("refreshExpiresAt")),
            last_refresh_time=_as_int(data.get("lastRefreshTime")),
        )


class Date:
    """毫秒时间戳工具（官方用 Date.now()）。"""

    @staticmethod
    def now() -> int:
        return int(time.time() * 1000)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class WBHttpClient:
    """WorkBuddy 网关 HTTP 客户端。"""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        timeout: float = 20.0,
        verbose: bool = False,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self._ssl_ctx = ssl.create_default_context()
        # 关键：显式绕过环境代理（HTTP_PROXY/HTTPS_PROXY）。
        # 某些受管运行环境会把这些变量指向只放行内部流量的本地代理，
        #  urllib 默认读取它们会导致对 WorkBuddy 网关的请求被拒
        # （表现为 "upstream connect failed"）。签到属于账号直连业务流量，
        #  必须直连网关，因此这里固定使用空 ProxyHandler。
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self._ssl_ctx),
        )

    # ---------- 底层 ----------

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.endpoint}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """发起请求并返回解析后的 JSON。HTTP 4xx/5xx 时抛 WBError。"""
        url = self._url(path, query)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            if value:
                req.add_header(key, value)

        if self.verbose:
            # 调试输出脱敏：不打印 query（其中可能含临时登录态 state / 令牌参数）
            print(f"[wb] {method} {self.endpoint}{path}", flush=True)

        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return _parse_json(raw, resp.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            parsed = _parse_json(raw, exc.code, allow_empty=True)
            code = _as_int(parsed.get("code")) if parsed else 0
            msg = (parsed or {}).get("msg") or f"HTTP {exc.code}"
            raise WBError(msg, code=code or None, http_status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise WBError(f"网络请求失败: {exc.reason}") from exc

    def _poll(self, method: str, path: str, *, query=None, headers=None) -> dict[str, Any]:
        """轮询接口：code=11217 视为 pending 而非错误。"""
        return self._request(method, path, query=query, body={} if method == "POST" else None, headers=headers)

    # ---------- 鉴权 ----------

    def fetch_auth_state(self) -> dict[str, str]:
        """申请登录态，返回 {'state': ..., 'authUrl': ...}。"""
        no_auth = {
            "X-No-Authorization": "true",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }
        envelope = self._request(
            "POST",
            "/v2/plugin/auth/state",
            query={"platform": PLATFORM},
            body={},
            headers=no_auth,
        )
        data = envelope.get("data") or {}
        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or "")
        if not state:
            raise WBError("获取登录态失败：响应缺少 state", code=_as_int(envelope.get("code")))
        return {"state": state, "authUrl": auth_url}

    def poll_auth_token(
        self,
        state: str,
        *,
        timeout_ms: int = SIGN_IN_PENDING_TIMEOUT_MS,
        interval_ms: int = SIGN_IN_FETCH_INTERVAL_MS,
        should_abort=None,
        on_tick=None,
    ) -> AuthToken:
        """轮询直到拿到 accessToken。11217 视为等待中。"""
        no_auth = {
            "X-No-Authorization": "true",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }
        deadline = Date.now() + timeout_ms
        while Date.now() < deadline:
            if should_abort and should_abort():
                raise WBError("登录已取消")
            time.sleep(interval_ms / 1000)
            try:
                envelope = self._request("GET", "/v2/plugin/auth/token", query={"state": state}, headers=no_auth)
            except WBError as exc:
                if exc.code == LOGIN_PENDING_CODE:
                    continue
                if exc.http_status == 401:
                    continue
                raise
            if on_tick:
                on_tick()
            data = envelope.get("data") or {}
            if isinstance(data, dict) and data.get("accessToken"):
                return AuthToken.from_payload(data)
            code = _as_int(envelope.get("code"))
            if code == LOGIN_PENDING_CODE:
                continue
            if code == CODE_IP_LIMIT:
                raise WBError("登录失败：触发 IP 频率限制（10081），请稍后重试", code=code)
            raise WBError(envelope.get("msg") or "获取登录令牌失败", code=code or None)
        raise WBError("登录超时：请在有效期内完成微信扫码")

    def refresh_access_token(self, refresh_token: str) -> AuthToken:
        """用 refreshToken 换新 accessToken。"""
        if not refresh_token:
            raise WBError("缺少 refreshToken，无法刷新")
        envelope = self._request(
            "POST",
            "/v2/plugin/auth/token/refresh",
            body={},
            headers={
                "X-Refresh-Token": refresh_token,
                "X-Auth-Refresh-Source": "plugin",
            },
        )
        data = envelope.get("data") or {}
        if not isinstance(data, dict) or not data.get("accessToken"):
            raise WBError(envelope.get("msg") or "刷新令牌失败", code=_as_int(envelope.get("code")) or None)
        token = AuthToken.from_payload(data)
        if not token.refresh_token:
            token.refresh_token = refresh_token
        return token

    def fetch_account(self, token: AuthToken) -> dict[str, Any]:
        """获取当前账号信息（含 uid）。"""
        envelope = self._request(
            "GET",
            "/v2/plugin/account",
            headers=self.auth_headers(token),
        )
        data = envelope.get("data") or {}
        if not data.get("uid"):
            raise WBError("获取账号信息失败：响应缺少 uid", code=_as_int(envelope.get("code")) or None)
        return data

    def auth_headers(self, token: AuthToken, uid: str = "", enterprise_id: str = "") -> dict[str, str]:
        """官方 buildHeaders 的等价实现。"""
        headers = {"Authorization": f"Bearer {token.access_token}"}
        if uid:
            headers["X-User-Id"] = uid
        if enterprise_id:
            headers["X-Enterprise-Id"] = enterprise_id
            headers["X-Tenant-Id"] = enterprise_id
        if token.domain:
            headers["X-Domain"] = token.domain
        return headers

    # ---------- 签到 ----------

    def checkin_status(self, token: AuthToken, uid: str, enterprise_id: str = "") -> dict[str, Any]:
        """查询签到状态（官方 checkinStatus 字段映射）。"""
        envelope = self._request(
            "POST",
            "/v2/billing/meter/checkin-activity-status",
            body={},
            headers=self.auth_headers(token, uid, enterprise_id),
        )
        if _as_int(envelope.get("code")) != 0 or not envelope.get("data"):
            return {}
        d = envelope["data"]
        return {
            "active": bool(d.get("active", False)),
            "todayCheckedIn": bool(d.get("today_checked_in", False)),
            "streakDays": _as_int(d.get("streak_days")),
            "dailyCredit": _as_int(d.get("daily_credit")),
            "todayCredit": _as_int(d.get("today_credit")),
            "isStreakDay": bool(d.get("is_streak_day", False)),
            "nextStreakDay": _as_int(d.get("next_streak_day")),
            "streakBonusDays": d.get("streak_bonus_days"),
            "streakBonusCredit": d.get("streak_bonus_credit"),
            "checkinDates": d.get("checkin_dates") or [],
            "totalCredits": d.get("total_credits"),
            "startTime": d.get("start_time"),
            "endTime": d.get("end_time"),
            "activityName": d.get("activity_name"),
        }

    def daily_checkin(self, token: AuthToken, uid: str, enterprise_id: str = "") -> dict[str, Any]:
        """执行签到，返回 {'claimed', 'credit', 'streakDays', 'isStreakDay', 'code', 'msg'}。"""
        envelope = self._request(
            "POST",
            "/v2/billing/meter/daily-checkin",
            body={},
            headers=self.auth_headers(token, uid, enterprise_id),
        )
        code = _as_int(envelope.get("code"))
        msg = str(envelope.get("msg") or "")
        if code != 0:
            return {
                "claimed": False,
                "status": CLAIM_CODE_MEANING.get(code, "unknown_biz_error"),
                "code": code,
                "msg": msg,
                "credit": 0,
                "streakDays": 0,
            }
        data = envelope.get("data") or {}
        return {
            "claimed": True,
            "status": "claimed",
            "code": 0,
            "msg": msg,
            "credit": _as_int(data.get("credit")),
            "streakDays": _as_int(data.get("streak_days")),
            "isStreakDay": bool(data.get("is_streak_day", False)),
        }


def _parse_json(raw: str, status: int, *, allow_empty: bool = False) -> dict[str, Any]:
    if not raw.strip():
        if allow_empty:
            return {}
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 网关 401/5xx 时返回 HTML 错误页
        snippet = raw.strip().replace("\r", " ").replace("\n", " ")[:160]
        raise WBError(f"响应非 JSON（HTTP {status}）: {snippet}", http_status=status)
    return parsed if isinstance(parsed, dict) else {"data": parsed}
