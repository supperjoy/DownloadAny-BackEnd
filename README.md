# DownloadAny Back-End

基于 FastAPI、SQLAlchemy 和 MySQL 的认证后端，包含：

- 邮箱注册：只需邮箱 + 密码 + 确认密码，注册成功即登录（不再发邮箱验证码/验证链接）
- 邮箱密码登录、忘记密码（邮件验证码或一次性链接）、登录后修改密码
- Google Identity Services ID Token 登录
- 30 分钟访问令牌、30 天刷新令牌轮换、服务端会话撤销和退出登录
- 邮箱/IP 双维度登录限流、账号临时锁定、失败 3 次后要求验证码；注册按 IP 限频（不再有邮件冷却这道闸）
- 邮件冷却时间及邮箱/IP 每小时发送上限（现在只约束找回密码邮件）
- 登录撞见未注册邮箱回 404「该邮箱尚未注册」、重复注册回 409「该邮箱已注册」——这是产品要的引导，
  不是疏漏：密码输错仍是统一的 401「邮箱或密码错误」，账号存在性只在“没注册”这一侧暴露，并按 IP 限频
- Argon2 密码哈希；数据库仅保存验证/刷新令牌的 SHA-256 摘要

## 本机运行

需要 Python 3.13、`uv` 和本机 MySQL。先在 MySQL 中创建数据库：

```sql
CREATE DATABASE downloadany CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

复制 `.env.example` 为 `.env`，至少修改：

```dotenv
DATABASE_URL=mysql+pymysql://root:你的密码@127.0.0.1:3306/downloadany?charset=utf8mb4
JWT_SECRET=使用密码生成器产生的至少32字符随机值
```

安装并启动：

```powershell
uv sync
uv run uvicorn downloadany_backend.app:app --reload
```

首次启动会自动创建数据表。接口文档位于 <http://127.0.0.1:8000/docs>，健康检查为
`GET /health`。

开发环境未配置 SMTP 时，验证邮件内容会写入服务端日志。实际发送邮件请设置
`SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD` 和 `SMTP_FROM`。

Google 登录还需把 Google Cloud 控制台中的“桌面应用”Client ID 写入
`GOOGLE_CLIENT_ID`，并把同一值写入桌面端的 `VITE_GOOGLE_CLIENT_ID`。桌面端通过
系统浏览器和 PKCE 获取 Google ID Token，再提交到 `POST /api/auth/google`。
后端会校验签名、签发者、受众、有效期以及 `email_verified`。

## 服务器部署（宿主机 MySQL + SSH 隧道连调）

面向“后端跑在远端、桌面端在本地开发”的形态。两个文件承担这套形态：

- `docker-compose.server.yml`：只起 backend，`network_mode: host`，uvicorn 绑 **127.0.0.1**:8000
- `.env.server.example` → 服务器上的 `.env`（密钥由脚本生成，绝不进镜像、不进 git）

在本机仓库根目录执行：

```bash
SERVER=root@你的服务器 bash scripts/deploy-server.sh --with-swap
```

脚本依次做：ssh 连通性检查 → （可选）建 2G swapfile → rsync 源码到 `/opt/downloadany-backend`
→ 跑 `scripts/provision-server-env.sh`（生成 `JWT_SECRET`/`DB_PASSWORD`，`CREATE USER IF NOT EXISTS`
+ `ALTER USER` 建应用账号 `downloadany_api@localhost/127.0.0.1`，把密码写回 `.env` 的
`DATABASE_URL`，再用临时 defaults-file 真连一次验证）→ `docker compose up -d --build` →
`curl 127.0.0.1:8000/health` 验收。全程不打印任何密钥。

部署完再跑一遍 `bash scripts/smoke-auth.sh`：它按产品流程走“未注册邮箱登录被引导注册 → 确认密码不一致被拒 →
注册即登录（`/me` 里 `has_password=true`、`email_verified=true`）→ 重复注册 409 → 密码登录 → 改密码 →
旧密码失效 → 找回密码 → 刷新令牌（body 与纯 Cookie 两条路径）”。只有找回密码那步需要那 6 位码：未配 SMTP 时
脚本自己 ssh 进容器日志取，验证码/令牌/密码一律不打印；拿不到日志就 `SKIP_RESET=1 bash scripts/smoke-auth.sh`
跳过那步。前提是本机隧道已开着。

两个刻意的设计，改动前先想清楚：

1. **为什么 host 网络而不是 `ports` 映射**：mysqld 通常只 `bind-address 127.0.0.1`，容器经
   bridge/`host.docker.internal` 根本连不上它。用 host 网络后 `DATABASE_URL` 里的 `127.0.0.1`
   就是宿主机，不需要动 MySQL 的 bind-address，也不需要给它开公网网卡。代价是 `ports:` 失效，
   监听面完全由 `command` 里的 `--host` 决定 —— 所以那里写死 `127.0.0.1`，公网不会多出 8000 端口。
   验收用 `ss -ltnp | grep :8000`，只应出现 `127.0.0.1`。**别指望防火墙兜底**：很多机器上 `ufw`
   装了但 `Status: inactive`，真正拦住外网的只有这个回环绑定；改 `--host` 之前先去云控制台确认安全组。
2. **为什么连调阶段 `ENVIRONMENT=development`**：刷新令牌 Cookie 的 `Secure` 标记由它决定。
   桌面端经隧道走 `http://127.0.0.1:8000`，`Secure` Cookie 在 http 下存不住，30 分钟访问令牌
   过期后刷新会静默失败（表现为“登录状态已失效”）。顺带好处：不配 `SMTP_HOST` 时验证码直接写
   容器日志，取码联调不用收邮件。

### 构建失败：服务器连不上 Docker Hub

国内 VPS 上 `docker compose up -d --build` 常见报错：

```
failed to resolve source metadata for docker.io/docker/dockerfile:1:
dial tcp 199.59.148.147:443: i/o timeout
```

原因不是本项目的问题，而是 Dockerfile 首行 `# syntax=docker/dockerfile:1` 会强制 BuildKit
去 docker.io 拉外部 frontend 镜像，而 `registry-1.docker.io` 的 DNS 常被污染到假 IP。本仓库
已去掉该行：Docker 29 内嵌的 dockerfile frontend 就支持 `RUN --mount=type=cache`，无需外部镜像；
基础镜像来自 `ghcr.io`，不受影响。若将来必须恢复该行，请给服务器配 registry mirror。

同理，`pip`/PyPI 拉依赖在这类机器上可能很慢甚至超时，构建首次耗时长属正常现象，别当成卡死。

### 本地连调

```bash
ssh -N -o ExitOnForwardFailure=yes -L 8000:127.0.0.1:8000 root@你的服务器
```

前端 `.env` 保持 `VITE_API_BASE_URL=http://127.0.0.1:8000` 即可 —— CORS Origin
（`http://localhost:1420` / `tauri://localhost`）和 `tauri.conf.json` 的 CSP `connect-src`
都已放行该地址，一行都不用改。取验证码：

```bash
ssh root@你的服务器 'cd /opt/downloadany-backend && docker compose -f docker-compose.server.yml logs --since=2m backend'
```

### 换成公网访问 / 正式生产

1. 域名解析到服务器，Caddy/nginx 反代 `127.0.0.1:8000` 并出 HTTPS 证书（TLS 终结在反代）。
2. `.env` 改 `ENVIRONMENT=production`、`TRUST_PROXY_HEADERS=true`，配好真实 `SMTP_*`；
   Cookie 变 `Secure` 必须走 https，桌面端 `VITE_API_BASE_URL` 与 CSP `connect-src` 同步改成该域名。
3. 想直连 8000 而不走反代也行，但要：`command` 改 `--host 0.0.0.0`，并且在云厂商安全组里只放行你的
   出口 IP（若改用 `ufw`，先 `ufw status` 确认它真是 active，别假设），
   并接受“账号密码和验证码在公网上明文传输”——只建议临时联调。

## 主要接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/auth/register` | 邮箱 + 密码 + 确认密码注册，成功即登录（已注册回 409） |
| POST | `/api/auth/login` | 邮箱密码登录（未注册回 404，前端引导去注册） |
| GET | `/api/auth/captcha` | 获取失败登录所需算术验证码 |
| POST | `/api/auth/forgot-password` | 请求重置邮件 |
| POST | `/api/auth/reset-password` | 使用 token 重置密码 |
| POST | `/api/auth/reset-password/code` | 使用邮件验证码重置密码 |
| POST | `/api/auth/change-password` | 已登录用户修改密码 |
| POST | `/api/auth/google` | Google 一键登录 |
| POST | `/api/auth/refresh` | 轮换刷新令牌 |
| POST | `/api/auth/logout` | 撤销当前会话 |
| GET | `/api/auth/me` | 获取当前用户 |
| POST | `/api/media/parse` | 使用后端 yt-dlp 解析媒体链接并返回可下载格式 |
| POST | `/api/media/resolve` | 返回可直接播放和下载的音视频合一临时地址 |
| POST | `/api/analytics/events` | 接收桌面端启动、解析和下载行为事件 |
| GET | `/api/analytics/overview?days=7` | 获取后台所需的匿名聚合统计与趋势 |
| GET | `/api/updates/desktop/{target}/{arch}/{version}` | 按通道、设备灰度返回 Tauri 更新 |
| GET | `/api/updates/components/{component}/{target}/{version}` | 返回独立签名组件更新 |

动态更新发布管理接口使用独立的 `UPDATE_ADMIN_TOKEN`，产物地址由
`UPDATE_CDN_BASE_URL` 与发布记录中的相对路径拼接。完整发布步骤见桌面端仓库
`docs/PHASE2_UPDATES.md`。

## 产品统计

桌面端会为每次安装生成随机设备 ID（不读取硬件标识），并记录 `app_open`、解析请求与结果、
下载开始与结果。登录状态下事件会关联用户，未登录时仍可按匿名设备统计日活。
事件通过唯一 `event_id` 幂等写入 `analytics_events`，后续后台可按日期、用户、设备、站点和
事件类型聚合 DAU、解析量、下载量与成功率。

登录连续失败 3 次后，接口返回 `428` 和 `X-Captcha-Required: true`。客户端应先调用
`GET /api/auth/captcha`，再把 `captcha_id` 和答案随登录请求提交。账号 15 分钟窗口内
失败 5 次会锁定 15 分钟；单 IP 失败 20 次会返回 `429`。这些阈值均可通过环境变量调整。

受保护接口使用 `Authorization: Bearer <access_token>`。刷新令牌同时通过 HttpOnly、
SameSite=Lax Cookie 下发，也保留在响应体中以支持原生客户端；Web 前端应优先使用 Cookie。

## 测试

```powershell
uv run pytest -q
```

测试使用内存 SQLite，不会触碰本机 MySQL，覆盖注册即登录、未注册邮箱引导注册、重复注册不能改他人密码、
旧版验证码登录/邮箱验证端点已下线、一次性令牌、失败触发验证码，以及重置密码后撤销全部会话。
