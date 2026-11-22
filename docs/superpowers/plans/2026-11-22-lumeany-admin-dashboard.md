# LumeAny-Admin 后台管理系统实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 DownloadAny 建一个只读运营看板：后端新增 `/api/analytics/admin/*` 统计接口（管理令牌保护），前端新建独立仓库 `LumeAny-Admin`（Vue 3 SPA）展示使用人数、下载量、内容明细等指标。

**Architecture:** 数据已在库中（`analytics_events` 等表，桌面端持续上报），本项目只做聚合查询 + 展示。后端一个新模块挂 7 个只读端点，鉴权把 `update_routes._require_admin` 提升到 `dependencies.py` 共享；前端独立仓库静态部署，Bearer 令牌由运营者登录页输入存 localStorage。

**Tech Stack:** 后端 FastAPI + SQLAlchemy 2 + MySQL 8（测试 SQLite in-memory，现有 pytest+httpx AsyncClient 模式）。前端 Vue 3.5 + Vite 6 + TS 5.7 + vue-router 4.6 + ECharts 5 + vitest 3，pnpm，版本号对齐 LumeAny-Main。

**Spec:** `docs/superpowers/specs/2026-11-22-admin-dashboard-design.md`（本仓库内；§5.2 端点表、§7 指标口径、§5.3 脱敏是取数与验收的唯一依据）

## Global Constraints

- 桌面端（DownloadAny 仓库）与官网（LumeAny-Main 仓库）**零改动**——A 案隐私定案，"链接明细"一律为标题+站点口径
- admin 模块**只读**：`admin_stats_routes.py` 内不得出现 `db.add` / `db.commit` / `db.delete` / session.execute 的 UPDATE/INSERT
- 鉴权：`Authorization: Bearer <UPDATE_ADMIN_TOKEN>`，`hmac.compare_digest` 常数时间比较；令牌未配置时**一律 401（fail-closed）**
- 时间窗：`days` 查询参数 1–366、默认 30（admin 端点）；旧 `/api/analytics/overview` 维持其原 1–90 clamp 与响应形状不变
- 窗口算法：`[今 UTC 日+1天 00:00) - days … +1天 00:00)`，环比取前一等长窗口（沿用 analytics_routes.py:186-189 写法）；库内时间为 naive UTC
- 脱敏：IP → `a.b.*.*`；邮箱 → 首字符 + `***@域名`；device_id 不回传明细；`media_url` 现值即站点根，作为 `site` 展示
- API 响应字段一律 snake_case（镜像后端 pydantic 模型名与字段名）
- 错误分组不做截断：`error` 已是客户端 classifyError 后的短标签/短文案，空值归入 `"未分类"`
- 热门榜排除占位标题：`title IS NULL OR title='' OR title LIKE '任务 #%' OR title LIKE 'Task #%'`
- 前端不引 UI 框架/Pinia/axios；只用 vue-router + echarts + 自写 fetch 封装
- 新仓库路径：`/Users/dengjun/Desktop/Download-Project/LumeAny-Admin`
- 提交信息用中文 conventional commits（后端仓库现有风格）；每个 Task 至少一次提交

## Review Focus

（每条在其归属 Task 里有对应测试，先读这里再读任务）

1. **空窗口不炸**：生产库 `analytics_events` 可能近乎为空（Google 登录都没配好，事件量未知）——窗口内 0 事件时，overview 各指标为 0/None change、timeseries 零填充整段日期、top-media/system 返回空列表，绝不 500 → Task 2
2. **令牌未配置 fail-closed**：`UPDATE_ADMIN_TOKEN=""` 时，即使请求不带 Authorization 也不得放行（`_require_admin` 现有逻辑已如此，测试钉住并推广到所有 admin 端点）→ Task 1（每个后续端点测试都重复"无头 401"一条）
3. **UTC 边界归属**：created_at 恰落在窗口起点/终点秒、以及跨日的 naive UTC 事件，日桶与窗口计数必须正确 → Task 2
4. **q 特殊字符**：标题检索含 `%` `_` `\` 与 CJK 时按字面匹配（LIKE 转义 + 参数绑定），`q="%"` 只命中标题真含百分号的行 → Task 5
5. **旧 overview 兼容**：加保护后旧 `/api/analytics/overview` 响应键集与语义完全不变（防未来消费方被静默破坏）→ Task 1

---

## 后端（仓库：DownloadAny-BackEnd）

### Task 1: require_admin 提升共享 + 旧 overview 收进保护

**Files:**
- Modify: `src/downloadany_backend/dependencies.py`（文件末尾追加 `require_admin`）
- Modify: `src/downloadany_backend/update_routes.py:60-67`（删 `_require_admin`，改 import）
- Modify: `src/downloadany_backend/analytics_routes.py:182`（overview 加 dependencies）
- Test: `tests/test_admin_stats.py`（新建，本 Task 起共用此文件）

**Interfaces:**
- Produces: `dependencies.require_admin(authorization: str | None = Header, settings = Depends(get_settings)) -> None`；401 detail `"管理令牌无效"`。测试注入令牌的既有手法：`monkeypatch.setattr(get_settings(), "update_admin_token", "test-admin-token")`（与 test_updates.py 一致，get_settings 为 lru_cache 实例）
- Produces: 请求头约定 `ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}`（后续 Task 测试直接复用此常量名）

- [ ] **Step 1: 写失败测试**（`tests/test_admin_stats.py`）

```python
ADMIN_TOKEN = "test-admin-token"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

@pytest.fixture
def admin_token(monkeypatch):
    from downloadany_backend.config import get_settings
    monkeypatch.setattr(get_settings(), "update_admin_token", ADMIN_TOKEN)

async def test_legacy_overview_requires_admin(client, admin_token):
    assert (await client.get("/api/analytics/overview")).status_code == 401
    ok = await client.get("/api/analytics/overview", headers=ADMIN_HEADERS)
    assert ok.status_code == 200
    # Review Focus 5：响应键集不变
    assert set(ok.json()) == {
        "days", "active_devices", "parse_requests", "downloads_completed",
        "download_success_rate", "daily", "recent_events",
    }

async def test_admin_fail_closed_when_token_unset(client):
    # Review Focus 2：空令牌时，无头与带头都 401
    assert (await client.get("/api/analytics/overview")).status_code == 401
    assert (await client.get("/api/analytics/overview", headers=ADMIN_HEADERS)).status_code == 401
```

- [ ] **Step 2: 跑测试确认失败** — `uv run pytest tests/test_admin_stats.py -v` → 预期：旧 overview 匿名请求返回 200 导致断言失败（fail 原因即"未保护"）
- [ ] **Step 3: 实现**：`dependencies.py` 末尾加 `require_admin`（把 update_routes.py:60-67 的 `_require_admin` 整体搬来，函数改公有名、detail 文案改"管理令牌无效"，需要 `import hmac` 与 `from fastapi import Header`——注意 dependencies.py 已 import HTTPBearer 不冲突）；update_routes.py 删原函数并 `from .dependencies import require_admin`、4 处 `Depends(_require_admin)` 改为 `Depends(require_admin)`；analytics_routes.py 的 `analytics_overview` 路由装饰器加 `dependencies=[Depends(require_admin)]` 并补 import
- [ ] **Step 4: 全量后端测试绿** — `uv run pytest -q` → 预期：全部 PASS（test_updates.py 的 admin 用例行为不变，只是换了函数名）
- [ ] **Step 5: Commit** — `git add src/downloadany_backend tests/test_admin_stats.py && git commit -m "refactor: 提升 require_admin 至 dependencies 并保护 analytics overview"`

### Task 2: admin_stats_routes 模块 + overview + timeseries

**Files:**
- Create: `src/downloadany_backend/admin_stats_routes.py`
- Modify: `src/downloadany_backend/app.py`（import + `app.include_router(admin_stats_router)`）
- Test: `tests/test_admin_stats.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `require_admin`、`ADMIN_HEADERS`、`admin_token` fixture
- Produces: router `admin_stats_routes.router`，`prefix="/api/analytics/admin"`，**全部路由** `dependencies=[Depends(require_admin)]`（后续 Task 往同文件追加端点，自动继承）
- Produces: 复用 `from .analytics_routes import AnalyticsMetric, _percentage_change`
- Produces: `seed_event(db, event_type, *, created_at, device_id=None, user_id=None, **fields) -> AnalyticsEvent`（写入测试文件的共用辅助：默认随机 uuid device、media_url 传站点根、media_site 按主机名回填，与生产 record_event 行为一致）
- Produces 响应模型（字段名即 API 契约，snake_case）：

```python
class AdminOverview(BaseModel):
    days: int
    total_users: int
    new_users: AnalyticsMetric
    dau_devices: int; wau_devices: int; mau_devices: int   # distinct device_id @1/7/30 天窗口
    dau_users: int; wau_users: int; mau_users: int          # distinct user_id NOT NULL
    downloads_started: AnalyticsMetric
    downloads_completed: AnalyticsMetric
    downloads_failed: AnalyticsMetric
    downloads_cancelled: AnalyticsMetric
    download_success_rate: AnalyticsMetric   # completed/started*100，started=0 时 0.0
    parse_requests: AnalyticsMetric
    website_resolves: AnalyticsMetric        # media_resolve_attempts 计数
    website_resolves_ok: AnalyticsMetric     # successful=True 计数

class AdminDailyPoint(BaseModel):
    date: date
    active_devices: int; active_users: int; app_opens: int
    parse_requests: int; downloads_completed: int; new_users: int

class AdminTimeseries(BaseModel):
    days: int
    daily: list[AdminDailyPoint]   # 零填充整段窗口（Review Focus 1/3）
```

- [ ] **Step 1: 写失败测试**（要点，均加 `admin_token` fixture + `ADMIN_HEADERS`）

```python
async def test_admin_endpoints_reject_no_token(client):  # 继承性防呆：路由若漏保护立刻红
    assert (await client.get("/api/analytics/admin/overview")).status_code == 401

async def test_admin_overview_empty_window(client, admin_token):
    # Review Focus 1：空库 → 全 0，daily 仍返回 days 个点，不 500
    r = await client.get("/api/analytics/admin/overview?days=30", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total_users"] == 0 and body["dau_devices"] == 0
    assert body["downloads_completed"]["value"] == 0
    ts = (await client.get("/api/analytics/admin/timeseries?days=7", headers=ADMIN_HEADERS)).json()
    assert len(ts["daily"]) == 7 and all(p["active_devices"] == 0 for p in ts["daily"])

async def test_admin_overview_counts_and_windows(client, admin_token, db):
    # Review Focus 3：恰在窗口起点的事件计入，早 1 秒不计入
    now = datetime.now(UTC).date()
    start = datetime.combine(now - timedelta(days=6), time.min)  # days=7 窗口起点
    seed_event(db, "download_completed", created_at=start)
    seed_event(db, "download_completed", created_at=start - timedelta(seconds=1))
    for _ in range(2): seed_event(db, "download_started", created_at=start)
    body = (await client.get("/api/analytics/admin/overview?days=7", headers=ADMIN_HEADERS)).json()
    assert body["downloads_completed"]["value"] == 1
    assert body["downloads_started"]["value"] == 2
    assert body["download_success_rate"]["value"] == 50.0
    # 3 条窗口内事件（1 completed + 2 started，seed_event 默认各自随机 device）
    # → wau/mau 数 distinct device == 3；start-1s 那条在窗口外，其 device 不得计入
    assert body["wau_devices"] == 3 and body["dau_devices"] == 1  # dau 窗口=1 天：仅恰在 start（6 天前）的事件都不属于 1 天窗口——days=7 请求里 dau 子窗口是"最近 1 天"，本测试种子的时间全在边界附近，dau 断言按实现窗口=今起最近 1 天算，若为 0 则改断言为 0（DAU 窗口独立于 days 参数，见实现步骤）
```

**窗口语义钉死（实现者必读）**：`dau/wau/mau` 是**固定** 1/7/30 天窗口（均锚定"今 UTC 日+1 天"终点向前数），**不随 `days` 参数变**；`days` 只影响卡片类环比指标与 timeseries。上面 dau 断言因此与请求里的 `days=7` 无关：种子事件时间戳 = 6 天前，任何一条都不在最近 1 天内 → `dau_devices == 0`，据此把注释行改成 `assert body["dau_devices"] == 0`。

- [ ] **Step 2: 跑测试确认失败** — `uv run pytest tests/test_admin_stats.py -v` → FAIL：404/路由不存在
- [ ] **Step 3: 实现**：新建 `admin_stats_routes.py`。窗口计算抄 analytics_routes.py:186-191 的 `period_start/period_end/previous` 三件套；单窗口事件计数用 `sum(case((event_type == X, 1), else_=0))` 模式（同 `_period_metrics`）；distinct device 活跃计数写小辅助 `_active(db, start, end, user_only: bool)`；DAU/WAU/MAU 各调一次 `_active`；`website_resolves*` 查 `MediaResolveAttempt.created_at`；`total_users/new_users` 查 `User.created_at`；`timeseries` 的 daily 用「按日聚合 + 空日零填充」两段式（照抄 analytics_routes.py:193-216 的 by_day 回填结构，追加 active_users 与 new_users 两列——new_users 按 `func.date(User.created_at)` 单独查再合并进点）。全部查询函数标注返回类型，端点 `response_model=` 模型名
- [ ] **Step 4: 跑测试确认通过** — `uv run pytest tests/test_admin_stats.py -v` → PASS
- [ ] **Step 5: Commit** — `"feat: admin 统计 overview/timeseries 端点"`

### Task 3: users 端点（注册渠道 + 每日新增）

**Files:**
- Modify: `src/downloadany_backend/admin_stats_routes.py`
- Test: `tests/test_admin_stats.py`

**Interfaces:**
- Consumes: Task 2 的 router、`seed_event`；新用户辅助 `seed_user(db, *, created_at, with_password, with_google) -> User`（追加进测试文件）
- Produces:

```python
class AdminChannelRow(BaseModel):
    kind: str          # "password" | "google" | "both"
    count: int

class AdminDayCount(BaseModel):
    date: date
    count: int

class AdminUsers(BaseModel):
    days: int
    total_users: int
    new_users: AnalyticsMetric            # 窗口新增 + 环比
    channels: list[AdminChannelRow]       # §7：password_hash/google_subject 字段组合
    new_daily: list[AdminDayCount]        # 零填充窗口每日
```

- [ ] **Step 1: 写失败测试**：种 4 个用户（纯密码×2、纯 Google×1、both×1、其一 created_at 窗口外），断言 channels 三桶计数正确、`total_users==4`、`new_users.value==3`、`new_daily` 长度==days 且和==3；无 token 401
- [ ] **Step 2: 确认失败**（404）
- [ ] **Step 3: 实现**：channels 口径见 spec §7——SQL 用 `case((and_(User.password_hash.is_not(None), User.google_subject.is_not(None)), "both"), (User.password_hash.is_not(None), "password"), else_="google")` 分组计数（注意先判 both）；无任何登录方式的用户不会存在于表里（注册必有密码或 Google），空桶**省略不返回**
- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** — `"feat: admin 统计 users 端点"`

### Task 4: downloads + top-media 端点

**Files:**
- Modify: `src/downloadany_backend/admin_stats_routes.py`
- Test: `tests/test_admin_stats.py`

**Interfaces:**
- Consumes: Task 2 router/seed_event
- Produces:

```python
class AdminStage(BaseModel):        name: str; count: int     # parse_requested|parse_succeeded|download_started|download_completed
class AdminSlice(BaseModel):        name: str; count: int     # 站点/清晰度；NULL 归 "unknown"
class AdminFailureRow(BaseModel):   error: str; count: int; last_seen: datetime
class AdminDownloads(BaseModel):
    days: int
    funnel: list[AdminStage]        # 按上述固定顺序
    sites: list[AdminSlice]         # completed 事件按 media_site，count DESC
    formats: list[AdminSlice]       # completed 按 format_label
    failures: list[AdminFailureRow] # download_failed 按 error 值分组，count DESC LIMIT 10；error 空→"未分类"
class AdminTopMediaRow(BaseModel):
    title: str; site: str | None; format_label: str | None
    completed_count: int; last_seen: datetime
class AdminTopMedia(BaseModel):     days: int; items: list[AdminTopMediaRow]
```

- [ ] **Step 1: 写失败测试**：种 completed（标题 A 共 4 条：清晰度 1080p×2 + 720p×2；标题 B×1；`任务 #12` 占位×5；title=NULL×1）与 failed（error="proxyFailed"×2、error=None×1），断言：top-media 第一名 title=A、completed_count==4、format_label 取组内最新一条的值、site 非空；**占位与空标题不进榜**（items 恰 2 行）；failures 含 `("proxyFailed",2)` 与 `("未分类",1)`；funnel 四段顺序与计数；sites/formats 桶含 unknown
- [ ] **Step 2: 确认失败**（404）
- [ ] **Step 3: 实现 top-media 两段式**（spec §7 口径 + 可移植性）：第一段 completed 过滤占位标题后 `GROUP BY media_title` 计数取 `MAX(created_at)`，`ORDER BY count DESC, last_seen DESC LIMIT n`；第二段把上一页 ≤n 个标题一次 `IN` 查询，`id = max(id)`（每标题最新行）取该行的 media_site/format_label 回填。禁用在 SQLite 语义不稳的窗口函数
- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** — `"feat: admin 统计 downloads/top-media 端点（标题榜排除占位标题）"`

### Task 5: events 明细检索端点

**Files:**
- Modify: `src/downloadany_backend/admin_stats_routes.py`
- Test: `tests/test_admin_stats.py`

**Interfaces:**
- Consumes: router；用户关联（email 脱敏需 join `User`）
- Produces:

```python
class AdminEventRow(BaseModel):
    id: int; occurred_at: datetime; event_type: str
    site: str | None; title: str | None; format_label: str | None
    platform: str; app_version: str
    masked_ip: str; email_masked: str | None
    error: str | None          # 截断 200 字符 + "…"
class AdminEventsPage(BaseModel):
    total: int; offset: int; limit: int; items: list[AdminEventRow]
```

- 查询参数：`days`（窗过滤）、`event_type`、`site`、`q`、`offset>=0`、`limit`（le=200, default 50）
- Produces（共用，Task 10-11 前端对应展示）: `admin_stats_routes.py` 顶部模块级私有函数 `_mask_ip(ip: str) -> str`（IPv4 → 前两段 + `.*.*`；非 IPv4/空 → `*.*`）与 `_mask_email(email: str | None) -> str | None`（`x***@domain`；非法值返回 None）；测试经端点响应断言，不直接 import 私有函数

- [ ] **Step 1: 写失败测试**（Review Focus 4 在此钉死）：种标题 `100% 真的_好看` 与 `普通标题` 各一事件；`q=100% 真的_` → total==1（字面命中）；`q=%` → total==1（只命中真含 % 的标题，不得当通配）；`q=_` → 1；`event_type` 与 `site` 过滤组合生效；断言 `masked_ip=="12.34.*.*"`（种子 ip `12.34.56.78`）、登录行 `email_masked=="x***@b.com"`、匿名行 None；offset 越界返回空 items 不 500
- [ ] **Step 2: 确认失败**（404）
- [ ] **Step 3: 实现**：LIKE 转义——`q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")` 后 `.contains(..., autoescape=False)` 改为手拼 `AnalyticsEvent.media_title.like(f"%{escaped}%")` 并在 SQL 尾 `ESCAPE '\'`（SQLAlchemy：`like(pattern, escape="\\")`）；count 用 `select(func.count())` 子查询同款过滤；error 截断 `error[:200] + ("…" if len(error)>200 else "")`
- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** — `"feat: admin 统计 events 明细端点（标题检索转义/字段脱敏）"`

### Task 6: system 端点

**Files:**
- Modify: `src/downloadany_backend/admin_stats_routes.py`
- Test: `tests/test_admin_stats.py`

**Interfaces:**
- Consumes: router；`seed_event`；`db` 直插 `Feedback`/`DesktopRelease`/`ComponentRelease`
- Produces:

```python
class AdminReleaseRow(BaseModel):
    kind: str        # "desktop" | "component"
    name: str        # desktop: f"{target}/{arch}"; component: component 名
    channel: str; version: str; published_at: datetime
class AdminSystem(BaseModel):
    days: int
    platforms: list[AdminSlice]      # §7：窗口内每 device_id 最新事件（max(id)）的 platform 分布
    versions: list[AdminSlice]       # 同上按 app_version
    feedback_count: int              # 窗口内
    feedback_recent: list[AdminFeedbackRow]  # 最近 10 条：kind/text(300 截断)/app_version/platform/created_at
    releases: list[AdminReleaseRow]  # active=True；desktop 按 (target,arch,channel) 取 published_at 最新一条；component 按 (component,channel) 同理
```

- [ ] **Step 1: 写失败测试**：同一 device 种先后两条事件（platform "Win32" 后 "Mac"、version 0.1.0 后 0.2.0），窗口另一 device 一条 Mac/0.1.0——断言 platforms 桶 `Mac:2, Win32:0 不出现`、versions `{"0.2.0":1,"0.1.0":1}`（每设备只计最新一次）；种 active 发布 0.2.0 + inactive 0.3.0 → releases 只含 0.2.0；feedback 两条（text 400 字符）→ count==2、recent[0].text 长 301（含 `…`）；无 token 401
- [ ] **Step 2: 确认失败**（404）
- [ ] **Step 3: 实现**：设备最新事件用 Task 4 同款两段式（`select(func.max(AnalyticsEvent.id)).group_by(device_id)` 进 `id.in_(subq)`）；releases 聚合直接 Python 端排序去重（行数少）
- [ ] **Step 4: 确认通过**
- [ ] **Step 5: Commit** — `"feat: admin 统计 system 端点（按设备最新版本分布与发布状态）"`

### Task 7: 配置注释、只读守卫、性能冒烟、README

**Files:**
- Modify: `.env.example` / `.env.server.example`（CORS_ORIGINS 行旁注释 `https://admin.lumeany.cc`；确认 UPDATE_ADMIN_TOKEN 说明存在，缺则补注释）
- Modify: `README.md`（新增「后台管理系统」一节：admin 端点清单、令牌来源、部署核查 TRUST_PROXY_HEADERS 真值的要求（spec §8）、指向 LumeAny-Admin 仓库）
- Create: `scripts/smoke-admin-stats.py`（性能冒烟脚本）
- Test: `tests/test_admin_stats.py`

**Interfaces:**
- Consumes: Task 1-6 全部端点
- Produces: `python scripts/smoke-admin-stats.py --base-url http://127.0.0.1:8000 --token <TOKEN> [--seed 100000]`：--seed 时直连 `DATABASE_URL` 灌 N 条混合类型事件（media_url 用 8 个站点根轮询、title 用 500 个假标题轮询），然后对 7 个端点各计时 3 次输出 JSON `{endpoint: p50_ms}`，任一 >1500ms 退出码 1

- [ ] **Step 1: 只读守卫测试**（钉反目标）

```python
def test_admin_module_is_read_only():
    src = (Path(__file__).resolve().parents[1] / "src/downloadany_backend/admin_stats_routes.py").read_text()
    for bad in ["db.add", ".commit(", ".delete(", "UPDATE ", "INSERT "]:
        assert bad not in src, bad
```

- [ ] **Step 2: 跑守卫确认通过**（实现本应只读，一次通过；不通过说明前面 Task 混入了写操作，立即回改）
- [ ] **Step 3: 写冒烟脚本 + 本地 MySQL 计时**：起 `docker compose up -d mysql`，脚本内 `os.environ["DATABASE_URL"]=compose 的 mysql url`；跑 `--seed 100000`；预期 7 端点全 <1500ms；若 top-media 或 events 超时，按 spec §9 记录 EXPLAIN 结果并把「加 `(event_type, media_site, created_at)` 索引」写成脚本输出建议（不自动改 schema）
- [ ] **Step 4: env 注释 + README 成文**；`uv run pytest -q` 全量绿
- [ ] **Step 5: Commit** — `"docs+ops: admin 统计部署说明、只读守卫与 10 万级冒烟"`

## 前端（新仓库：/Users/dengjun/Desktop/Download-Project/LumeAny-Admin）

### Task 8: 项目脚手架 + api/format 纯函数（vitest）

**Files:**
- Create: `package.json`、`vite.config.ts`、`tsconfig.json`(+node/dom)、`index.html`、`src/main.ts`、`src/env.d.ts`、`.gitignore`、`.env.example`（`VITE_API_BASE_URL=https://api.lumeany.cc`）、`src/router/index.ts`（5 条主路由 + /login，`meta.requiresToken`）
- Create: `src/lib/token.ts`（localStorage `adminToken` 读写、trim）
- Create: `src/lib/api.ts`：`apiGet<T>(path: string, params?: Record<string, string|number>): Promise<T>`——注入 Bearer；401 时清除 token 并 `window.dispatchEvent(new CustomEvent('admin:unauthorized'))`；非 2xx 抛 `Error(后端 detail 文本)`
- Create: `src/lib/format.ts`：`dayLabel(d: string): string`（UTC 日期串→本地 `M月D日`）、`fmtTime(iso: string): string`（→本地 `MM-DD HH:mm`）、`pct(v: number|null): string`（`null→'—'`，否则 `x.x%`）、`change(v: number|null): string`（带正负号或 `—`）
- Test: `src/lib/__tests__/api.spec.ts`、`src/lib/__tests__/format.spec.ts`（vitest）

**Interfaces:**
- Consumes: 后端 Task 1-6 全部 JSON（字段名原样 snake_case 落 `src/types.ts` 的 `AdminOverview/AdminDailyPoint/AdminUsers/AdminDownloads/AdminTopMedia/AdminEventsPage/AdminSystem/AnalyticsMetric` 接口——逐字段照抄本计划各 Task 模型定义，作为前端唯一类型来源）
- Produces: `apiGet`、`format.*`、`token.getToken/setToken/clearToken`（后续 Task 全从这里 import）

- [ ] **Step 1: 脚手架**：按文件清单建仓库（版本抄 LumeAny-Main package.json，加 `echarts`/`vitest`；scripts: dev/build(`vue-tsc --noEmit && vite build`)/test(`vitest run`)/typecheck；vite.config 与官网同构：`@` alias、dev proxy `/api→127.0.0.1:8000`；vitest 环境 `happy-dom` 不引——纯 node 环境即可（不测组件））。`pnpm install` 成功
- [ ] **Step 2: 写失败测试**：format：`pct(null)==='—'`、`pct(50.0)==='50.0%'`、`change(-12.3)==='-12.3%'`、`change(null)==='—'`；api（stub `globalThis.fetch`）：请求头含 `Bearer tok`、401 触发 `admin:unauthorized` 且 clearToken 生效、URL 拼 query
- [ ] **Step 3: 确认失败** — `pnpm test` → FAIL（模块不存在/断言失败）
- [ ] **Step 4: 实现至通过** — `pnpm test && pnpm typecheck && pnpm build` 全绿
- [ ] **Step 5: `git init` + Commit** — `git add -A && git commit -m "feat: 脚手架与 api/format 基础层"`（后端仓库无涉及）

### Task 9: 登录 + App 壳 + 概览页

**Files:**
- Create: `src/styles/base.css`（浅色令牌抄 DownloadAny `App.css :root` 风格：侧栏 208px、#f7f8fa 背景、卡片白底圆角）
- Create: `src/App.vue`、`src/state.ts`（`reactive<{range: 7|30|90}>`，顶栏与页面共用）、`src/components/TopBar.vue`、`src/components/SideNav.vue`、`src/components/MetricCard.vue`、`src/components/charts/LineChart.vue`（echarts init/dispose/resize + setOption(props.option)，Task 10 复用）
- Create: `src/views/LoginView.vue`、`src/views/OverviewView.vue`
- Modify: `src/router/index.ts`（挂路由 + `beforeEach`：`requiresToken && !token` → /login；已登录访问 /login → /）

- [ ] **Step 1**: `src/lib/api.ts` 的 unauthorized 监听放 App.vue：收到事件 → `router.push('/login')`
- [ ] **Step 2: 实现登录页**：单输入粘贴令牌（trim）→ `apiGet('/api/analytics/admin/overview', {days:1})` 验证 → setToken → 跳概览；失败显示后端 detail
- [ ] **Step 3: 实现壳 + 概览**：TopBar 含 7/30/90 分段按钮（写 state.range）、退出（clearToken→/login）；OverviewView：`total_users/new_users/dau_devices/dau_users` + 下载 4 计数 + 成功率 + 官网解析 两张卡片区 + timeseries LineChart（active_devices/downloads_completed 两条线）；所有页面统一 `watch(state.range, 重新 fetch)`
- [ ] **Step 4: 手动验收**（后端 `uv run uvicorn main:app --port 8000` + 用 `scripts/smoke-admin-stats.py --seed` 或少量手工种子）：错误令牌停留并提示；正确令牌进入；切 30/90 数字变化；`pnpm typecheck && pnpm test && pnpm build` 绿
- [ ] **Step 5: Commit** — `"feat: 登录、App 壳与概览页"`

### Task 10: 用户页 + 下载页（含热门榜、漏斗、失败 Top）

**Files:**
- Create: `src/components/charts/{BarChart,DonutChart,FunnelChart}.vue`（同 LineChart 模式）
- Create: `src/views/UsersView.vue`、`src/views/DownloadsView.vue`

- [ ] **Step 1**: UsersView：`/admin/users` → 三渠道 Donut + 每日新增 Bar；总数/新增卡片复用 MetricCard
- [ ] **Step 2**: DownloadsView：`/admin/downloads` → 漏斗 FunnelChart、站点/清晰度 Bar、失败 Top 表（error/count/last_seen）；`/admin/top-media` 表（标题/站点/清晰度/次数/最近时间）；**空态**：items 为空显示「窗口内暂无数据」（Review Focus 1 的前端半边）
- [ ] **Step 3**: `pnpm typecheck && pnpm build` 绿 + 手动走查：榜内不出现 `任务 #`；切换 range 生效
- [ ] **Step 4**: Commit — `"feat: 用户与下载统计页"`

### Task 11: 明细页 + 系统页

**Files:**
- Create: `src/views/DetailsView.vue`、`src/views/SystemView.vue`、`src/components/DataTable.vue`（轻量：columns props + slot）

- [ ] **Step 1**: DetailsView：filters（事件类型下拉=KNOWN 17 类、站点输入、标题关键字、range 全局）→ `/admin/events`；分页「加载更多」offset+=limit；行展开显示 error 全文（截断值 + 提示 200 字符截断）
- [ ] **Step 2**: SystemView：平台/版本 Donut、反馈表（kind/摘要/版本/时间）、发布状态表（name/channel/version/published_at）
- [ ] **Step 3**: 手动走查：q 输入 `%` 不再命中全部（Review Focus 4 前端半边）；masked_ip/邮箱格式符合 spec §5.3
- [ ] **Step 4**: `pnpm typecheck && pnpm build` 绿；Commit — `"feat: 事件明细检索与系统概况页"`

### Task 12: 全局刷新、空态、部署文档与终验收

**Files:**
- Create: `src/lib/useAutoRefresh.ts`（60s interval，`document.visibilityState==='visible'` 才触发；组件 onMounted 注册）
- Create: `README.md`（仓库：本地联调步骤、生产部署：build→`/opt/lumeany-admin/`→nginx `admin.lumeany.cc` server block 样例（root+SPA fallback+https）、CORS_ORIGINS 加白、**部署核查清单：UPDATE_ADMIN_TOKEN 已配置、TRUST_PROXY_HEADERS 真值记录（spec §8）**）
- Modify: 各 View 接入 useAutoRefresh；无数据页统一空态文案
- Create: `.github/workflows/build.yml`? 不做（YAGNI，README 记录手动发布即 v1）

- [ ] **Step 1**: useAutoRefresh 测试（vitest fake timers：隐藏页不请求、可见 60s 触发一次）→ 实现 → 各页接入
- [ ] **Step 2**: README 成文（含 spec §8 的 nginx 样例与核查清单）
- [ ] **Step 3: 终验收（spec §12 逐条）**：后端全量 `uv run pytest -q` 绿 + 前端 `pnpm test/typecheck/build` 绿 + 本地起两仓端到端走查清单：①错/无令牌全 401 ②种子数据后五页数字与 SQL 手查一致 ③7/30/90 与 60s 刷新 ④标题 `%` 检索字面命中 ⑤桌面/官网两仓库 `git status` 零改动 ⑥明细脱敏格式
- [ ] **Step 4**: Commit 两仓收尾 — `"feat: 自动刷新与部署文档，后台看板 v1 完成"`

---

## 依赖顺序

Task 1→2→3→4→5→6→7（后端严格串行，同文件追加）；Task 8 可与后端并行开（types 以本计划为准）；9→10→11→12。前端联调需要 Task 2+ 端点可用。
