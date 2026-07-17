# blive-monitor · Code Wiki

> 多平台直播 / 新作品监控 + 多渠道推送 项目的结构化代码文档。
> 覆盖：整体架构、模块职责、关键类与函数、依赖关系、数据模型、运行方式。

---

## 目录

1. [项目定位与功能概览](#1-项目定位与功能概览)
2. [整体架构](#2-整体架构)
3. [目录结构](#3-目录结构)
4. [模块职责详解](#4-模块职责详解)
5. [关键类与函数说明](#5-关键类与函数说明)
6. [数据模型（DB 表 / JSON 文件）](#6-数据模型db-表--json-文件)
7. [依赖关系](#7-依赖关系)
8. [项目运行方式](#8-项目运行方式)
9. [配置项与环境变量](#9-配置项与环境变量)
10. [测试体系](#10-测试体系)

---

## 1. 项目定位与功能概览

**`racheko-lab/blive-monitor`** 是一个用 Python 实现的轻量级直播监控工具：

- **核心能力**：定时检测 **B站 / 抖音** 主播是否开播，及抖音是否有新作品发布；状态变化时通过多渠道推送通知。
- **支持平台**：
  - 直播监控：B站、抖音（常驻）；预留快手、微信视频号、淘宝直播适配器骨架（按 `BLIVE_CONFIG.platforms` 启用）。
  - 新作品监控：抖音（完整三层策略 + 中毒防护）；预留小红书笔记适配器骨架。
- **推送渠道**：Bark / Server酱 / 企业微信 / PushPlus / Telegram，统一由 [push_utils.py](file:///workspace/push_utils.py) 调度。
- **双运行形态**：
  1. **CI 形态（原版）**：GitHub Actions 每 5 分钟跑一次检测脚本，状态写 JSON 文件并 commit 回 master。
  2. **后端形态（阶段四）**：FastAPI + SQLite + APScheduler 自驱调度，REST API + 持久化 DB，可独立部署（Docker）。
- **技术栈**：
  - 后端：Python 3.8+（检测脚本仅标准库），FastAPI / SQLAlchemy 2.0 / APScheduler（阶段四），Playwright（抖音新作用）。
  - 前端：原生 HTML + JavaScript（`monitor.html` 等多个变体）。
  - 部署：GitHub Actions + GitHub Pages / Netlify / Docker。

---

## 2. 整体架构

### 2.1 双形态总览

```
┌────────────────────────────────────────────────────────────────────┐
│                        blive-monitor 总体架构                       │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  [形态 A · CI 模式（原版）]                                          │
│    GitHub Actions (check.yml) ─每5min─▶ check_status.py            │
│                                       └─▶ check_new_posts.py       │
│                                       └─▶ auto_summary.py          │
│                                       └─▶ transcode_covers.py      │
│            持久化：state.json / tracking.json / history.json        │
│            （git commit 回 master 作为唯一真相来源）                  │
│            推送：push_utils.dispatch_event                          │
│            去重：notify_dedup.json（第二道防线）                     │
│            合并：merge_state.py（语义合并避免 rebase 冲突）           │
│                                                                    │
│  [形态 B · 后端模式（阶段四）]                                        │
│    FastAPI (backend/app.py) ─ /api/v1 ─▶ 8 个 router                │
│    APScheduler (backend/jobs/scheduler.py)                          │
│       ├─ live_check  每 5 min                                       │
│       ├─ post_check  每 10 min（ENABLE_POST_CHECK=true）            │
│       ├─ summary     每轮 live 后惰性评估                            │
│       └─ transcode   每轮 post 后顺带执行                            │
│    持久化：SQLite (data/blive.db) + 8 张表                          │
│    检测编排：DetectionService → 复用 check_status.run_live_check     │
│              / check_new_posts.run_post_check / auto_summary        │
│              （通过 LivePersist/PostPersist/SummaryPersist 门面解耦）│
│    多平台适配器：AdapterRegistry.from_config(cfg_all)               │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 后端分层架构（阶段四）

```
┌──────────────────────────────────────────────────────────────┐
│  HTTP 层 (backend/api/*.py)                                  │
│  rooms / posts / events / notify / config_api /              │
│  summary_api / silence_api / jobs_api                        │
│  鉴权：deps.require_auth（AUTH_TOKEN 空=放行）                 │
├──────────────────────────────────────────────────────────────┤
│  编排层 (backend/jobs/)                                      │
│  Scheduler → DetectionService → run_live/run_post/...        │
│  Persist 门面：LivePersist / PostPersist / SummaryPersist    │
│  （把 DB 写操作收敛到 Persistence/Dedup/History/NotifyLog）   │
├──────────────────────────────────────────────────────────────┤
│  适配器层 (backend/adapters/)                                │
│  PlatformAdapter 抽象基类 + AdapterRegistry 注册表           │
│  BilibiliAdapter / DouyinAdapter / KuaishouAdapter /         │
│  ChannelsAdapter / XhsAdapter / TaobaoLiveAdapter            │
│  产出 RoomModel / PostModel 归一化模型，绝不写 DB/JSON        │
├──────────────────────────────────────────────────────────────┤
│  核心服务层 (backend/core/)                                  │
│  Persistence（rooms/posts/events 落库）                      │
│  DedupService（去重账本）                                     │
│  HistoryStore（含 30min 节流）                                │
│  NotifyLogStore（推送账本）                                   │
├──────────────────────────────────────────────────────────────┤
│  持久化层 (backend/db.py / models.py / config_store.py)      │
│  SQLAlchemy 2.0 sync ORM + SQLite (WAL + 全局写锁)           │
│  8 张表：rooms/posts/events_history/notify_log/notify_dedup/ │
│         config_kv/summary_state/silence_state                │
├──────────────────────────────────────────────────────────────┤
│  复用层（仓库根的既有纯函数检测模块）                          │
│  check_status.py / check_new_posts.py / auto_summary.py      │
│  common.py / push_utils.py / notify_dedup.py / log_utils.py  │
│  transcode_covers.py / merge_state.py / state_prune.py       │
└──────────────────────────────────────────────────────────────┘
```

### 2.3 关键设计决策

| 决策点 | 方案 | 理由 |
|---|---|---|
| 后端框架 | FastAPI（sync `def` 路由 + 线程池） | 与同步检测逻辑（urllib / Playwright sync）零冲突 |
| 数据库 | SQLite + SQLAlchemy 2.0 sync ORM | 零依赖、单机够用；可平滑切 Postgres |
| 并发写 | WAL 模式 + 全局 `WRITER_LOCK` | SQLite 单写者；防 `database is locked` |
| 调度 | APScheduler `AsyncIOScheduler` 自驱 | 替代 CI 抢推，过渡期 CI 退役 |
| 检测逻辑复用 | 直接 import 既有 `check_status.run_live_check` 等纯编排函数 | 抓取/解析/路由/模板**一字不改**，行为不降级 |
| 持久化解耦 | `Persist` 门面（duck-typed） | `check_status` 不依赖 SQLAlchemy，CI/后端双形态可跑 |
| 时间统一 | 北京时间字符串 `YYYY-MM-DD HH:MM:SS` | 与 history.json/state.json 逐字节一致 |
| 配置 | `BLIVE_CONFIG` 整段存 `config_kv` 表 | 语义 100% 兼容，`dispatch_event` 直接消费 |
| 去重 | `notify_dedup` 表 + 文件版 `notify_dedup.json` 双形态 | 直播 key 冷却 2h；新作品永久；防重复推送第二道防线 |

---

## 3. 目录结构

```
blive-monitor/
├── 【检测入口脚本（CI 形态）】
│   ├── check_status.py          # B站/抖音直播状态检测主脚本
│   ├── check_new_posts.py       # 抖音新作品检测（三层策略+中毒防护）
│   ├── auto_summary.py          # 定时摘要自动投递 CLI（A1）
│   ├── transcode_covers.py      # 抖音新作封面转存到仓库 raw URL（D1）
│   └── run.sh                   # 一键运行脚本（once/posts/all/loop）
│
├── 【共享工具模块】
│   ├── common.py                # 时间/JSON 读写/静默/路由/模板等公共工具
│   ├── push_utils.py            # 多通道推送（Bark/Server酱/企微/PushPlus/TG）+重试
│   ├── notify_dedup.py          # 通知去重账本（文件版，CI 形态用）
│   ├── log_utils.py             # 运行时日志 + history.json 读写统一模块
│   ├── merge_state.py           # CI 状态文件语义合并（替代 git rebase）
│   └── state_prune.py           # 级联清理（孤儿 history/tracking 记录）
│
├── 【后端（阶段四）】backend/
│   ├── app.py                   # FastAPI 入口（lifespan+router 挂载+/healthz）
│   ├── config.py                # 环境变量驱动的运行时配置
│   ├── db.py                    # SQLite engine/SessionLocal/WRITER_LOCK/PRAGMA
│   ├── models.py                # SQLAlchemy ORM（8 张表）
│   ├── schemas.py               # Pydantic 请求/响应模型
│   ├── config_store.py          # ConfigStore：BLIVE_CONFIG/summary/silence 读写
│   ├── api/                     # REST API 路由层
│   │   ├── deps.py              #   鉴权依赖 require_auth + get_db_session
│   │   ├── rooms.py             #   /rooms CRUD + 状态读写
│   │   ├── posts.py             #   /posts 列表 + 记录
│   │   ├── events.py            #   /events 历史查询
│   │   ├── notify.py            #   /notify/log + /notify/dedup
│   │   ├── config_api.py        #   /config 读写
│   │   ├── summary_api.py       #   /summary/state 读写
│   │   ├── silence_api.py       #   /silence/state 读写
│   │   └── jobs_api.py          #   /jobs/check 手动触发 + /jobs/status
│   ├── core/                    # 核心服务层
│   │   ├── persistence.py       #   Persistence：rooms/posts/events 落库
│   │   ├── dedup.py             #   DedupService：DB 版去重账本
│   │   ├── history_store.py     #   HistoryStore：含 30min 节流
│   │   └── notify_log_store.py  #   NotifyLogStore：推送账本
│   ├── jobs/                    # 编排与调度
│   │   ├── scheduler.py         #   AsyncIOScheduler 封装 + 重入保护
│   │   ├── registry.py          #   Scheduler 单例注册表（避免循环依赖）
│   │   ├── detection_service.py #   DetectionService：编排入口
│   │   ├── live_check.py        #   LivePersist：直播检测持久化门面
│   │   ├── post_check.py        #   PostPersist：新作检测持久化门面
│   │   ├── summary_job.py       #   SummaryPersist：摘要投递持久化门面
│   │   └── transcode_job.py     #   封面转存后端化
│   └── adapters/                # 多平台适配器
│       ├── base.py              #   PlatformAdapter 抽象基类 + RoomModel/PostModel
│       ├── registry.py          #   AdapterRegistry 注册表
│       ├── bilibili.py          #   B站（复用 fetch_bilibili_batch）
│       ├── douyin.py            #   抖音（直播+新作，复用三层策略）
│       ├── kuaishou.py          #   快手（live_api + SSR 降级 + graphql 新作）
│       ├── channels.py          #   微信视频号（开放平台 / playwright 双模）
│       ├── xhs.py               #   小红书（仅笔记，签名 API 骨架）
│       └── taobao_live.py       #   淘宝直播（仅直播，SSR 解析）
│
├── 【前端】
│   ├── index.html               # 首页（多监控页变体入口）
│   ├── monitor.html             # 主监控页（标签切换式，正式版）
│   ├── monitor-dashboard.html   # 双栏仪表盘变体
│   ├── monitor-feed.html        # 统一动态流变体
│   ├── monitor-hero.html        # 英雄区+网格变体
│   ├── libsodium.js             # 前端加密库（配置加密写 Secret 用）
│   ├── api/rooms.js             # 房间输入校验与去重（RoomValidator，ES Module）
│   ├── cors-proxy-worker.js     # [已弃用] Cloudflare Worker CORS 代理
│   └── worker.js                # [已弃用] Cloudflare Worker 触发器
│
├── 【配置 / 状态文件】
│   ├── rooms.json               # 直播监控房间列表（前端直连 GitHub API 编辑）
│   ├── post_rooms.json          # 抖音新作品监控账号列表
│   ├── config/platforms.example.json  # 多平台适配器配置示例
│   ├── summary_state.json       # 摘要投递状态（lastSent/失败冷却）
│   ├── silence_state.json       # 静默时段状态
│   ├── assets/covers/           # 抖音新作封面转存目录 + .manifest.json
│   └── .keepalive               # 保活时间戳（防 60 天自动停用 schedule）
│
├── 【CI / 部署】
│   ├── .github/workflows/check.yml  # GitHub Actions：检测+持久化+测试+部署
│   ├── Dockerfile                  # 后端镜像（python:3.11-slim）
│   ├── docker-compose.yml          # 后端服务编排
│   └── DEPLOYMENT.md               # 部署指南
│
├── 【工具脚本】tools/
│   ├── import_json_to_db.py     # JSON 状态文件 → SQLite 迁移（幂等可重跑）
│   ├── migrate_history_types.py # history type 字段迁移
│   └── strip_status.py          # status.json 时间戳剥离（避免空推 commit）
│
├── 【测试】tests/                # pytest 回归套件（50+ 测试文件）
│
├── 【依赖】
│   ├── requirements.txt         # 运行时依赖（FastAPI/SQLAlchemy/APScheduler/playwright）
│   ├── requirements-dev.txt     # 开发/测试依赖（pytest/pyyaml）
│   └── pytest.ini               # testpaths = tests
│
├── 【文档】docs/                 # 项目自身设计文档（PRD/设计/mermaid 图）
│
└── README.md / DEPLOYMENT.md / qa_report_*.md
```

---

## 4. 模块职责详解

### 4.1 检测入口脚本（CI 形态）

#### [check_status.py](file:///workspace/check_status.py) — 直播状态检测主脚本
- **职责**：检测 B站 / 抖音 主播是否开播；状态变化时推送通知；更新 `state.json` / `status.json` / `history.json` / `tracking.json`。
- **核心流程**：`load_config()` → 遍历 rooms → `fetch_bilibili_batch` / `fetch_douyin` → 状态判定 → `should_push` → `dispatch_event` → 写状态文件 + history + notify_dedup。
- **被复用**：后端 `DetectionService.run_live` 调用其 `run_live_check(cfg_all, persist, now, adapters)` 纯编排函数（不改 `main()`）。
- **关键常量**：`BILIBILI_STATUS_MAP = {0: "offline", 1: "live", 2: "replay"}`、`DOUYIN_STATUS_LIVE = 2`、`DOUYIN_STATUS_OFFLINE = 4`。

#### [check_new_posts.py](file:///workspace/check_new_posts.py) — 抖音新作品检测
- **职责**：独立于直播监控，读 `post_rooms.json`，检测抖音新作品，写 `post_tracking.json`。
- **三层策略 + 优雅降级**：
  - 策略 0（免 Cookie 首选）：`m.douyin.com/web/api/v2/aweme/post/` 移动端老接口。
  - 策略 1（需 Cookie）：Playwright 拦截 `aweme/v1/web/aweme/post/` 已签名响应。
  - 策略 2（退化）：解析 `user/profile/other` 的 `aweme_count`，作品数增加才提示。
- **中毒防护**：`resolve_sec_uid` 只取房间主人 `anchor` 结构化字段；`looks_like_handle` 校验实际账号，被污染时清除 `sec_uid` 并跳过。
- **被复用**：后端 `DouyinAdapter.fetch_new_posts` 复用其 `resolve_sec_uid` / `get_latest_aweme` / `should_notify_new_post` / `should_update_baseline` / `looks_like_handle`。

#### [auto_summary.py](file:///workspace/auto_summary.py) — 定时摘要投递
- **职责**：按 `BLIVE_CONFIG.summary`（daily/weekly + sendTime）投递摘要；失败冷却 4h。
- **纯函数**：`compute_since(freq, now_bj)` / `compute_summary(hist, since)`（与前端 JS 逐字节一致，可单测）。
- **退出码**：一律 `exit 0`（非致命），配合 CI `continue-on-error`。

#### [transcode_covers.py](file:///workspace/transcode_covers.py) — 封面转存
- **职责**：把抖音新作封面从外部 CDN 转存到仓库 `assets/covers/`，改写 `latest_cover` 为 raw URL，规避防盗链破图。
- **差异提交**：基于 `.manifest.json` 判定，仅新作品才下载，避免仓库膨胀。

### 4.2 共享工具模块

#### [common.py](file:///workspace/common.py) — 公共工具
- `bjnow()` / `parse_beijing(s)`：北京时间工具（与前端 JS `parseBeijing` 逐字节一致）。
- `load_json_file` / `save_json_file`：原子 JSON 读写（先写 `.tmp` 再 `os.replace`）。
- `room_enabled` / `load_silence_cfg` / `should_skip_by_silence`：A3 静默时段。
- `resolve_channel` / `render_template`：A2 多通道路由 + A4 模板渲染（与 `push_utils.dispatch_event` 同源）。

#### [push_utils.py](file:///workspace/push_utils.py) — 多通道推送
- **5 个渠道**：`send_via_bark` / `send_via_serverchan` / `send_via_wecom` / `send_via_pushplus` / `send_via_telegram`，均返回 `SendResult`。
- **`dispatch_event`**：A2/A4 统一推送入口，按路由选通道 + 模板渲染 + 重试。
- **`send_with_retry`**：指数退避重试（默认 3 次，2s/4s/8s）。
- **`is_retryable(status_code, last_error)`**：5xx/429/网络→重试；4xx/业务拒绝/配置缺失→放弃。
- **`SendResult`** dataclass：`ok/attempts/last_error/status_code` 结构化结果。

#### [notify_dedup.py](file:///workspace/notify_dedup.py) — 通知去重账本（文件版）
- **第二道防线**：与状态持久化解耦，防 CI 状态丢失 / 抖音闪烁导致重复推送。
- **key 规则**：
  - 直播：`live:{platform}_{rid}`，冷却 `LIVE_COOLDOWN_SECONDS = 7200`（2h）。
  - 新作品：`post:{sec_uid}:{aweme_id}`，永久（`cooldown = math.inf`）。
- **裁剪**：`live:` key 超 7d 清理；总数超 5000 保留最近 N。

#### [log_utils.py](file:///workspace/log_utils.py) — 运行时日志 + history 统一模块
- `HISTORY_MAX = 500`（唯一来源，消除 200/500 不一致）。
- `init_runtime_logging()`：`RotatingFileHandler(logs/runtime.log, 5MB, 5 备份)` + 控制台。
- `EVENT_TYPES` / `STATUS_TO_TYPE` / `TYPE_TO_LEVEL`：统一事件模型（`live_on/live_off/new_post/error/cookie_warn/system`）。
- `append_history` / `dedupe_by_throttle`：error/cookie_warn 类 30min 窗口节流。

#### [merge_state.py](file:///workspace/merge_state.py) — CI 状态文件语义合并
- **解决问题**：CI `git pull --rebase` 在状态文件冲突时失败 → 状态丢失 → 重复推送。
- **语义合并规则**：
  - `notify_dedup.json`：并集，同 key 保留更早 ts（绝不丢去重记录）。
  - `post_tracking.json`：每账号取基线更新者（aweme_id 更大 / create_time 更新）。
  - `post_rooms.json`：并集（按 id 去重），sec_uid 取非空。
  - `state.json` / `tracking.json`：取本地（本 run 最新）。
  - `history.json`：并集（按 time+name 去重，保留最近 N）。

#### [state_prune.py](file:///workspace/state_prune.py) — 级联清理
- `prune_history_orphans(history, active_keys)`：保留 `f"{platform}|{rid}" ∈ active_keys` 的 history 条目。
- `prune_tracking_orphans(tracking, active_keys)`：删除 `key ∉ active_keys` 的孤儿。
- `merge_post_rooms_fields(config_file, resolved)`：重读磁盘，仅对仍存在的账号原地更新 sec_uid/name（替代内联补丁）。

### 4.3 后端模块（阶段四）

#### [backend/app.py](file:///workspace/backend/app.py) — FastAPI 入口
- `lifespan`：启动时 `db.init_db()` 建表；按 `START_SCHEDULER` 决定是否自启 Scheduler。
- 挂载 8 个 router 到 `/api/v1` 前缀；`GET /healthz` 鉴权豁免。
- API 前缀常量 `API_PREFIX = "/api/v1"`（显式，不受 env 影响）。

#### [backend/config.py](file:///workspace/backend/config.py) — 运行时配置
- 环境变量驱动：`DB_PATH` / `AUTH_TOKEN` / `ENABLE_POST_CHECK` / `TZ` / `LIVE_CHECK_INTERVAL_MIN` / `POST_CHECK_INTERVAL_MIN` / `MISFIRE_GRACE_SEC` / `COVERS_DIR` / `GITHUB_OWNER/REPO/BRANCH` / `API_PREFIX` / `PUBLIC_PATHS` / `LIVE_DEDUP_COOLDOWN_SEC`。

#### [backend/db.py](file:///workspace/backend/db.py) — SQLite 访问层
- `engine = create_engine("sqlite:///{DB_PATH}", check_same_thread=False, future=True)`。
- `SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)`。
- `Base = declarative_base()`。
- `WRITER_LOCK = threading.Lock()`：所有写操作必须持锁。
- `_apply_pragmas`：`PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON;`。
- `init_db()` / `get_db()`（FastAPI 依赖，session per request）。

#### [backend/models.py](file:///workspace/backend/models.py) — SQLAlchemy ORM（8 张表）
- 详见 [§6 数据模型](#6-数据模型db-表--json-文件)。

#### [backend/schemas.py](file:///workspace/backend/schemas.py) — Pydantic 模型
- `RoomBase/Create/Update/StatusUpdate/StatusOut/Out`、`PostCreate/Out`、`EventOut`、`NotifyLogIn/Out`、`DedupUpsert/QueryOut`、`SummaryStateOut`、`SilenceStateOut`、`HealthOut`、`JobTriggerOut`、`PagedRooms/Events/Posts`。
- 所有时间字段统一 `YYYY-MM-DD HH:MM:SS`（北京时间）。

#### [backend/config_store.py](file:///workspace/backend/config_store.py) — ConfigStore
- `get_config()` / `put_config(cfg)`：BLIVE_CONFIG 整段读写（key='blive_config'）。
- `get_push_cfg()` / `get_platform_cfg(platform)`：兼容 legacy 单通道 + 多平台适配器配置。
- `get_summary_state` / `put_summary_state` / `get_silence_state` / `put_silence_state`：摘要/静默状态读写。
- 所有写操作持 `WRITER_LOCK`。

#### backend/api/ — REST API 路由
| Router | 前缀 | 关键端点 |
|---|---|---|
| [rooms.py](file:///workspace/backend/api/rooms.py) | `/rooms` | `GET ""` 列表（kind/platform/enabled/q 过滤+分页）、`POST ""` 创建、`GET/PUT/DELETE /{id}`、`GET/PUT /{id}/status` |
| [posts.py](file:///workspace/backend/api/posts.py) | `/posts` | `GET ""` 列表、`POST ""` 记录 |
| [events.py](file:///workspace/backend/api/events.py) | `/events` | `GET ""` 历史查询（room_id/platform/event_type/frm/to + 分页） |
| [notify.py](file:///workspace/backend/api/notify.py) | `/notify` | `POST /log` 推送账本、`GET /dedup?key=` 查询、`POST /dedup` 标记 |
| [config_api.py](file:///workspace/backend/api/config_api.py) | `/config` | `GET ""` / `PUT ""` |
| [summary_api.py](file:///workspace/backend/api/summary_api.py) | `/summary` | `GET/PUT /state` |
| [silence_api.py](file:///workspace/backend/api/silence_api.py) | `/silence` | `GET/PUT /state` |
| [jobs_api.py](file:///workspace/backend/api/jobs_api.py) | `/jobs` | `POST /check?type=live\|post\|all` 手动触发、`GET /status` |

- **鉴权**（[deps.py](file:///workspace/backend/api/deps.py)）：`AUTH_TOKEN` 空=放行；非空校验 `X-Bearer-Token` 头；`/healthz` 与读接口豁免。

#### backend/core/ — 核心服务层
- [Persistence](file:///workspace/backend/core/persistence.py)：rooms/posts/events_history 的 DB CRUD。`set_room_status` 写状态列 + 合并 meta 基线；`append_event` 裸落盘；`list_rooms/count_rooms/list_events/count_events/list_posts/count_posts` 查询。
- [DedupService](file:///workspace/backend/core/dedup.py)：DB 版去重账本。`should_notify(key, cooldown)` / `record(key, meta)` / `last_sent_at(key)` / `prune()`。
- [HistoryStore](file:///workspace/backend/core/history_store.py)：含 30min 节流（error/cookie_warn 同 rid+type 不重复写）。`append_event(entry)` 节流命中返回 False。
- [NotifyLogStore](file:///workspace/backend/core/notify_log_store.py)：每次推送尝试落 notify_log。`log(...)` / `list_logs(...)`。

#### backend/jobs/ — 编排与调度
- [Scheduler](file:///workspace/backend/jobs/scheduler.py)：`AsyncIOScheduler` 封装。
  - `_live_job`：每 5min 跑 `detection.run_live` + 顺带 `run_summary`（惰性评估，§8.10）。
  - `_post_job`：每 10min 跑 `detection.run_post` + 顺带 `run_transcode`（仅 `ENABLE_POST_CHECK=true`）。
  - **重入保护**：`RUNNING_FLAGS` + `coalesce=True` + `misfire_grace_time=60`。
  - `trigger(type_)`：手动触发（P1），有 loop 用 `run_coroutine_threadsafe`，无 loop 起临时 loop。
- [registry.py](file:///workspace/backend/jobs/registry.py)：`set_scheduler` / `get_scheduler` 单例（解 API↔调度循环依赖）。
- [DetectionService](file:///workspace/backend/jobs/detection_service.py)：编排入口。`run_live` / `run_post` / `run_summary` / `run_transcode`，各延迟 import 既有模块，构造 Persist 门面 + AdapterRegistry 后调 `run_live_check` 等。
- [LivePersist](file:///workspace/backend/jobs/live_check.py) / [PostPersist](file:///workspace/backend/jobs/post_check.py) / [SummaryPersist](file:///workspace/backend/jobs/summary_job.py)：持久化门面，把 DB 写操作收敛到 Persistence/Dedup/History/NotifyLog，使 `check_status` 等与 SQLAlchemy 完全解耦（duck-typed 接口）。
- [transcode_job](file:///workspace/backend/jobs/transcode_job.py)：复用 `transcode_covers._raw_url/download_cover/_sha256`，读 `Room(kind='post').meta` + 落 `Post.cover`。

#### backend/adapters/ — 多平台适配器
- [PlatformAdapter](file:///workspace/backend/adapters/base.py)（抽象基类）：
  - 类常量：`platform` / `poll_interval` / `rate_limit` / `supports_live` / `supports_posts` / `needs_context`。
  - 抽象方法：`fetch_room_status(room_id) -> RoomModel` / `fetch_new_posts(author_or_room, since, baseline, context) -> List[PostModel]`。
  - 可选：`fetch_room_status_batch`（bilibili 批量优化）/ `apply_credentials`（注入 Cookie 到 Playwright）。
  - 归一化模型：`RoomModel`（live_status 为 BOOL）/ `PostModel`。
  - 异常：`AdapterError` / `AdapterSkip`（reason: no_sec_uid/poisoned）/ `AdapterGated`（风控/未登录）。
- [AdapterRegistry](file:///workspace/backend/adapters/registry.py)：`from_config(cfg_all)` 构建注册表。bilibili/douyin 常驻；其余 4 平台按 `config.platforms.<code>.enabled` 构建，单平台失败不阻断其他。
- **适配器实现**：
  | 适配器 | 平台 | supports_live | supports_posts | 说明 |
  |---|---|---|---|---|
  | [BilibiliAdapter](file:///workspace/backend/adapters/bilibili.py) | bilibili | ✅ | ❌ | 复用 `fetch_bilibili_batch`，批量接口；replay 状态透传 |
  | [DouyinAdapter](file:///workspace/backend/adapters/douyin.py) | douyin | ✅ | ✅ | 复用三层策略 + 中毒防护；Cookie 注入突破风控 |
  | [KuaishouAdapter](file:///workspace/backend/adapters/kuaishou.py) | kuaishou | ✅ | ✅ | live_api + SSR 降级；graphql 新作（需 did/cookie） |
  | [ChannelsAdapter](file:///workspace/backend/adapters/channels.py) | channels | ✅ | ✅ | 开放平台 / playwright 双模（需真实凭证接入） |
  | [XhsAdapter](file:///workspace/backend/adapters/xhs.py) | xhs | ❌ | ✅ | 仅笔记；签名 API 骨架（需 Cookie + x-s/x-t） |
  | [TaobaoLiveAdapter](file:///workspace/backend/adapters/taobao_live.py) | taobao_live | ✅ | ❌ | 仅直播；SSR 解析 `__INITIAL_STATE__` |

### 4.4 前端
- [monitor.html](file:///workspace/monitor.html)：主监控页（标签切换式，正式版）。直连 GitHub Contents API 读写 `rooms.json`（GET → 改 → 带 sha PUT，遇 409 重试）。Token 存 localStorage，不入仓库。
- [index.html](file:///workspace/index.html)：多监控页变体入口（dashboard / feed / hero）。
- [api/rooms.js](file:///workspace/api/rooms.js)：`RoomValidator` ES Module（`validateInput` / `key` / `dedupe`），平台枚举 `[bilibili, douyin]`。
- [libsodium.js](file:///workspace/libsodium.js)：前端加密库，配置加密写入 GitHub Secret。

---

## 5. 关键类与函数说明

### 5.1 后端核心类

#### `PlatformAdapter`（抽象基类，[base.py](file:///workspace/backend/adapters/base.py)）
```python
class PlatformAdapter(ABC):
    platform: str = ""            # 子类必须覆盖
    poll_interval: int = 300
    supports_live: bool = True
    supports_posts: bool = True
    needs_context: bool = False   # 是否需 Playwright

    @abstractmethod
    def fetch_room_status(self, room_id: str) -> RoomModel: ...
    @abstractmethod
    def fetch_new_posts(self, author_or_room, since, baseline, context) -> List[PostModel]: ...
    def fetch_room_status_batch(self, room_ids) -> Dict[str, RoomModel]: ...  # 可选
    def apply_credentials(self, context) -> None: ...  # 注入 Cookie 钩子
```

#### `AdapterRegistry`（[registry.py](file:///workspace/backend/adapters/registry.py)）
- `from_config(cfg_all)`：构建注册表（bilibili/douyin 常驻 + config.platforms 段启用者）。
- `register(adapter)` / `get(platform)` / `list_platforms()`。

#### `Scheduler`（[scheduler.py](file:///workspace/backend/jobs/scheduler.py)）
- `start()`：注册 live/post job 到 AsyncIOScheduler。
- `trigger(type_)`：手动触发一轮（live/post/all）。
- `shutdown()`。
- `_guarded(name, coro_fn)`：重入保护 + 异常隔离（单轮异常不打断调度器）。

#### `DetectionService`（[detection_service.py](file:///workspace/backend/jobs/detection_service.py)）
- `run_live(adapters=None)`：构造 `LivePersist` + `AdapterRegistry` → 调 `check_status.run_live_check`。
- `run_post(context=None, adapters=None)`：调 `check_new_posts.run_post_check`。
- `run_summary()`：调 `auto_summary.run_summary`。
- `run_transcode()`：调 `transcode_job.run`。

#### `Persistence`（[persistence.py](file:///workspace/backend/core/persistence.py)）
- Room：`list_rooms` / `count_rooms` / `get_room` / `get_room_by_key` / `upsert_room` / `update_room` / `delete_room`。
- 状态/基线：`get_room_status` / `get_tracking` / `set_room_status(*, platform, external_id, kind, name, result, meta_update, now_str, status_item)`。
- 事件：`append_event(entry)` / `list_events` / `count_events`。
- 作品：`upsert_post` / `get_post` / `list_posts` / `count_posts`。

#### `DedupService`（[dedup.py](file:///workspace/backend/core/dedup.py)）
- `should_notify(key, cooldown=LIVE_COOLDOWN_SECONDS, now=None) -> bool`。
- `record(key, now=None, meta=None)`（仅在推送成功后调）。
- `last_sent_at(key) -> float`。
- `prune(now=None)`：`live:` key 超 7d 清理；总数超 5000 保留最近 N。

#### `ConfigStore`（[config_store.py](file:///workspace/backend/config_store.py)）
- `get_config() -> dict`（缺失返回含空段的默认 dict）。
- `put_config(cfg) -> str`（返回 updated_at）。
- `get_push_cfg()` / `get_platform_cfg(platform)`。
- `get_summary_state` / `put_summary_state(value, remove=None)`。
- `get_silence_state` / `put_silence_state(value)`。

### 5.2 既有纯函数（被后端复用）

#### `check_status.run_live_check(*, cfg_all, persist, now=None, adapters=None)`
- 一轮直播检测编排：遍历 rooms → 适配器 fetch → 状态判定 → should_push → dispatch_event → 经 persist 落库。
- `persist` 为 duck-typed 门面（`LivePersist` 实现）。

#### `check_new_posts.run_post_check(*, cfg_all, persist, now=None, context=None, adapters=None)`
- 一轮新作检测编排，同上范式。

#### `auto_summary.run_summary(*, cfg_all, persist, now=None)`
- 摘要投递评估：gate（未启用/未到时间/已投/冷却中→exit）→ `compute_since` → `compute_summary` → `dispatch_push` → 状态回写。

#### `push_utils.dispatch_event(...)`
- A2/A4 统一推送入口：按路由选通道 + 模板渲染 + 重试，返回 `SendResult`。

#### `push_utils.SendResult`
```python
@dataclass
class SendResult:
    ok: bool
    attempts: int
    last_error: str
    status_code: Optional[int]
```

#### `push_utils.is_retryable(status_code, last_error) -> bool`
- 5xx/429/网络→True；4xx/业务拒绝/配置缺失→False。

#### `common.parse_beijing(s) -> Optional[int]`
- 北京时间字符串 → 真实 UTC 秒（与前端 JS `parseBeijing` 逐字节一致）。

### 5.3 异常类型
- `AdapterError`：适配器检测异常基类。
- `AdapterSkip(reason, detail)`：跳过该账号（reason ∈ `no_sec_uid` / `poisoned` / `playwright_required`）。
- `AdapterGated(detail)`：接口被风控/未登录，无真实数据（等价 `cookie_warn` 事件）。

---

## 6. 数据模型（DB 表 / JSON 文件）

### 6.1 SQLAlchemy ORM（8 张表，[models.py](file:///workspace/backend/models.py)）

| 表名 | 模型类 | 主键/唯一约束 | 职责 |
|---|---|---|---|
| `rooms` | `Room` | PK `id`；UQ `(platform, external_id, kind)` | 监控目标（直播/新作共用，`kind` 区分）。含状态列 + `meta` JSON 基线 |
| `posts` | `Post` | PK `id`；UQ `(platform, post_id)` | 新作列表（每条作品一行） |
| `events_history` | `EventHistory` | PK `id` | 统一历史事件（替代 history.json）。含 `occurred_at` 字符串 + `occurred_ts` epoch |
| `notify_log` | `NotifyLog` | PK `id` | 通知账本：每次推送尝试（成功/失败） |
| `notify_dedup` | `NotifyDedup` | PK `key` | 去重账本（替代 notify_dedup.json） |
| `config_kv` | `ConfigKV` | PK `key` | 通用 KV 配置；`blive_config` 存 BLIVE_CONFIG |
| `summary_state` | `SummaryState` | PK `key` | 摘要状态（替代 summary_state.json） |
| `silence_state` | `SilenceState` | PK `key` | 静默状态（替代 silence_state.json） |

#### `Room` 关键字段
- `kind` ∈ `{'live','post'}`：同一抖音号可同时被直播监控与新作监控。
- `meta`（JSON）：承载平台/维度专属运行时基线（live 基线 / post 基线）。
- 状态列：`live_status` / `current_title` / `online` / `area` / `cover` / `last_live_at` / `live_started_at` / `live_duration` / `last_checked_at`。
- `key` property：`{platform}_{external_id}`（等价原 JSON 键）。

#### `EventHistory` 关键字段
- `event_type` / `name` / `title` / `detail` / `level` / `changed` / `prev` / `push` / `payload`（JSON）。
- `occurred_at`（字符串，北京时间）+ `occurred_ts`（epoch，供范围查询）。
- `room_id` 外键 `rooms.id`（`ondelete=SET NULL`）。

### 6.2 JSON 状态文件（CI 形态）

| 文件 | 内容 | 写入者 |
|---|---|---|
| `rooms.json` | 直播监控房间列表 `[{platform, id, name, sec_uid?}]` | 前端直连 GitHub API |
| `post_rooms.json` | 抖音新作品监控账号列表 | 前端 + CI 回写 |
| `state.json` | 直播状态缓存（每房间 prev_status 等） | check_status.py |
| `status.json` | 当前状态快照（前端展示用，含 updated/time） | check_status.py |
| `tracking.json` | 直播追踪数据（last_live / live_start / live_duration） | check_status.py |
| `post_tracking.json` | 新作基线（latest_aweme_id / latest_ct / mode / sec_uid 等） | check_new_posts.py |
| `history.json` | 历史事件日志（≤500 条，含节流） | check_status.py + check_new_posts.py |
| `notify_dedup.json` | 通知去重账本 | check_status.py + check_new_posts.py |
| `summary_state.json` | 摘要投递状态（lastSent / 失败冷却） | auto_summary.py |
| `silence_state.json` | 静默时段状态 | 前端 |
| `assets/covers/.manifest.json` | 封面转存清单 `{id:{aweme_id, sha256}}` | transcode_covers.py |

---

## 7. 依赖关系

### 7.1 Python 依赖（[requirements.txt](file:///workspace/requirements.txt)）

| 包 | 版本 | 用途 |
|---|---|---|
| `playwright` | 1.58.0 | 抖音新作品检测（无头 Chromium，拦截已签名 XHR） |
| `fastapi` | 0.128.2 | 后端 Web 框架（sync def 路由 + 线程池） |
| `uvicorn` | — | ASGI 服务器 |
| `sqlalchemy` | 2.0.51 | ORM / DB 访问层（sync 2.0） |
| `apscheduler` | 3.11.3 | 后端自驱 scheduler（AsyncIOScheduler） |
| `httpx` | — | 推送/检测逻辑复用的 HTTP 客户端 |
| `python-multipart` | — | 备选表单/文件导入端点 |
| `pydantic` | — | 请求/响应模型 |

### 7.2 开发依赖（[requirements-dev.txt](file:///workspace/requirements-dev.txt)）
- `pytest>=8.0`、`pyyaml>=6.0`（test_refactor_edge 校验工作流文件用）。

### 7.3 模块依赖图（核心）

```
backend/app.py
  ├─ backend/config.py (环境变量)
  ├─ backend/db.py (engine/SessionLocal/WRITER_LOCK)
  ├─ backend/api/* (8 个 router)
  │    └─ backend/api/deps.py (require_auth + get_db_session)
  └─ backend/jobs/registry.py (get_scheduler)

backend/jobs/scheduler.py
  ├─ backend/jobs/detection_service.py
  │    ├─ backend/config_store.py
  │    ├─ backend/jobs/live_check.py (LivePersist)
  │    ├─ backend/jobs/post_check.py (PostPersist)
  │    ├─ backend/jobs/summary_job.py (SummaryPersist)
  │    ├─ backend/jobs/transcode_job.py
  │    ├─ check_status.run_live_check (复用)
  │    ├─ check_new_posts.run_post_check (复用)
  │    ├─ auto_summary.run_summary (复用)
  │    └─ backend/adapters/AdapterRegistry
  └─ apscheduler.AsyncIOScheduler

backend/adapters/*
  ├─ backend/adapters/base.py (PlatformAdapter + RoomModel/PostModel)
  ├─ check_status.fetch_* (bilibili/douyin 复用)
  ├─ check_new_posts.resolve_sec_uid/get_latest_aweme/... (douyin 复用)
  └─ transcode_covers._raw_url/download_cover (transcode_job 复用)

backend/core/*
  ├─ backend/core/persistence.py → backend/models (Room/Post/EventHistory)
  ├─ backend/core/dedup.py → backend/models (NotifyDedup)
  ├─ backend/core/history_store.py → persistence.append_event
  └─ backend/core/notify_log_store.py → backend/models (NotifyLog)

既有共享模块（CI + 后端双形态复用）：
  common.py ← check_status / check_new_posts / auto_summary / push_utils / log_utils / notify_dedup / merge_state / state_prune
  push_utils.py ← check_status / check_new_posts / auto_summary
  notify_dedup.py (文件版) ← check_status / check_new_posts
  log_utils.py ← check_status / check_new_posts / merge_state
```

### 7.4 外部服务依赖
- **B站**：`api.live.bilibili.com/xlive/web/room/v1/info/getRoomBaseInfo`（官方批量接口）。
- **抖音直播**：`live.douyin.com/{web_rid}` SSR 页面提取（多策略兜底）。
- **抖音新作**：`m.douyin.com/web/api/v2/aweme/post/`（移动端免 Cookie）+ `aweme/v1/web/aweme/post/`（Playwright 拦截）+ `user/profile/other`（count 退化）。
- **推送渠道**：Bark / Server酱 / 企业微信 / PushPlus / Telegram API。
- **GitHub**：Contents API（前端读写 rooms.json）、Actions（CI 形态）。

---

## 8. 项目运行方式

### 8.1 CI 形态（GitHub Actions，推荐生产部署）

```bash
# 1. Fork 仓库
# 2. Settings → Secrets → Actions 添加 BLIVE_CONFIG（JSON 推送配置）
# 3. Settings → Pages → Source 选 GitHub Actions
# 4. 工作流自动每 5 分钟检测并更新监控页面
```

工作流 [.github/workflows/check.yml](file:///workspace/.github/workflows/check.yml) 三个 job：
1. **check**：检测 + 持久化（语义合并 + git push）+ 构建 Pages。含 Playwright 缓存、15min 闸门的新作品检测/封面转存、auto_summary（continue-on-error）。
2. **test**：跑 `pytest -q`（不影响监控/部署，仅质量信号）。
3. **deploy**：部署到 GitHub Pages。

> GitHub Actions schedule 不稳定，建议配合外部 cron（如 cron-job.org）通过 `workflow_dispatch` 触发，形成双保险。

### 8.2 本地运行（CI 形态脚本）

```bash
# 配置推送（可选）
export BLIVE_CONFIG='{"push": {"type": "bark", "url": "https://api.day.app/你的KEY"}}'

./run.sh              # 检测一次直播状态（默认 once）
./run.sh posts        # 检测抖音新作品（需 ENABLE_POST_CHECK=true）
./run.sh all          # 两者都跑
./run.sh loop         # 持续监控（每 60 秒）
./run.sh help         # 帮助
```

或直接调用脚本：
```bash
python3 check_status.py          # 直播检测
ENABLE_POST_CHECK=true python3 check_new_posts.py   # 新作检测
python3 auto_summary.py          # 摘要投递
python3 transcode_covers.py --owner racheko-lab --repo blive-monitor --branch master --covers-dir assets/covers
```

### 8.3 后端形态（Docker，阶段四）

```bash
# 构建并启动（默认不自启调度器、无鉴权）
docker compose up -d

# 启用后端自驱检测 + 鉴权
AUTH_TOKEN=your-strong-token START_SCHEDULER=true docker compose up -d
```

[docker-compose.yml](file:///workspace/docker-compose.yml) 配置：
- 端口 `8000:8000`。
- 环境变量：`AUTH_TOKEN` / `START_SCHEDULER` / `TZ=Asia/Shanghai`。
- 挂载卷：`./data:/app/data`（SQLite 库）、`./assets/covers:/app/assets/covers`（封面）。
- CMD：`uvicorn backend.app:app --host 0.0.0.0 --port 8000`。

### 8.4 后端形态（本地开发）

```bash
pip install -r requirements.txt -r requirements-dev.txt

# 启动后端（自驱调度器）
START_SCHEDULER=true uvicorn backend.app:app --host 0.0.0.0 --port 8000

# 或仅启动 API（不自驱，用 /jobs/check 手动触发）
uvicorn backend.app:app --port 8000
```

### 8.5 从 CI 形态迁移到后端（JSON → SQLite）

```bash
# 幂等可重跑，建议在全新 blive.db 上运行
BLIVE_CONFIG='你的配置JSON' python tools/import_json_to_db.py --repo-root . --db data/blive.db
```

[tools/import_json_to_db.py](file:///workspace/tools/import_json_to_db.py) 读取 `rooms.json` / `status.json` / `state.json` / `tracking.json` / `post_rooms.json` / `post_tracking.json` / `history.json` / `notify_dedup.json` / `summary_state.json` / `silence_state.json`，按 PK/UNIQUE upsert；`events_history` 清空+重插保证重跑确定。

### 8.6 前端部署
- 监控页部署在 GitHub Pages（CI 形态自动构建 `_site/`）或 Netlify（拖拽整个文件夹）。
- 用户在页面「⚙️ 设置 Token」填入 GitHub Fine-grained PAT（仅 Contents 读写权限），存 localStorage。

---

## 9. 配置项与环境变量

### 9.1 后端环境变量（[backend/config.py](file:///workspace/backend/config.py)）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BLIVE_DB_PATH` | `DATA_DIR/blive.db` 或 `<repo>/data/blive.db` | SQLite 库路径 |
| `DATA_DIR` | `<repo>/data` | 数据目录 |
| `AUTH_TOKEN` | `""` | 写接口鉴权 token；空=放行（内网默认） |
| `ENABLE_POST_CHECK` | `True` | 是否启用新作品检测 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `LIVE_CHECK_INTERVAL_MIN` | `5` | 直播检测轮询间隔（分钟） |
| `POST_CHECK_INTERVAL_MIN` | `10` | 新作检测轮询间隔（分钟） |
| `MISFIRE_GRACE_SEC` | `60` | misfire 宽限（秒） |
| `START_SCHEDULER` | `false` | 是否自启 Scheduler（app lifespan 控制） |
| `BLIVE_COVERS_DIR` | `<repo>/assets/covers` | 封面转存目录 |
| `BLIVE_GITHUB_OWNER/REPO/BRANCH` | `racheko-lab`/`blive-monitor`/`master` | 封面 raw URL 构造 |
| `BLIVE_API_PREFIX` | `/api/v1` | API 前缀 |

### 9.2 CI 形态环境变量

| 变量 | 说明 |
|---|---|
| `BLIVE_CONFIG` | JSON 字符串推送配置（Secret）。结构：`{push, channels, routes, templates, silence, summary, platforms}` |
| `ENABLE_POST_CHECK` | `true`/`false`，是否启用新作品检测 |
| `DOUYIN_COOKIE` | 抖音登录 Cookie（Secret，突破作品接口风控） |

### 9.3 BLIVE_CONFIG 结构

```json
{
  "push": {                          // 兼容 legacy 单通道
    "type": "bark|wecom|serverchan|pushplus|telegram",
    "url": "...", "webhook": "...", "sendkey": "...", "token": "...", "chat": "...", "group": "..."
  },
  "channels": [...],                 // A2 多通道路由
  "routes": [...],                   // A2 路由规则
  "templates": {...},                // A4 模板
  "silence": {                       // A3 静默时段
    "enabled": false, "start": "23:00", "end": "08:00"
  },
  "summary": {                       // A1 摘要投递
    "enabled": false, "freq": "daily|weekly", "sendTime": "09:00"
  },
  "platforms": {                     // 阶段三多平台适配器配置
    "kuaishou": {"enabled": true, "credentials": {...}, "poll_interval": 300, "rate_limit": {...}},
    "channels": {...}, "xhs": {...}, "taobao_live": {...}
  }
}
```

合法顶层段（[config_store.py](file:///workspace/backend/config_store.py) `CONFIG_SECTIONS`）：`channels` / `routes` / `templates` / `silence` / `summary` / `push` / `platforms`。

---

## 10. 测试体系

### 10.1 测试配置
- [pytest.ini](file:///workspace/pytest.ini)：`testpaths = tests`（只收集根 `tests/`）。
- 运行：`python -m pytest -q`（CI 的 test job 自动执行）。

### 10.2 测试覆盖范围（50+ 测试文件）
| 类别 | 代表测试文件 |
|---|---|
| 适配器 | `test_adapters_base/bili_douyin/channels/kuaishou/taobao/xhs.py` |
| 检测编排 | `test_check_status.py` / `test_check_new_posts.py` / `test_detection_wiring.py` |
| 推送 | `test_push_utils.py` |
| 去重 | `test_notify_dedup.py` / `test_notify_reliability.py` |
| 日志 | `test_log_utils.py` / `test_log_functional.py` / `test_log_rewrite_strengthen.py` / `test_new_post_logging.py` / `test_frontend_log.py` |
| 状态合并/清理 | `test_merge_state.py` / `test_state_prune.py` / `test_migrate_history_types.py` |
| 阶段二功能 | `test_phase2_*.py`（A1 摘要 / A2 路由 / A3 静默 / A4 模板 / B1 批量 / B2 标签 / B3 启停 / B4 排序 / C1 时长 / C2 趋势 / C3 详情 / C4 导出 / D1 封面 / D2 回写） |
| 阶段四后端 | `test_phase4_api/migration/models/scheduler.py` / `test_dashboard.py` / `test_list_search.py` / `test_selfcheck.py` / `test_platform_position.py` |
| 前端 | `test_live_room_clickable.py` / `test_live_rooms_load.py` |
| CI 工作流 | `test_a2a4_ci.py` / `test_refactor_edge.py` |

### 10.3 测试约定
- 纯函数优先：`compute_since` / `compute_summary` / `should_notify_new_post` / `is_retryable` / `parse_beijing` 等均设计为可单测的纯函数。
- monkeypatch 网络：`fetch_bilibili_batch` / `fetch_douyin` / `_http_get` 等可被测试 monkeypatch。
- DB 隔离：测试通过 `BLIVE_DB_PATH` 指向临时库文件隔离。

---

## 附录：关键设计文档索引（docs/）

| 文档 | 内容 |
|---|---|
| [system_design.md](file:///workspace/docs/system_design.md) | 日志模块重构设计（运行时日志 + history + 级联清理） |
| [phase4_design.md](file:///workspace/docs/phase4_design.md) | 阶段四后端地基技术设计（FastAPI + SQLite + Scheduler） |
| [phase3_design.md](file:///workspace/docs/phase3_design.md) | 阶段三多平台适配器设计 |
| [phase34_prd.md](file:///workspace/docs/phase34_prd.md) | 阶段三/四产品需求 |
| [a1_summary_design.md](file:///workspace/docs/a1_summary_design.md) | A1 定时摘要投递设计 |
| [a2a4_ci_design.md](file:///workspace/docs/a2a4_ci_design.md) | A2 多通道路由 + A4 模板 CI 设计 |
| [p0_*.md](file:///workspace/docs/) | P0 系列：健康仪表盘 / 列表搜索 / 通知精化 / 通知可靠性 / 平台位置 / 自检 PRD+设计 |
| [blive-monitor-context.md](file:///workspace/docs/blive-monitor-context.md) | 项目交接文档（上下文迁移包） |
| [live-monitor-detection-landscape.md](file:///workspace/docs/live-monitor-detection-landscape.md) | 多平台直播检测方案调研 |
| [class-diagram.mermaid](file:///workspace/docs/class-diagram.mermaid) / [sequence-diagram.mermaid](file:///workspace/docs/sequence-diagram.mermaid) | 类图/时序图 |
| [DEPLOYMENT.md](file:///workspace/DEPLOYMENT.md) | 部署指南（前端直连 GitHub API 方案） |
