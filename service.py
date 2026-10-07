"""
签到服务：把 HTTP 客户端、状态层、调度与通知编排成可用业务流。

职责边界
--------
- CheckinService.ensure_token()  取有效令牌（必要时自动刷新）
- CheckinService.do_checkin()   执行一次签到并落库 + 通知
- CheckinService.start()        启动每日定时线程
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable

import qrcode
from state import AppState, CST, now_str, today_str
from wb_client import AuthToken, Date, WBError, WBHttpClient

# 令牌提前 5 分钟刷新，避免边界失效
TOKEN_REFRESH_MARGIN_MS = 5 * 60 * 1000

# 主动续期间隔：即使当天没有签到动作，也定期走一次 refresh，
# 保持 refresh 链活跃，并在失效时第一时间通知而不是静默掉线。
TOKEN_KEEPALIVE_INTERVAL_S = 30 * 60

# 离目标时刻还远时的巡检粒度（秒）。临近目标时刻会按剩余秒数精确对齐。
SCHEDULER_IDLE_INTERVAL_S = 300

# 到点判定的容差（秒）：剩余时间小于该值即视为「已到点」，立即执行。
SCHEDULER_ALIGN_TOLERANCE_S = 5

# 触发一次后至少等待的秒数，避免同一分钟内重复打签到接口。
SCHEDULER_MIN_RETRY_GAP_S = 60


class CheckinService:
    def __init__(self, state: AppState, client: WBHttpClient | None = None) -> None:
        self.state = state
        self.client = client or WBHttpClient()
        self._login_lock = threading.Lock()
        self._login_abort = threading.Event()
        self._active_state: str | None = None
        self._active_url: str | None = None
        self._checkin_lock = threading.Lock()
        self._auto_cooldown_until = 0  # 自动签到失败后的冷却截止（ms）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 令牌 ----------

    def ensure_token(self) -> tuple[AuthToken, str, str]:
        """返回 (token, uid, enterpriseId)；必要时用 refreshToken 换新。"""
        token = self.state.load_token()
        if not token:
            raise WBError("尚未授权：请先通过微信扫码登录")

        if token.access_expired or Date.now() >= (token.expires_at - TOKEN_REFRESH_MARGIN_MS):
            if not token.refresh_token:
                raise WBError("登录已过期，请重新微信扫码授权")
            if token.refresh_expired:
                raise WBError("登录凭证已过期，请重新微信扫码授权")
            token = self.client.refresh_access_token(token.refresh_token)
            self.state.save_token(token)

        uid, enterprise_id = self.state.account_id()
        if not uid:
            account = self.client.fetch_account(token)
            self.state.set_account(account)
            uid, enterprise_id = self.state.account_id()
        return token, uid, enterprise_id

    # ---------- 登录 ----------

    def begin_login(self) -> dict[str, Any]:
        """申请登录态，返回二维码与链接。"""
        with self._login_lock:
            self._login_abort.clear()
            auth_state = self.client.fetch_auth_state()
            self._active_state = auth_state["state"]
            self._active_url = auth_state["authUrl"]
            return {
                "state": auth_state["state"],
                "authUrl": auth_state["authUrl"],
                "qrSvg": qrcode.to_svg(auth_state["authUrl"]),
                "expiresIn": 300,
            }

    def poll_login(self, state: str, timeout_ms: int = 300_000) -> dict[str, Any]:
        """阻塞轮询扫码结果。成功返回账号信息。"""
        if state != self._active_state:
            raise WBError("登录会话已失效，请重新获取二维码")
        self._login_abort.clear()
        try:
            token = self.client.poll_auth_token(
                state,
                timeout_ms=timeout_ms,
                should_abort=self._login_abort.is_set,
            )
            account = self.client.fetch_account(token)
        finally:
            # 无论成功、失败还是超时，都释放登录态，避免下次扫码被判定为
            # 「会话已失效」，同时防止 _active_state 长期占用。
            self._active_state = None
            self._active_url = None
        self.state.save_token(token)
        self.state.set_account(account)
        info = {
            "ok": True,
            "uid": account.get("uid", ""),
            "nickname": account.get("nickname") or account.get("displayName") or "",
        }
        # 写入后立即读回校验：云端容器若把凭据/密钥写在易失位置，
        # 这里会立刻暴露，而不是等下次访问才发现「又要重新扫码」。
        if not self.state.verify_credentials():
            info["warning"] = "凭据写入后无法读回，数据目录可能不可靠，重启后可能需要重新扫码"
        return info

    def cancel_login(self) -> None:
        self._login_abort.set()
        self._active_state = None
        self._active_url = None

    # ---------- 签到 ----------

    def query_status(self) -> dict[str, Any]:
        token, uid, enterprise_id = self.ensure_token()
        status = self.client.checkin_status(token, uid, enterprise_id)
        return {
            "ok": True,
            "status": status,
            "account": self.state.account(),
        }

    def do_checkin(self, *, source: str = "manual") -> dict[str, Any]:
        """执行签到（幂等：已签到时直接落地，不重复请求）。"""
        if not self._checkin_lock.acquire(blocking=False):
            return {"ok": False, "message": "签到正在进行中，请稍候"}
        try:
            # 自动触发时，当日已完成则不再请求（手动触发仍允许查询刷新）
            if source == "auto" and self.state.checked_in_today():
                return {"ok": False, "message": "今日已完成签到"}

            token, uid, enterprise_id = self.ensure_token()
            status = self.client.checkin_status(token, uid, enterprise_id)

            # 前置检查：网关已标记今日签到，直接落地，避免打 daily-checkin 报 1001
            if status.get("todayCheckedIn"):
                record = self._already_claimed_record(status, source)
                self.state.record_checkin(record)
                self._notify(record)
                return {"ok": True, "result": record, "status": status}

            result = self.client.daily_checkin(token, uid, enterprise_id)

            record = {
                "claimed": bool(result.get("claimed")),
                "status": result.get("status", "unknown"),
                "code": result.get("code", -1),
                "msg": result.get("msg", ""),
                "credit": int(result.get("credit") or 0),
                "streakDays": int(result.get("streakDays") or status.get("streakDays") or 0),
                "isStreakDay": bool(result.get("isStreakDay")),
                "dailyCredit": int(status.get("dailyCredit") or 0),
                "todayCheckedIn": bool(status.get("todayCheckedIn")),
                "totalCredits": status.get("totalCredits"),
                "source": source,
            }
            self.state.record_checkin(record)
            self._notify(record)
            return {"ok": True, "result": record, "status": status}
        except WBError as exc:
            # 业务码 1001 = 服务端判定今日已签到：落地为「已签到」而非失败，
            # 终止当日自动重试（否则会形成 30 秒一次的死循环）。
            if exc.code == 1001:
                record = {
                    "claimed": False,
                    "status": "already_claimed",
                    "code": 1001,
                    "msg": exc.message,
                    "credit": 0,
                    "streakDays": 0,
                    "source": source,
                }
                self.state.record_checkin(record)
                self.state.push_notification("info", "今日已签到", "当日积分已领取，明天再来")
                return {"ok": True, "result": record}
            self.state.push_notification("error", "签到失败", exc.message)
            # 失败后冷却 60 分钟，避免调度线程高频重试
            self._auto_cooldown_until = Date.now() + 60 * 60 * 1000
            return {"ok": False, "message": exc.message, "code": exc.code}
        finally:
            self._checkin_lock.release()

    @staticmethod
    def _already_claimed_record(status: dict[str, Any], source: str) -> dict[str, Any]:
        return {
            "claimed": False,
            "status": "already_claimed",
            "code": 1001,
            "msg": "今日已签到",
            "credit": 0,
            "streakDays": int(status.get("streakDays") or 0),
            "isStreakDay": bool(status.get("isStreakDay")),
            "dailyCredit": int(status.get("dailyCredit") or 0),
            "todayCheckedIn": True,
            "totalCredits": status.get("totalCredits"),
            "source": source,
        }

    # ---------- 通知 ----------

    def _notify(self, record: dict[str, Any]) -> None:
        if record["claimed"]:
            title = f"签到成功 +{record['credit']} 积分"
            level = "success"
            detail = f"连续签到 {record['streakDays']} 天"
        elif record["status"] == "already_claimed":
            title = "今日已签到"
            level = "info"
            detail = f"连续签到 {record['streakDays']} 天"
        elif record["status"] == "not_eligible":
            title = "当前不满足签到条件"
            level = "warn"
            detail = record["msg"] or "活动可能尚未开始或已结束"
        elif record["status"] == "event_ended":
            title = "签到活动已结束"
            level = "warn"
            detail = record["msg"] or ""
        else:
            title = "签到未成功"
            level = "error"
            detail = record["msg"] or f"业务码 {record['code']}"

        self.state.push_notification(level, title, detail)
        self._send_webhook(level, title, detail)

    def _send_webhook(self, level: str, title: str, detail: str) -> None:
        settings = self.state.settings()
        if not settings.get("webhookEnabled"):
            return
        url = (settings.get("webhookUrl") or "").strip()
        if not url:
            return
        payload = json.dumps(
            {
                "level": level,
                "title": title,
                "detail": detail,
                "time": now_str(),
                "source": "workbuddy-checkin",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        def _push() -> None:
            try:
                # 同样绕过环境代理，保证通知能直达用户配置的地址
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                opener.open(req, timeout=10).read()
            except Exception:
                pass  # 通知失败不影响签到主流程

        threading.Thread(target=_push, daemon=True).start()

    # ---------- 调度 ----------

    def start(self, on_error: Callable[[str], None] | None = None) -> None:
        self._credential_selfcheck(on_error)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(on_error,), daemon=True, name="checkin-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _credential_selfcheck(self, on_error: Callable[[str], None] | None) -> None:
        """启动时凭据自检：把「又要重新登录」的原因讲清楚。"""
        try:
            info = self.state.credential_status()
        except Exception:  # 诊断失败不能影响主流程
            return
        if info.get("fileExists") and not info.get("decryptable"):
            msg = f"已保存的登录凭据无法解密（密钥来源={info.get('keySource')}），请重新微信扫码授权"
            self.state.push_notification("error", "登录态已失效", msg)
            if on_error:
                on_error(msg)

    def _keep_token_alive(self, on_error: Callable[[str], None] | None) -> None:
        """定期主动续期，避免 refresh 链长期闲置后被服务端回收。"""
        if not self.state.has_token():
            return
        try:
            self.ensure_token()
        except WBError as exc:
            msg = f"登录态续期失败：{exc.message}（请重新微信扫码授权）"
            self.state.push_notification("error", "登录态已失效", msg)
            if on_error:
                on_error(msg)
        except Exception as exc:  # noqa: BLE001
            self.state.push_notification("warn", "登录态续期异常", str(exc))

    # ---------- 定时判定 ----------

    def _target_hm(self) -> tuple[int, int]:
        """读取设定时刻，解析失败时回退 09:10。"""
        raw = str(self.state.settings().get("checkinTime") or "09:10").split(":")
        try:
            return int(raw[0]), int(raw[1])
        except (ValueError, IndexError):
            return 9, 10

    def _seconds_until_target(self, now: datetime, hh: int, mm: int) -> float:
        """距离下一个签到时刻的秒数。

        <= 0 表示今日目标时刻已过。此时若今日尚未签到，应立刻补签；
        已签到则顺延到明天同一时刻。时区与 today_str() 一致（北京时间），
        否则凌晨前后会误判自然日。
        """
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (target - now).total_seconds()
        if delta > 0:
            return delta
        if not self.state.checked_in_today():
            return 0.0
        return (target + timedelta(days=1) - now).total_seconds()

    def _next_wait_seconds(self, now: datetime, hh: int, mm: int) -> float:
        """下一次醒来的等待时间：取「距目标时刻」与「冷却剩余」的合理解。"""
        delta = self._seconds_until_target(now, hh, mm)
        cooldown_left = (self._auto_cooldown_until - Date.now()) / 1000.0
        if delta <= 0:
            # 已过点且今日未签到 —— 未冷却则立即执行，冷却中则等到冷却结束
            return 0.0 if cooldown_left <= 0 else min(cooldown_left, SCHEDULER_IDLE_INTERVAL_S)
        wait = min(delta, SCHEDULER_IDLE_INTERVAL_S)
        if cooldown_left > 0:
            wait = max(wait, min(cooldown_left, SCHEDULER_IDLE_INTERVAL_S))
        return wait

    def _auto_due(self, now: datetime) -> bool:
        """当前是否应当执行一次自动签到。"""
        settings = self.state.settings()
        if not settings.get("autoCheckin"):
            return False
        if self.state.checked_in_today():
            return False
        if Date.now() < self._auto_cooldown_until:
            return False  # 上次失败仍在冷却期
        hh, mm = self._target_hm()
        return (now.hour, now.minute) >= (hh, mm)

    def run_due_checkin(self, *, reason: str = "scheduler") -> bool:
        """到点即签：条件满足时立即执行一次自动签到，返回是否触发。

        调度线程与 HTTP 请求路径共用同一入口 —— 云端容器休眠后进程不存在，
        只有被访问（或冷启动）时才有机会补上错过的定时任务。
        """
        try:
            if not self._auto_due(datetime.now(CST)):
                return False
            if not self.state.has_token():
                return False
            self.do_checkin(source="auto")
            return True
        except Exception as exc:  # 补签失败不得影响请求主流程
            self.state.push_notification("warn", "自动签到补触发异常", str(exc))
            return False

    def scheduler_snapshot(self) -> dict[str, Any]:
        """调度状态快照（供 /api/diag 诊断「定时到底会不会触发」）。"""
        now = datetime.now(CST)
        hh, mm = self._target_hm()
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "autoCheckin": bool(self.state.settings().get("autoCheckin")),
            "checkinTime": f"{hh:02d}:{mm:02d}",
            "now": now.strftime("%H:%M:%S"),
            "checkedInToday": self.state.checked_in_today(),
            "due": self._auto_due(now),
            "nextRunInSeconds": int(self._next_wait_seconds(now, hh, mm)),
            "cooldownSecondsLeft": int(max(0, (self._auto_cooldown_until - Date.now()) / 1000)),
        }

    def _loop(self, on_error: Callable[[str], None] | None) -> None:
        """调度主循环：按「距目标时刻的剩余秒数」精确对齐，到点立即签到。

        旧实现是固定 sleep 300 秒后再判断，最坏情况会晚 5 分钟才签，
        且进程刚启动时同样要空等第一个窗口。改为动态计算后：
          - 离目标时刻还远 → 每 5 分钟巡检一次（兼顾登录态续期）；
          - 临近目标时刻 → 按剩余秒数精确等待，误差 < 5 秒；
          - 已过点且今日未签到 → 立即补签（覆盖容器冷启动/休眠场景）。
        """
        last_keepalive = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_keepalive >= TOKEN_KEEPALIVE_INTERVAL_S:
                    last_keepalive = time.time()
                    self._keep_token_alive(on_error)

                hh, mm = self._target_hm()
                wait = self._next_wait_seconds(datetime.now(CST), hh, mm)

                if wait <= SCHEDULER_ALIGN_TOLERANCE_S:
                    triggered = self.run_due_checkin(reason="scheduler")
                    wait = self._next_wait_seconds(datetime.now(CST), hh, mm)
                    # 触发过一次后至少间隔 1 分钟，避免失败时反复打接口
                    if triggered:
                        wait = max(wait, SCHEDULER_MIN_RETRY_GAP_S)

                self._stop.wait(min(max(wait, 1), SCHEDULER_IDLE_INTERVAL_S))
            except Exception as exc:  # 调度线程必须永不退出
                if on_error:
                    on_error(str(exc))
                self._stop.wait(60)
