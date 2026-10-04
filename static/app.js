/* WorkBuddy 签到助手 —— 前端逻辑（浅色主题） */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var loginState = null;   // 当前登录会话 state
  var pollTimer = null;

  // ---------- 工具 ----------
  function toast(msg, kind) {
    var el = $("toast");
    el.textContent = msg;
    el.className = "toast" + (kind ? " " + kind : "");
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.hidden = true; }, 3200);
  }

  function api(path, options) {
    return fetch(path, Object.assign({
      headers: { "Content-Type": "application/json" }
    }, options || {})).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok || body.ok === false) {
          throw new Error(body.message || ("请求失败 (" + res.status + ")"));
        }
        return body;
      });
    });
  }

  function fmtNum(v) {
    if (v === null || v === undefined || v === "") return "—";
    if (typeof v === "number") return v.toLocaleString("zh-CN");
    return String(v);
  }

  // ---------- 概览 ----------
  function loadOverview() {
    return api("/api/overview").then(function (data) {
      renderAuth(data);
      renderStatus(data);
      renderSettings(data.settings || {});
      renderNotifications(data.notifications || []);
      return data;
    }).catch(function (err) {
      $("authBadge").textContent = "服务异常";
      $("authBadge").className = "hero-badge warn";
      console.error(err);
    });
  }

  function renderAuth(data) {
    var badge = $("authBadge");
    if (data.loggedIn) {
      var name = (data.account && data.account.nickname) || "已登录";
      badge.textContent = "已授权 · " + name;
      badge.className = "hero-badge ok";
      ["statusCard", "settingCard", "historyCard"].forEach(function (id) { $(id).hidden = false; });
    } else {
      badge.textContent = "未授权";
      badge.className = "hero-badge warn";
      ["statusCard", "settingCard", "historyCard"].forEach(function (id) { $(id).hidden = true; });
    }
    // 有通知才显示通知卡片
    $("notifyCard").hidden = !(data.notifications && data.notifications.length);
  }

  function renderStatus(data) {
    var s = data.status || {};
    var today = $("statToday");

    if (s.todayCheckedIn || data.checkedInToday) {
      today.textContent = "已签到";
      today.className = "stat-value ok";
    } else {
      today.textContent = "未签到";
      today.className = "stat-value warn";
    }

    $("statDaily").textContent = s.dailyCredit ? "+" + s.dailyCredit : "—";
    $("statStreak").textContent = s.streakDays ? s.streakDays + " 天" : "—";
    $("statTotal").textContent = fmtNum(s.totalCredits);

    var btn = $("btnCheckin");
    var done = s.todayCheckedIn || data.checkedInToday;
    btn.disabled = !!done;
    btn.textContent = done ? "今日已签到" : "立即签到";

    var hint = $("checkinHint");
    if (data.statusError) {
      hint.textContent = "状态获取失败：" + data.statusError;
    } else if (s.active === false) {
      hint.textContent = "签到活动当前未开启";
    } else if (s.activityName) {
      hint.textContent = "活动：" + s.activityName;
    } else {
      hint.textContent = "";
    }
  }

  function renderSettings(settings) {
    $("autoCheckin").checked = !!settings.autoCheckin;
    if (settings.checkinTime) $("checkinTime").value = settings.checkinTime;
    if (settings.webhookUrl) $("webhookUrl").value = settings.webhookUrl;
  }

  function renderNotifications(items) {
    var ul = $("notifyList");
    ul.innerHTML = "";
    items.forEach(function (n) {
      var li = document.createElement("li");
      var t = document.createElement("time");
      t.textContent = n.time || "";
      var span = document.createElement("span");
      span.textContent = n.title + (n.detail ? " · " + n.detail : "");
      li.appendChild(t);
      li.appendChild(span);
      ul.appendChild(li);
    });
  }

  // ---------- 历史 ----------
  function loadHistory() {
    return api("/api/history?limit=30").then(function (data) {
      var rows = data.history || [];
      var tb = $("historyBody");
      tb.innerHTML = "";
      $("historyEmpty").hidden = rows.length > 0;

      rows.forEach(function (r) {
        var tr = document.createElement("tr");

        var tdDate = document.createElement("td");
        tdDate.textContent = r.date || "";
        tr.appendChild(tdDate);

        var tdRes = document.createElement("td");
        var pill = document.createElement("span");
        if (r.claimed) { pill.className = "pill ok"; pill.textContent = "成功"; }
        else if (r.status === "already_claimed") { pill.className = "pill info"; pill.textContent = "已签过"; }
        else if (r.status === "not_eligible" || r.status === "event_ended") { pill.className = "pill warn"; pill.textContent = "不可签"; }
        else { pill.className = "pill err"; pill.textContent = "失败"; }
        tdRes.appendChild(pill);
        tr.appendChild(tdRes);

        var tdCredit = document.createElement("td");
        tdCredit.textContent = r.credit ? "+" + r.credit : "—";
        tr.appendChild(tdCredit);

        var tdStreak = document.createElement("td");
        tdStreak.textContent = r.streakDays ? r.streakDays + " 天" : "—";
        tr.appendChild(tdStreak);

        var tdSrc = document.createElement("td");
        tdSrc.textContent = r.source === "auto" ? "自动" : "手动";
        tr.appendChild(tdSrc);

        tb.appendChild(tr);
      });

      var stats = null;
      api("/api/overview").then(function (d) {
        stats = d.stats || {};
        var s = stats;
        $("historyStat").textContent =
          "共 " + (s.totalDays || 0) + " 天 · 成功 " + (s.successDays || 0) + " 天 · 累计 " + (s.totalCredits || 0) + " 积分";
      });
    }).catch(function (e) { console.error(e); });
  }

  // ---------- 登录 ----------
  function startLogin() {
    var btn = $("btnStartLogin");
    btn.disabled = true;
    btn.textContent = "获取中…";
    $("scanStatus").textContent = "正在申请登录二维码…";
    $("qrHolder").innerHTML = '<p class="muted">生成中…</p>';

    api("/api/login/start", { method: "POST", body: "{}" })
      .then(function (data) {
        loginState = data.state;
        $("qrHolder").innerHTML = data.qrSvg;
        var link = $("authLink");
        link.href = data.authUrl;
        link.textContent = data.authUrl;
        $("btnCancelLogin").hidden = false;
        $("scanStatus").textContent = "请使用微信扫描二维码…";
        beginPolling();
      })
      .catch(function (err) {
        toast(err.message, "err");
        $("scanStatus").textContent = "获取失败：" + err.message;
        $("qrHolder").innerHTML = '<p class="muted">点击下方按钮重试</p>';
        btn.disabled = false;
        btn.textContent = "获取登录二维码";
      });
  }

  function beginPolling() {
    stopPolling();
    pollTimer = setInterval(function () {
      if (!loginState) return stopPolling();
      api("/api/login/status?state=" + encodeURIComponent(loginState))
        .then(function (data) {
          if (data.status === "success") {
            stopPolling();
            $("scanStatus").textContent = "授权成功，正在加载…";
            toast("授权成功", "ok");
            resetLoginUi();
            loadOverview().then(loadHistory);
          } else if (data.status === "failed") {
            stopPolling();
            $("scanStatus").textContent = "授权失败：" + (data.message || "未知错误");
            toast(data.message || "授权失败", "err");
          } else {
            $("scanStatus").textContent = "等待微信确认…";
          }
        })
        .catch(function () { /* 轮询期间网络抖动忽略 */ });
    }, 1500);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function resetLoginUi() {
    loginState = null;
    $("btnStartLogin").disabled = false;
    $("btnStartLogin").textContent = "重新获取二维码";
    $("btnCancelLogin").hidden = true;
    $("qrHolder").innerHTML = '<p class="muted">授权成功，可重新扫码切换账号</p>';
    $("authLink").textContent = "—";
    $("authLink").removeAttribute("href");
  }

  // ---------- 签到 ----------
  function doCheckin() {
    var btn = $("btnCheckin");
    btn.disabled = true;
    btn.textContent = "签到中…";
    api("/api/checkin", { method: "POST", body: "{}" })
      .then(function (data) {
        var r = data.result || {};
        if (r.claimed) {
          toast("签到成功，+" + r.credit + " 积分", "ok");
        } else if (r.status === "already_claimed") {
          toast("今日已签到", "ok");
        } else {
          toast(r.msg || "签到未成功", "err");
        }
        return loadOverview().then(loadHistory);
      })
      .catch(function (err) {
        toast(err.message, "err");
        btn.disabled = false;
        btn.textContent = "立即签到";
      });
  }

  // ---------- 设置 ----------
  function saveSettings() {
    var payload = {
      autoCheckin: $("autoCheckin").checked,
      checkinTime: $("checkinTime").value || "09:10",
      webhookUrl: $("webhookUrl").value.trim(),
      webhookEnabled: !!$("webhookUrl").value.trim()
    };
    api("/api/settings", { method: "POST", body: JSON.stringify(payload) })
      .then(function () { toast("设置已保存", "ok"); })
      .catch(function (err) { toast(err.message, "err"); });
  }

  function logout() {
    if (!confirm("确定清除本地登录凭证？清除后需要重新微信扫码。")) return;
    api("/api/logout", { method: "POST", body: "{}" })
      .then(function () {
        toast("已清除登录凭证");
        resetLoginUi();
        loadOverview();
      })
      .catch(function (err) { toast(err.message, "err"); });
  }

  // ---------- 绑定 ----------
  document.addEventListener("DOMContentLoaded", function () {
    $("btnStartLogin").addEventListener("click", startLogin);
    $("btnCancelLogin").addEventListener("click", function () {
      api("/api/login/cancel", { method: "POST", body: "{}" }).catch(function () {});
      stopPolling();
      resetLoginUi();
      $("scanStatus").textContent = "已取消";
    });
    $("btnCheckin").addEventListener("click", doCheckin);
    $("btnRefresh").addEventListener("click", function () { loadOverview().then(loadHistory); });
    $("btnSaveSettings").addEventListener("click", saveSettings);
    $("btnLogout").addEventListener("click", logout);

    loadOverview().then(function (data) {
      if (data.loggedIn) loadHistory();
    });
    // 低频兜底刷新（5 分钟）：签到每天只需一次，无需高频轮询；
    // 需要即时状态可点「刷新」按钮。
    setInterval(function () { loadOverview(); }, 300000);
  });
})();
