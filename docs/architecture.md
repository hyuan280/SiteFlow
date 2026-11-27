# 架构

SiteFlow 是面向站点自动化任务的管理系统，第一个业务场景是 PT 站点签到。设计目标：前后端分离、配置入库、功能块可编排、浏览器任务可观测、单机低依赖（SQLite）。

## 1. 组件

```text
Browser
  │ HTTP / WebSocket :8088
  ▼
web (Caddy)
  ├── /                         Vue 3 SPA
  └── /api/*                    reverse proxy
        │
        ▼
api (FastAPI)
  ├── 单管理员认证 / CSRF
  ├── 代理 / 2FAuth / CFbox 集成配置
  ├── 站点登录凭据（本地加密）
  ├── 车道状态 / VNC WebSocket 代理 / 浏览器许可证配置代理
  ├── 运行队列 / 批量车道 dispatch 注册与续期 / 站点任务 / 步骤结果
  └── SQLite (WAL)
        │ internal network
        ▼
worker (FastAPI + Playwright CDP client)
  ├── 4 lanes: general / interaction / proxy_general / proxy_interaction
  ├── BrowserBackend / BrowserSession / optional Desktop
  ├── mss / xdotool clients through dedicated X11 socket volume
  └── finite profile dispatch reservations / first-profile preflight / lease / viewer / idle lifecycle
        │ HTTP / CDP / VNC
        ▼
browser (official CloakBrowser Manager, keyless by default)
  └── persistent profiles + downloaded browser cache + Xvnc / KasmVNC + fonts
```

- 只有 `web` 发布宿主机端口 `8088`。
- `api:8000`、`worker:6300`、`browser:8080` 仅存在于 Compose 内部网络，KasmVNC 仅在 browser 容器内部。
- 不使用 host 网络。
- 前端在宿主机预构建，Docker 只把 `frontend/dist` 复制到 Caddy；运行和构建容器都不依赖 npm registry。

## 2. 关键约束

1. **单实例 worker**：车道 profile 映射及租约不允许并行 worker 抢占；浏览器进程由外部后端所有。
2. **SQLite 单写入者**：API 与 worker 不共享数据库；需要任务结果时通过 API 或独立 DB 层写入。
3. **按需启动**：车道默认停止；前端、任务 lease 或调度首个 profile 预检可启动，空闲后定时关闭，容量压力下可立即安全停止空闲自有 profile。
4. **所有权优先**：dispatch pin、活跃任务 lease、viewer、任务页及隔离清理阻止空闲/压力停机；release 后重新开始空闲计时。停机不删除 profile，保留 ID、Cookie 和数据。
5. **VNC 鉴权边界**：API 校验管理员会话和 Origin，Worker 使用独立令牌连接 Manager。Manager 处理 Kasm 协议适配，SiteFlow 不重复改写。
6. **配置优先级**：一般集成配置为环境变量 > SQLite > 默认值；密钥环境变量不写库，库内值使用 AES-256-GCM。浏览器许可证例外：环境变量或 Manager `/data` 保存，不在 SiteFlow SQLite/Worker 数据中重复保存。
7. **不做自动禁用/自动根因判断**：站点失败由人或后续任务策略处理。
8. **许可证与 profile 容量**：默认 Manager 使用 keyless 镜像；要求许可证的新版即使免费版也需要上游许可。Worker 的 `SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES` 是整数 `1..4`，默认 `1`；free 无论配置多高始终为 `1`，pro/未知容量无可靠 Manager 席位元数据时默认保守为 `1`，keyless 也默认 `1`。运维配置的有限预算不是许可证权益，未知许可 tier 仍拒绝启动。同车道多标签共享 profile，不同车道使用不同 profile；任务并发与 profile 容量独立。
9. **Worker dispatch 准入**：后端一次注册 Run 的所有初始非空车道，Worker 按 `general`、`interaction`、`proxy_general`、`proxy_interaction` 优先级非抢占式授予预留。获准后才创建执行任务并竞争全局任务槽，pending 不占全局任务槽。有效上限 `2` 可让同 Run 的两个不同车道重叠，free 上限 `1` 则不同车道顺序执行；其他 Run、助手、viewer 共用 profile 池。同车道并发跨 Run/助手共享：普通默认 `5`、代理普通默认 `3`（可配置 `1..10`），交互固定 `1`，短时输入锁不变。

## 3. 当前实现状态

已实现：

- 单管理员登录、会话、CSRF、登录限速。
- 代理 / 2FAuth / CFbox 配置：env 优先、DB 加密、掩码、连通性测试。
- 站点登录凭据本地 AES-256-GCM 加密；独立 API 管理，站点详情只返回掩码元数据。
- 四车道生命周期、有限 profile 队列、按需启动/关闭、lease/release、VNC viewer 计数、定时空闲/安全压力停机。
- API → Worker → Manager VNC WebSocket 代理、noVNC 前端。
- BrowserView 顶部 CloakBrowser Manager 许可证卡片：来源、只读/可编辑、是否配置、许可 tier 与二进制版本；不返回或显示现有密钥及服务商掩码，已配置不代表授权有效。
- 站点 / 模板 / 功能块模型：功能目录、模板继承、站点覆盖、配置 Schema 校验、解析预览。
- 单页管理界面：数据面板、车道状态、功能管理、模板管理、站点管理、系统设置。
- 功能 Schema 驱动字段表单：默认值、必填校验、数组增删、字段级继承/覆盖。
- 自定义页面信号与步骤执行条件；信号无法判断时升级人工接管。
- CFbox 截图识别集成：外部服务返回 CF 复选框坐标，供 xdotool 点击。
- 执行器基础版：运行记录、站点任务、步骤结果、条件/信号求值、取消与超时。
- Artifact：每个任务写详细执行日志，失败步骤保存截图；共享卷存储，数据库索引，运行详情查看详细日志。
- Worker dispatch 预留与车道批次并发、运行/任务队列限流、带首 profile 启动预检的 cron/间隔/随机时间范围调度器；Turnstile 使用 Playwright 点击并带 X11/xdotool fallback。
- 运行分析：状态矩阵、失败分析、趋势；基于原始运行数据实时聚合。
- Docker Compose 部署与无 host 网络运行。

- 工作流分组与排序、元素拾取器、通知、artifact 自动保留期清理。
- 插件运行时、能力审批、凭据提供商和安全审计。

首次安装通过 SQLAlchemy `create_all` 创建当前模型的表，管理员使用环境变量或安全的首次登录引导。功能和调度设置使用当前代码默认值；启动仅为已存在的 `sites` 表幂等补齐可信网址列（默认空数组），不进行通用历史升级、数据回填或工作流清洗。

### 调度与浏览器容量边界

生产调度在创建 Run、SiteJob 或步骤之前，使用令牌认证的 `POST worker:/browser/preflight`：只启动/复用规范顺序中的首个所需 profile，验证 CDP 及本地 `about:blank` 主页，其余所需车道只检查可排队资格。不访问业务站点或执行任何业务动作，首个验证成功之前不创建 Run；远端 HTTP 402、容量阻塞或预检失败会延后调度。多个所需车道的数量超过上限本身不构成失败，运行时由 dispatch 按可用容量准入。既有 `GET /browser/readiness` 仅为只读元数据，不启动 profile，也不是启动证明或生产调度门禁。

预检预热首个浏览器但不持有租约、不预留未来席位；后续仍可与其他 Run/助手/viewer 竞争，不保证所有后续车道、连接或业务成功。普通运行时租约单次最多等待容量 120 秒，持续续期的 pending dispatch ticket 可再次尝试容量获取，并非 dispatch 队列总时限；UI 手动启动受容量保护并快速失败。压力停机不等待空闲定时器，`SITEFLOW_LANE_IDLE_STOP_SECONDS=0` 只关闭定时停机，不固定占用 profile，viewer 则会固定占用。只停止安全空闲的 SiteFlow 自有 profile，不停止活跃租约（含 dispatch pin）/viewer/任务页或隔离所有权，不删除 profile；未确认清理仍占容量。

获准 ticket 的 pin 租约贯穿整个车道批次；跨车道 fallback 释放源任务的全局槽并排队，源批次结束后进入目标批次，目标批次已结束时可用新 ticket 重开，不扩大业务重试预算。ticket TTL 为 60 秒、后端每 1 秒续期，绑定 owner/Run/车道并校验 Worker generation 与固定 session；取消/收尾清理本 owner 预留，延迟续期不能复活已释放 ticket。过期只释放 dispatch pin，不释放普通任务/隔离清理租约或 viewer 保护；预留丢失或 generation 变化不自动重放。公开 `in_use` 包含 dispatch pin，不等于精确运行中任务数。

助手完整运行前仅关闭自己的探索浏览器会话并释放其租约，不关闭其他会话或任务。容量队列和预检都不授权在业务步骤执行后自动重放业务。两份 Compose 显式将 `SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES: "${SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES:-1}"` 映射给 Worker。详细时限、调度复核和安全所有权规则见[执行](execution.md#scheduled-browser-admission)及[浏览器后端](browser-backends.md#finite-profile-admission)；这些是工作区行为说明，不代表已部署或通过当前许可镜像的实际运行验证。

### 浏览器许可证配置边界

[许可证卡片](browser-backends.md#siteflow-license-card)通过管理员认证的 `GET /api/v1/lanes/browser-settings` 和 CSRF 保护的 `PUT` 进入 API，再经令牌认证的 Worker `/browser/settings` 访问 Manager。只读取 `/api/status`、`/api/settings` 的公开元数据，只转发 `license_key` 到 Manager `PUT /api/settings`：普通保存为非空新 key，独立清除按钮确认后发送精确空字符串，空白输入不等于清除。不转发其他设置；Manager 在 `/data`（`browser_data`）持久化，清除只移除 Manager 保存值，不删除环境变量或 wrapper 的 `license.key`。读取/保存/清除不创建、修改、启动、停止或删除 profile，不改变车道或 viewer 租约，也不启动浏览器或重跑任务；Manager 写入时可以执行授权检查及二进制下载。版本提示以 `146.0.7680.177.5` 为已知旧版 keyless 参考，不覆盖实际授权状态，也不保证任意旧版本可用。

两份 Compose 显式向 `api`、`worker`、`browser` 传入同一个 `CLOAKBROWSER_LICENSE_KEY: '${CLOAKBROWSER_LICENSE_KEY:-}'`，统一 shell/`.env` 插值。非空时 API 与 Worker 独立执行本地 ENV 锁，卡片只读；空/未设置时可使用 Manager 保存值并热更新。新增、修改或移除 ENV 均需等待任务/viewer 结束后重建三个服务，不能只重建 browser，`restart` 不加载新环境。自定义外部 Manager 必须在三者配置相同的 key ENV（或都留空）；服务商不能报告 key 的 ENV 来源，因此来源标签不是对远程 Manager 实际环境的检查。操作命令见[部署](deployment.md#配置许可证)。

热保存可能持续数分钟。已派发 PUT 的结果未知时，Worker 保留粘性写保护，后续 GET 刷新仍返回 `license.editable=false`；不自动重试。必须先确认 Manager 原写入/下载实际完成，再排空任务及 viewer 并重启 Worker 才能清除保护，不得为绕过活跃下载而重启。此恢复步骤不替代环境变更所需的三服务重建，也不保证给运行中的浏览器重新授权。缺少 settings API 的旧 Manager 在卡片中只读，原有 keyless 回退及实际运行待验证说明保留。

## 4. 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia + noVNC |
| 后端 | Python 3.12、FastAPI、SQLAlchemy 2、Pydantic v2、Argon2id |
| 数据库 | SQLite（WAL、单写入者） |
| worker | Playwright CDP、OCR、浏览器后端适配、mss/xdotool 客户端，无浏览器二进制 |
| browser | 官方 Manager，固定 digest、许可证验证及独立浏览器下载、上游字体/显示服务 |
| 部署 | Docker Compose、Caddy、命名卷 |

两份 Compose 默认固定官方 keyless Manager 镜像，`CLOAKBROWSER_BINARY_PATH` 未设置时指向镜像自带的 Chromium `146.0.7680.177.5`。`/root/.cloakbrowser` 不挂载，避免遮蔽自带二进制。覆盖为要求许可证的新版时，需要同时指定 `SITEFLOW_BROWSER_IMAGE` 和显式空的 `CLOAKBROWSER_BINARY_PATH`；下载使用 `browser_cache:/var/cache/cloakbrowser`。镜像 digest 不固定另行下载的浏览器，`CLOAKBROWSER_AUTO_UPDATE=false` 不跳过首次下载、许可证或版本检查。具体配置见[部署](deployment.md#manager-镜像与兼容边界)。

挂载目标移动复用同一个实际命名卷，无需复制、缓存迁移、独立新卷或删卷；两份 Compose 默认 project name 均为 `siteflow`，部署切换时须保留原 project name/卷名覆盖。已有缓存及隐藏标记、wrapper `license.key` 原样保留，缓存备份含密钥，文件权限应为 `0600`，不得记录 key。Manager/profile 数据仍在 `/data`，与 Worker 映射成对备份；缓存布局不解决 profile/数据降级兼容性。Manager 保存 key/数据及 wrapper 缓存 key 仍会影响 keyless 回退，卡片清除只移除 Manager 保存值，不能通过静默删除整个缓存处理。任务/viewer 结束后需重建 browser 才应用挂载及环境变更，操作见[部署](deployment.md#缓存挂载切换)；本次未执行部署、下载或运行验证。

Worker 继续固定 `cloakbrowser==0.6.3` / `playwright==1.64.0`，不要求 Manager 使用同一 wrapper 或精确二进制版本；适配器接受 `keyless` / `free` / `pro`，拒绝 `none` / 未知 tier。镜像切换、缓存及数据降级说明见 [Browser Backends](browser-backends.md)。

## 5. 相关文档

- 功能块模型：`docs/feature-model.md`
- 数据模型：`docs/data-model.md`
- 配置：`docs/configuration.md`
- 认证：`docs/authentication.md`
- 车道与 VNC：`docs/vnc.md`
- 部署：`docs/deployment.md`
- 验证结论：`docs/verification.md`
- 使用指南：`docs/usage.md`；后续方向见 README 的“规划”。
