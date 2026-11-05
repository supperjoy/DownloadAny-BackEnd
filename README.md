# DownloadAny Back-End

基于 FastAPI、SQLAlchemy 和 MySQL 的认证后端，包含：

- 邮箱注册、6 位验证码和一次性验证链接
- 邮箱密码登录、忘记密码、登录后修改密码
- Google Identity Services ID Token 登录
- 30 分钟访问令牌、30 天刷新令牌轮换、服务端会话撤销和退出登录
- 邮箱/IP 双维度登录限流、账号临时锁定、失败 3 次后要求验证码
- 邮件冷却时间及邮箱/IP 每小时发送上限
- 注册、重发验证邮件和忘记密码接口采用统一状态码与响应，避免枚举已注册邮箱
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

Google 登录还需把 Google Cloud 控制台中的 Web Client ID 写入
`GOOGLE_CLIENT_ID`；前端拿到的 Google credential 直接提交到 `POST /api/auth/google`。
后端会校验签名、签发者、受众、有效期以及 `email_verified`。

## 主要接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/auth/register` | 邮箱注册 |
| POST | `/api/auth/verification/resend` | 重发验证邮件 |
| POST | `/api/auth/verification/code` | 验证 6 位验证码 |
| POST | `/api/auth/verification/link` | 验证一次性链接 token |
| POST | `/api/auth/login/code/send` | 发送独立的邮箱登录验证码 |
| POST | `/api/auth/login/code` | 使用一次性验证码登录或激活新用户 |
| POST | `/api/auth/login` | 密码登录 |
| GET | `/api/auth/captcha` | 获取失败登录所需算术验证码 |
| POST | `/api/auth/forgot-password` | 请求重置邮件 |
| POST | `/api/auth/reset-password` | 使用 token 重置密码 |
| POST | `/api/auth/change-password` | 已登录用户修改密码 |
| POST | `/api/auth/google` | Google 一键登录 |
| POST | `/api/auth/refresh` | 轮换刷新令牌 |
| POST | `/api/auth/logout` | 撤销当前会话 |
| GET | `/api/auth/me` | 获取当前用户 |

登录连续失败 3 次后，接口返回 `428` 和 `X-Captcha-Required: true`。客户端应先调用
`GET /api/auth/captcha`，再把 `captcha_id` 和答案随登录请求提交。账号 15 分钟窗口内
失败 5 次会锁定 15 分钟；单 IP 失败 20 次会返回 `429`。这些阈值均可通过环境变量调整。

受保护接口使用 `Authorization: Bearer <access_token>`。刷新令牌同时通过 HttpOnly、
SameSite=Lax Cookie 下发，也保留在响应体中以支持原生客户端；Web 前端应优先使用 Cookie。

## 测试

```powershell
uv run pytest -q
```

测试使用内存 SQLite，不会触碰本机 MySQL，覆盖完整密码认证生命周期、一次性令牌、
防邮箱枚举、失败触发验证码，以及重置密码后撤销全部会话。
