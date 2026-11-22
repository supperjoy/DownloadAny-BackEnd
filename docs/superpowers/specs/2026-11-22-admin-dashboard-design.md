# DownloadAny 后台管理系统设计（LumeAny-Admin）

日期：2026-11-22 · 状态：已批准（A 案：维持链接脱敏）

## 1. 意图与背景

运营者需要一个后台看板回答三个问题：**有多少人在用（使用人数）、下载了多少（下载量、转化率）、都在下载什么（视频标题与站点明细）**，并补充站点/格式/系统分布、失败原因等运营指标。

**隐私事实（2026-11-22 核实并定案）**：桌面端上报前刻意把链接脱敏为站点根（`analytics.ts:104` `new URL(...).origin + "/"`），`analytics_events.media_url` 库里只有 `https://www.youtube.com/` 这类站点根，**从来不存在完整视频链接**；terminal 事件（completed/failed/cancelled）也不带 `media_id`。需求方已确认维持该脱敏设计、不改客户端上报——本设计的"下载内容"一律以**标题 + 站点**为口径。

现有系统已具备完整的数据采集链路：桌面端 `src/lib/analytics.ts` 把 `download_started/completed/failed/cancelled`、`parse_*`、`app_open` 等事件（含 `media_url`、`media_title`、`media_site`、`format_label`、`platform`、`app_version`、`device_id`，登录时关联 `user_id`）上报到 `POST /api/analytics/events`，落 `analytics_events` 表。用户/登录/邮件/官网解析/反馈也各有表（`users`、`login_events`、`media_resolve_attempts`、`feedbacks`）。

缺口有三：**①** 现有 `GET /api/analytics/overview` 无鉴权、公网裸奔，且只有 4 个指标；**②** 没有任何展示界面（三个仓库零痕迹）；**③** 人数类统计、下载明细检索、分布类聚合没有现成接口。

本项目 = 扩展后端管理统计 API（加鉴权）+ 新建一个独立部署的后台前端。

## 2. 目标 / 非目标 / 反目标

**目标**
- 独立前端仓库 `LumeAny-Admin`（与 LumeAny-Main、DownloadAny 平级），静态部署，运营方在浏览器打开即用
- 后端新增一组 `/api/analytics/admin/*` 只读统计接口，统一由管理令牌保护；同时把裸奔的 `/api/analytics/overview` 收进保护
- 指标覆盖：用户总量/新增/活跃（DAU/WAU/MAU）、注册渠道、下载量与漏斗、按站点/清晰度/平台/版本分布、失败原因 Top、热门视频榜（标题+站点+清晰度+次数）、明细检索（标题关键字/事件类型/站点筛选）、官网解析量、反馈量、当前发布版本
- 时间范围全局切换（7/30/90 天）

**非目标（v1 明确不做）**
- 修改客户端上报以收集完整视频链接或 media_id——脱敏是已确认的产品隐私决定
- 用户管理操作（封禁/改密/删除）——纯只读看板
- 管理员账号体系与 `users.is_admin` 角色字段——用全局管理令牌（角色化留到以后，见 §11）
- 修改桌面端/官网的上报链路；给事件加字节数字段（下载体积估算 v1 不做，见 §11）
- 实时推送、任务队列、报表导出（CSV 导出见 §11）
- 独立鉴权服务/session——不引入新登录体系

**反目标**
- 管理数据在无令牌情况下可读（含现有 overview 的延续状态）
- 任何一个查询导致 MySQL 全表扫描把生产库拖垮

## 3. 约束

- 后端 FastAPI + SQLAlchemy + MySQL 8.0；沿用 pydantic-settings 配置风格与 `schema_migrations.py` 幂等启动迁移模式
- 鉴权复用现成 `_require_admin`（update_routes.py:60-67：`UPDATE_ADMIN_TOKEN` Bearer + `hmac.compare_digest` 常数时间比较）
- CORS：`CORSMiddleware` 已 `allow_headers=["*"]`，只需把后台域名加入 `cors_origins` 配置
- 桌面端发版与后端独立（analytics_routes.py:18 注释），接口形状需保持"宽松读取、严格定义指标"
- 单运营者使用，可接受令牌存 localStorage；部署环境为现有 VPS（api.lumeany.cc 同源基础设施）

## 4. 方案（已选定）

- **A. 官网内嵌 `/admin` 路由**：零新部署物，但后台代码混进公开站构建。用户未选。
- **B. 独立后台前端 + 复用现有后端 API（✅ 已选定）**：干净隔离、独立发版；代价是新起一个静态部署物。
- **C. 后端渲染单页**：最快起步，但看板每次改动都要发后端。用户未选。

## 5. 后端设计

### 5.1 鉴权与配置

- `_require_admin` 从 `update_routes.py` 提升到 `dependencies.py`（更名 `require_admin`，update_routes 改引用，行为不变）；错误 detail 改为"管理令牌无效"
- 新增模块 `admin_stats_routes.py`，router prefix `/api/analytics/admin`，全部 `dependencies=[Depends(require_admin)]`，只读、不写库
- 现有 `GET /api/analytics/overview` 加同样的 `require_admin`（已核实三个前端均无消费方，安全收紧；保留兼容字段不破坏响应形状）
- `.env.example`/`.env.server.example` 增加注释行：后台域名加入 `CORS_ORIGINS`（如 `https://admin.lumeany.cc`）；`UPDATE_ADMIN_TOKEN` 已存在则无需新密钥

### 5.2 端点与响应形状

统一查询参数：`days`（1–366，默认 30）。所有比率保留 1 位小数。所有计数查询命中现有索引（`ix_analytics_event_type_time` 等），Top 榜/明细带 `limit`。

| 端点 | 内容（精确口径见 §7） |
|---|---|
| `GET /overview` | 卡片指标一屏：total_users、new_users、dau/wau/mau（设备+登录用户两口径）、downloads_started/completed/failed/cancelled、download_success_rate、parse_requests、resolve_attempts_website，含环比 change |
| `GET /timeseries` | 日序列：date、active_devices、active_users、app_opens、parse_requested、downloads_completed、new_users |
| `GET /users` | 注册渠道分布（纯密码 / Google / 两者皆有，来自 users 表字段组合）+ 每日新增 |
| `GET /downloads` | 漏斗（parse_requested→parse_succeeded→download_started→download_completed 各阶段计数）、按站点、按清晰度 format_label、失败原因 Top10（error 已是客户端 `classifyError` 分类短标签/短文案，直接按值分组：error、count、last_seen） |
| `GET /top-media` | `?limit=20`：title、site、format_label（组内最近非空值）、completed_count、last_seen（热门视频榜，主视图；口径见 §7） |
| `GET /events` | 明细分页检索：`?event_type=&site=&q=&offset=&limit=`（limit≤200；q 对 media_title LIKE，转义 `%/_`）；返回 time、event_type、site（media_url 现存值即站点根，直接作为 site 展示）、title、format_label、platform、app_version、masked_ip、user_email_masked、error 截断 200 字符 |
| `GET /system` | 平台分布、app_version 分布（均按"窗口内每设备最近一条事件"归类）、近 30 天反馈数与最近 10 条摘要、当前 active 的 DesktopRelease/ComponentRelease 版本 |

新增查询所需索引：`analytics_events(event_type, media_site, created_at)` 若 explain 显示需要——v1 先不加，实现计划里安排一次真实数据 explain 验证（见 §9）。

### 5.3 隐私脱敏（管理端同样执行）

- 库内 `media_url` 本身只有站点根（§1 隐私事实），管理端能给出的内容明细即 **标题 + 站点 + 清晰度**；不存在也不新增"完整链接"字段
- IP 一律 `a.b.*.*` 掩码；邮箱一律首字符 + `***@域名`（保留域名便于判断渠道）；`device_id` 只出现在聚合里，明细不回传原值

## 6. 前端设计（新仓库 LumeAny-Admin）

- 栈（我的决定）：**Vue 3 + Vite + TypeScript + ECharts**（npm 依赖）+ 手写 CSS，不引 UI 框架、不引 Pinia/router 之外重依赖；只用 `vue-router`
- 目录：`src/views/{Login,Overview,Users,Downloads,Details,System}.vue`、`src/lib/api.ts`（fetch 封装，注入 Bearer，401 全局踢回登录）、`src/lib/format.ts`
- 布局线框：

```
┌────────┬──────────────────────────────────────────┐
│ 侧栏    │ 顶栏: [7天|30天|90天]  [刷新 ⟳]  [退出]     │
│ 概览    ├──────────────────────────────────────────┤
│ 用户    │ 指标卡 ×N ── 折线(日趋势) ── 柱状/条形      │
│ 下载    │ 漏斗 / 环形(分布) / 表格(榜单·明细·反馈)     │
│ 明细    │                                          │
│ 系统    │                                          │
└────────┴──────────────────────────────────────────┘
```

- 页面归属：概览=overview 卡片+timeseries；用户=users；下载=downloads+top-media；明细=events 检索表；系统=system
- 鉴权流程：登录页粘贴令牌 → `localStorage.adminToken` → 拉一次 overview 验证 → 通过则进主界面；自动刷新每 60s（页面可见时）
- 语言：中文界面，时间本地时区显示（后端 UTC）
- 环境变量：`VITE_API_BASE_URL`（默认 https://api.lumeany.cc）；无其他机密进前端

## 7. 指标口径定义（builder 取数依据）

- **窗口**：`[今天+1日的 00:00 UTC) - days, 今天+1日的 00:00 UTC)`（沿用现有 overview 算法，环比取前一等长窗口）
- **活跃设备** = 窗口内出现过任意 analytics 事件的 distinct `device_id`；**DAU/WAU/MAU** = 窗口 1/7/30 天同口径；**活跃用户** = 窗口内 `user_id IS NOT NULL` 的 distinct `user_id`
- **新增用户** = `users.created_at` 落在窗口内；**注册渠道** = `password_hash IS NOT NULL` / `google_subject IS NOT NULL` 两字段组合
- **下载量** = `download_completed` 事件数（完成口径，全站与"官网解析"分开）；**成功率** = completed / started
- **按站点** = `media_site`（后端由 media_url 主机名生成，已入库）；**按清晰度** = `format_label`（completed 事件）
- **失败原因 Top** = `download_failed` 按 `error` 值分组（客户端 `classifyError` 已收敛为短标签如 `proxyFailed` 或简短中文文案，无需截断归组；error 为空的记录单列"未分类"计数）
- **热门视频** = `event_type='download_completed'` 按 `media_title` 分组，site/format_label/last_seen 取组内最近一条非空值；**排除** title 为空、以及占位标题（`LIKE '任务 #%'`、`LIKE 'Task #%'`，源自 App.tsx `task.fallback`，每台机器各不相同会虚增碎片条目）；同一视频多清晰度会产生多行，属预期口径
- **官网解析量** = `media_resolve_attempts` 窗口内总数与 `successful=true` 数
- **平台/版本分布** = 窗口内每个 device_id 最近一条事件的 platform/app_version，再计数（避免旧版本设备重复计）

## 8. 部署（方案 B 的落地形态，我的决定）

- 与现有 VPS 同机：`pnpm build` → `dist/` 同步到 `/opt/lumeany-admin/`，nginx 新增 `admin.lumeany.cc` server block（TLS 证书复用 acme 方式），静态 root + SPA fallback；**仅内网/运营者使用可再加 IP 白名单（可选项）**
- 备选（文档记录即可）：Cloudflare Pages，构建时设 `VITE_API_BASE_URL`
- `CORS_ORIGINS` 追加 `https://admin.lumeany.cc` 并重启 backend
- 部署步骤含一条**实测核查**：服务器 `.env` 的 `TRUST_PROXY_HEADERS` 真值。若为 false，`ip_address` 列存的是 nginx 回环地址、明细的 masked_ip 无意义——本 v1 只记录该事实，**不**顺手改这个开关（它牵动登录限流与风控信任，属独立变更）

## 9. 性能

- 全部管理查询限定时间窗 + 走 `(event_type, created_at)` 索引；`top-media`/`events` 有 limit 上限
- MySQL 8 下窗口 ≤90 天时预计每查询扫"事件类型子集×窗口"行；若生产量级增长导致慢查询，后续把 timeseries 物化成日汇总表（v1 不做，留升级路径）
- 反目标兜底：实现计划中安排一步在 dev MySQL（docker-compose）灌 10 万级事件做接口耗时验证

## 10. 安全

- 唯一入口是 Bearer 管理令牌；`require_admin` 保持常数时间比较；未配置 `UPDATE_ADMIN_TOKEN` 时接口 401（fail-closed，与现行为一致）
- 全站 HTTPS；后台前端不内置任何密钥，令牌由运营者输入
- 明细接口不回传原始 IP/邮箱/device_id（§5.3 脱敏）
- 只读：admin 模块不出现 `db.add/commit`

## 11. 留给实现或后续决定的事项

- 视觉细节（配色、卡片排列、图表类型微调）由实现者定，风格对齐 DownloadAny 浅色侧栏语言即可
- CSV 导出、下载字节数统计、完整视频链接统计（需推翻 §1 脱敏定案并重做隐私决策）、`users.is_admin` 多管理员——均列为 v2 候选，v1 不做
- `UPDATE_ADMIN_TOKEN` 当前是否已在服务器 `.env` 配置：部署第一步需核实，未配置则按 README 流程生成
- 生产 MySQL 若数据量为 0（历史事件表可能近乎空），看板空态展示"暂无数据"即可，属预期

## 12. 验收标准

1. 无令牌/错令牌访问任一 admin 端点（含旧 overview）→ 401；带正确令牌 → 200
2. 六个端点在 dev compose（MySQL 8 + 种子数据）返回形状与 §7 口径一致，单测覆盖每个聚合函数的 SQL 语义
3. 浏览器完成：输令牌→五页全渲染→7/30/90 切换→热门榜排除占位标题、明细按标题关键字搜出 seeded 记录→脱敏字段符合 §5.3
4. 桌面端/官网代码零改动（A 案约束的回归验证）
5. 部署文档可复现：干净 VPS 按步骤能得到可用的 admin.lumeany.cc（含 TRUST_PROXY_HEADERS 核查记录）
