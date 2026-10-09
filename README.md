# WorkBuddy 签到助手

微信扫码授权后，每天自动领取 WorkBuddy 每日签到积分（100 分/天），并把结果通知给你。

纯 Python 标准库实现，**零第三方依赖**，支持本地运行、Docker 与 WorkBuddy 云端托管三种部署方式。

---

## 目录

- [功能](#功能)
- [安装（快速开始）](#安装快速开始)
- [部署方式](#部署方式)
- [环境变量](#环境变量)
- [使用说明](#使用说明)
- [技术要点](#技术要点)
- [目录结构](#目录结构)
- [安全设计](#安全设计)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 功能

| 功能     | 说明                                          |
| ------ | ------------------------------------------- |
| 微信扫码授权 | 页面内生成二维码，扫码确认后完成绑定，令牌加密落盘                   |
| 手动签到   | 页面「立即签到」按钮，实时返回本次获得的积分                      |
| 自动签到   | 每天在设定时间自动签到（默认 09:10，北京时间）                  |
| 结果通知   | 页面通知中心 + 可选 Webhook（企业微信 / 钉钉 / 飞书机器人）      |
| 签到历史   | 保留最近 120 天，含积分、连续天数、触发方式                    |
| 令牌自动续期 | access token 到期前自动用 refresh token 换新，无需重复扫码 |

---

## 安装（快速开始）

### 环境要求

| 项      | 要求                              |
| ------ | ------------------------------- |
| Python | **3.10 及以上**（仅标准库，无需 pip 安装任何包） |
| 网络     | 能访问 `https://www.workbuddy.cn`  |
| 浏览器    | 用于打开控制台页面扫码授权                   |

### 三步启动

```bash
# 1. 获取源码
git clone https://github.com/imeiming/workbuddy-checkin.git
cd workbuddy-checkin

# 2. 启动服务（无需安装依赖）
PORT=3000 python server.py

# 3. 打开控制台
#    浏览器访问 http://127.0.0.1:3000
```

Windows（PowerShell）的环境变量写法不同：

```powershell
$env:PORT = 3000
python server.py
```

启动后点&#x51FB;**「获取登录二维码」**，微信扫码即可完成授权。

> 若 3000 端口被占用，换一个即可：`PORT=8080 python server.py`。

---

## 部署方式

### 方式一：本地直接运行

适合本机使用。数据默认存放在：

- Windows：`%LOCALAPPDATA%\WorkBuddyCheckin\`
- 其他平台：`~/.workbuddycheckin/`

后台常驻（Linux / macOS）：

```bash
nohup env PORT=3000 WB_CHECKIN_DATA_DIR=/opt/wb-checkin/data \
  python server.py > checkin.log 2>&1 &
```

Linux 下注册为 systemd 服务（开机自启）：

```ini
# /etc/systemd/system/wb-checkin.service
[Unit]
Description=WorkBuddy Checkin Helper
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/wb-checkin
Environment=PORT=3000
Environment=WB_CHECKIN_DATA_DIR=/opt/wb-checkin/data
Environment=WB_CHECKIN_SECRET_KEY=请填写一串足够长的随机字符串
ExecStart=/usr/bin/python3 /opt/wb-checkin/server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now wb-checkin
sudo systemctl status wb-checkin
```

### 方式二：Docker

Dockerfile（保存为 `Dockerfile` 后构建）：

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY . /app

ENV PORT=3000
ENV WB_CHECKIN_DATA_DIR=/data

EXPOSE 3000
CMD ["python", "server.py"]
```

```bash
docker build -t wb-checkin .

docker run -d --name wb-checkin --restart unless-stopped \
  -p 3000:3000 \
  -v wb-checkin-data:/data \
  -e WB_CHECKIN_SECRET_KEY=请填写一串足够长的随机字符串 \
  wb-checkin
```

> `-v wb-checkin-data:/data` 必须保留，否则容器重启后令牌与签到历史会丢失。

### 方式三：WorkBuddy 云端托管

本项目是单端口 HTTP 服务，监听 `$PORT` 并绑定 `0.0.0.0`，符合云端托管要求：

- **启动命令必须显式写成 `python server.py`**：托管沙箱默认会去找 `main.py`，  
  不指定启动命令会直接发布失败（报 `can't open file '/workspace/main.py'`）
- 端口：环境变量 `PORT`
- 可选注入 `WB_CHECKIN_SECRET_KEY`；不注入也会自动在数据目录生成 `.keyfile` 持久化密钥

#### 登录态持久化机制（为什么不会频繁掉登录）

云端容器是 Linux，无 Windows DPAPI，凭据走 fallback 加密。密钥按以下优先级取值：

1. `WB_CHECKIN_SECRET_KEY` 环境变量
2. 数据目录下的 `.keyfile`（首次自动生成，与凭据同生命周期）
3. 机器派生密钥（`platform.node()` 等）——**会随容器 hostname 变化，是历史版本频繁掉登录的根因**

凭据与密钥都会**双写**：数据目录 + 应用目录（`.credentials.json` / `.keyfile`）。  
任一处存活即可免重新扫码：容器重建（hostname 变化）不影响解密；数据目录被整体重置时，  
会自动从应用目录的备份恢复并回写主文件。

排障用接口 `GET /api/diag`（不含任何敏感内容）：

```jsonc
{
  "uptimeSeconds": 12345,                    // 归零说明进程被重启/容器被回收
  "credential": {
    "fileExists": true,                      // 凭据文件是否存在
    "decryptable": true,                     // 能否解密（false = 需要重新扫码）
    "keySource": "keyfile",                  // env / keyfile / machine
    "mirrorExists": true,                    // 应用目录备份是否存在
    "reason": ""
  },
  "token": { "present": true, "hasRefreshToken": true, "accessExpiresAt": 0, "refreshExpiresAt": 0 }
}
```

---

## 环境变量

| 变量                             | 默认值       | 说明                                             |
| ------------------------------ | --------- | ---------------------------------------------- |
| `PORT`                         | `3000`    | 服务监听端口                                         |
| `HOST`                         | `0.0.0.0` | 监听地址                                           |
| `WB_CHECKIN_DATA_DIR`          | 平台默认目录    | 数据存放目录，容器部署建议指定到持久化卷                           |
| `WB_CHECKIN_SECRET_KEY`        | 无         | 凭据加密密钥（最高优先级）。不设置时会用数据目录的 `.keyfile`，容器重建同样可解密 |
| `WB_CHECKIN_NO_MIRROR`         | 无         | 设为任意值即关闭「应用目录凭据备份」，只写数据目录                      |
| `WB_CHECKIN_SELFPING`          | `1`       | 设为 `0` 关闭容器自心跳保活                               |
| `WB_CHECKIN_SELFPING_INTERVAL` | `30`      | 自心跳间隔（秒），最小 15                                 |
| `WB_CHECKIN_PUBLIC_URL`        | 无         | 自心跳目标地址。云端部署**建议显式设为自己的入口域名**；留空时先从请求 Host 推断，推断不到退回本机回环地址 |

生成随机密钥（可选，用于多机共享同一份凭据）：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## 使用说明

1. 打开应用首页，点&#x51FB;**「获取登录二维码」**
2. 用微信扫码并在手机上确认
3. 页面显示「授权成功」后即为绑定完成
4. 之后每天会在设定时间自动签到；也可随时&#x70B9;**「立即签到」**&#x624B;动触发

在**设置**中可调整：

- 自动签到开关与时间（默认 09:10，北京时间）
- Webhook 地址，用于把签到结果推送到企业微信 / 钉钉 / 飞书群机器人

---

## 技术要点

### 为什么必须服务端代理

WorkBuddy 网关（`www.workbuddy.cn`）**不返回** `Access-Control-Allow-Origin`，实测 OPTIONS 预检返回 404、POST 响应无 CORS 头，浏览器无法直接跨域调用。因此所有网关请求都由服务端转发，前端只访问同源的 `/api/*`。

### 定时签到如何保证一定触发

云端托管容器在**长时间无访问时会被回收**，回收期间进程不存在，进程内的定时线程自然无从运行——这是「设了时间却没自动签到」的根本原因。本项目用三层机制解决：

1. **容器自心跳保活（默认开启）**：进程每 30 秒访问一次自身公网地址的 `/api/health`，制造持续活动流量，阻止平台回收。实测 `uptime` 在 30 分钟观察期内单调增长、71 次心跳零失败。
2. **调度精准对齐**：调度线程按「距目标时刻的剩余秒数」动态等待，远离时刻时 5 分钟巡检、临近时刻精确对齐（误差 <5 秒）；若当前已过设定时刻且今日未签到，则**立即补签**。
3. **请求级兜底**：任何一次 `/api/overview` 请求（包括打开页面）都会检查是否满足签到条件，满足即刻签到并当场刷新返回——即使容器刚被回收冷启动，也能立刻补上。

> 注意：自心跳的目标**优先用环境变量 `WB_CHECKIN_PUBLIC_URL` 显式指定**。容器内 `Host` 头往往是平台内网域名（形如 `*.sandbox.xxx.club`），不可作为公网入口，代码会识别并跳过这类地址。

### 接口协议

逆向自 WorkBuddy Desktop 客户端，与官方客户端调用方式完全一致：

| 用途     | 方法   | 路径                                          |
| ------ | ---- | ------------------------------------------- |
| 申请登录态  | POST | `/v2/plugin/auth/state?platform=workbuddy`  |
| 轮询登录令牌 | GET  | `/v2/plugin/auth/token?state=<state>`       |
| 刷新令牌   | POST | `/v2/plugin/auth/token/refresh`             |
| 账号信息   | GET  | `/v2/plugin/account`                        |
| 签到状态   | POST | `/v2/billing/meter/checkin-activity-status` |
| 领取积分   | POST | `/v2/billing/meter/daily-checkin`           |

鉴权头：`Authorization: Bearer <accessToken>` + `X-User-Id: <uid>`。

签到业务码：`1001` 已签到、`1002` 不满足条件、`1003` 活动结束。

> 已签到（1001）会被落库为「今日已签到」而非失败，从而终止当日重试——否则会形成每轮调度重复报错的死循环。

### HEAD 请求与外部可用性监控

UptimeRobot / cron-job.org 等外部监控默认用 **HEAD** 请求探测。Python 的
`BaseHTTPRequestHandler` 未实现 `do_HEAD` 时会返回 `501 Not Implemented`，监控端据此判定
「站点离线」，外部保活会静默失效——这是一个很容易踩的坑。

本项目已实现 `do_HEAD`：复用 GET 路由，状态码与响应头（含 `Content-Length`）照常发出，
仅不发响应体，因此对 `/api/health`、`/api/overview`、`/`、`/static/*` 的 HEAD 探测均返回 200。

建议把外部监控的 URL 设为 **`/api/overview`**（而不是 `/api/health`）：它除了保活之外，
还会在唤醒冷启动容器时**立即检查并补签**，无需等待调度线程的下一轮巡检。

### 绕过环境代理

部分受管运行环境会把 `HTTP_PROXY` / `HTTPS_PROXY` 指向只放行内部流量的本地代理，urllib 默认读取这些变量会导致请求被拒（`upstream connect failed`）。`wb_client.py` 显式使用空 `ProxyHandler` 强制直连网关。

### 纯标准库 QR Code 编码器

不引入 `qrcode` 依赖，`qrcode.py` 自行实现了 Reed–Solomon 纠错、掩码选择与格式信息放置。实现过程中修复了三个会导致扫码失败的缺陷：

1. 掩码被错误施加到定位 / 定时 / 校正图形
2. 格式信息保留区判定反了，被数据位覆盖
3. 固定黑模块 `(size-8, 8)` 被格式信息写入覆盖（仅在版本 ≥ 3 时暴露）

### 调度策略

调度线程每 **5 分钟**唤醒一次判断是否到达签到时刻。每日只需签到一次，无需高频轮询；已签到或处于失败冷却期（60 分钟）时直接跳过。

---

## 目录结构

```
.
├── server.py        # HTTP 服务：路由 + 静态资源 + 网关代理
├── service.py       # 业务编排：登录、签到、令牌续期、定时调度、通知
├── wb_client.py     # WorkBuddy 网关客户端
├── state.py         # 状态层：令牌保管、签到历史、设置、通知队列
├── secret_store.py  # 凭据加密存储（DPAPI / 跨平台回退）
├── qrcode.py        # 纯标准库 QR Code 编码器（SVG 输出）
├── static/          # 控制台页面（HTML / CSS / JS）
└── requirements.txt # 无第三方依赖
```

---

## 安全设计

- **令牌加密存储**：Windows 走 DPAPI（`CryptProtectData`），其他平台走 `WB_CHECKIN_SECRET_KEY` 派生的对称加密，令牌永不明文落盘
- **令牌与状态分离**：`credentials.json` 加密，`state.json` 不含敏感字段
- **前端不接触令牌**：所有鉴权逻辑在服务端完成
- **原子写入**：所有文件落盘均为「临时文件 + `os.replace`」，避免并发产生半截文件
- **日志脱敏**：调试输出不打印 query 参数（其中可能包含临时登录态）

### 数据目录内容

| 文件                 | 说明                     |
| ------------------ | ---------------------- |
| `credentials.json` | 加密后的登录令牌，**不要提交到任何仓库** |
| `state.json`       | 签到历史与设置（无敏感字段）         |

仓库已附带 `.gitignore` 忽略上述文件；若不慎泄露，登录应用首页退出登录即可清除本地凭据。

---

## 常见问题

**扫码后一直显示「等待微信确认」**  
登录会话有效期约 5 分钟，超时需重新获取二维码。

**提示「登录已过期，请重新微信扫码授权」**  
refresh token 也已过期，属于账号侧安全策略，需重新扫码。

**提示「尚未授权」**  
说明本地令牌文件不存在或已被删除（例如重新部署、清空数据卷），重新扫码一次即可。

**提示「当前不满足签到条件」（1002）/「签到活动已结束」（1003）**  
签到活动尚未开始或已结束，可在页面查看活动起止时间。

**自动签到没有触发**  
自动签到依赖进程在线。离线期间不会补执行；下次打开会显示当日状态，点「立即签到」手动补签即可。

**容器内提示需要重新扫码**  
检查是否注入了 `WB_CHECKIN_SECRET_KEY`，且数据卷是否持久化。密钥变更后旧令牌无法解密。

---

## 免责声明

本项目为个人自用工具，仅供学习研究与技术交流，按其用途引发的后果由使用者自行承担。

- 接口协议逆向自本地客户端，仅用于实现客户端已有的签到功能，未发现在破坏任何服务与技术保护措施
- 使用前请务必确认自身有权访问相应账号，并**遵守 WorkBuddy 的服务条款**
- 若官方接口或协议发生变化，本项目可能失效。若收到官方停止相关使用的要求，请立即停止使用
- 请勿将本项目用于任何批量注册、账号交易或超出正常签到范围的用途

---

## License

[MIT](LICENSE)
