# SiteFlow

Plugin deployment is externally owned: one plugin, one immutable container.
SiteFlow handles registration, compatibility, permissions, references and persisted
removal tombstones, not installation, runtime pip, hot reload, upgrades, rollback
or container management. Restoring removed integration requires fresh approval.

SiteFlow 是一个前后端分离的站点自动化管理系统，第一个业务场景是 **PT 站点签到**。它把站点配置、登录/签到/验证能力、浏览器车道和运行状态集中到一个可管理、可观测的界面中。

当前版本已经具备：

- 单管理员登录、会话、CSRF、登录限速与修改密码。
- 代理、2FAuth、CFbox、Bitwarden 集成配置：环境变量优先、数据库加密存储、掩码展示。
- 通知设置：运行结束后发送邮件或 HTTP 通知，支持自定义标题和正文布局；环境变量配置只读，启用开关仍可调整。
- ALTCHA 验证：直接定位并点击复选框，等待组件完成验证，不依赖视觉坐标。
- 图形验证码识别：每个步骤可选择 ddddocr、视觉模型或 NexusPHP GD 5 模板识别；支持字符集、长度和 ddddocr 置信度校验。识别不通过时刷新验证码或重启任务（最多 2 次），视觉模型和 NexusPHP GD 5 不会静默回退。
- 元素拾取器：选择器字段可从交互车道打开页面，在 VNC 中按 Alt/Option 点击元素自动生成 CSS 选择器。
- 登录来源：账户 / Cookie / Bitwarden / 动态插件同级标签；外部提供商使用互斥启用开关及完整域名、主域名或自定义匹配，不将插件密码写入手动账户。本地账户 AES-256-GCM 加密保存；外部凭据仅在登录步骤通过受限一次性租约读取。
- 2FA 提供商：内置 2FAuth 与已批准插件均使用「2FA 服务名（必须）」和「2FA 用户名（可选）」；用户名可区分同服务下的多个账户，秘密值不进入普通任务载荷。
- 任务执行器基础版：运行记录、站点任务、步骤结果、取消、超时与人工接管状态。
- 四条车道：普通通用、普通交互、代理通用、代理交互。
- 车道按需启动/关闭，任务 lease 自动启动，无人使用时按空闲时间自动关闭。
- 车道状态、noVNC 实时画面、进入/退出 VNC。
- 站点 / 模板 / 功能块模型：模板继承、站点覆盖、Schema 校验、解析预览。
- 单页管理界面：左侧菜单包含数据面板、运行分析、任务调度、车道状态、功能管理、模板管理、站点管理、通知设置、系统设置。
- 功能字段由后端 Schema 自动渲染，支持默认值、必填校验、继承/覆盖、数组增删。
- 可自定义页面信号（包含/排除文本或选择器），步骤条件引用信号；信号无法判断时人工接管。
- 条件跳转：按页面文字或选择器命中后跳转到任意步骤 key，或重新开始任务，并限制最大跳转次数防止死循环。
- Docker Compose 部署，SQLite 持久化，不使用 host 网络，只有 Web 端口对外。

> 站点/模板/功能块模型、任务执行器、调度器和登录凭据均已实现；剩余规划见 `docs/roadmap.md`。

## 快速开始

镜像部署只需 Docker + Docker Compose；根目录 `docker-compose.yml` 使用 GHCR 镜像，不在服务器构建。首次使用前需先完成对应镜像的发布。

```bash
cp .env.example .env
chmod 600 .env

# 生成加密主密钥，填入 SITEFLOW_MASTER_KEY
openssl rand -base64 32

# 拉取和启动发布镜像
docker compose pull
docker compose up -d
docker compose ps
```

打开：

```text
http://localhost:8088
```

### 首次登录

如果没有在 `.env` 配置 `SITEFLOW_ADMIN_PASSWORD`，且数据库中还没有改过密码的管理员，API 每次启动会生成随机 `admin` 密码并打印到日志：

```bash
docker compose logs api
```

首次登录后请在页面修改密码；修改后会写入 SQLite，之后不再生成随机密码。

也可以在 `.env` 中固定管理员：

```dotenv
SITEFLOW_ADMIN_USERNAME=admin
SITEFLOW_ADMIN_PASSWORD=CHANGE_ME
```

env 管理员优先级最高，且页面不允许修改密码。

### 源码构建与镜像发布

源码构建保留在 `ops/`，另需 Node.js 22+/npm、curl 和 sha256sum：

```bash
bash ops/build.sh --host  # 不需要 host 构建网络时省略 --host
docker compose -f ops/docker-compose.yml up -d
```

GitHub Actions 按组件标签独立构建，例如推送 `api-v1.0` 仅发布
`ghcr.io/hyuan280/siteflow-api:v1.0` 和 `:latest`。同理支持 `worker-v*`、
`web-v*`、`bitwarden-v*`；预发布版本不更新 `latest`。
版本固定、权限和 Worker 浏览器再分发要求见 [镜像发布](docs/releases.md)。

## 使用说明

### 系统设置

左侧菜单「系统设置」展示代理、2FAuth、CFbox、Bitwarden 的当前状态、来源和掩码，并可编辑、测试或清除：

- `env`：来自环境变量，只读
- `db`：来自页面/数据库，可修改
- `auto`：未显式配置时自动检测 compose 内的 `bitwarden:8090`
- `unset`：未配置

Bitwarden 使用独立容器包装 `bw` CLI，通过 HTTP 提供健康检查和登录条目查询。默认自动检测 `http://bitwarden:8090`；可在系统设置中填写外部服务地址和 Token，或通过 `SITEFLOW_BITWARDEN_URL` / `SITEFLOW_BITWARDEN_TOKEN` 覆盖。站点凭据页在集成启用后增加「Bitwarden」标签，支持完整域名、主域名和自定义匹配。

CFbox 用于识别 Cloudflare 验证截图中复选框的位置，再通过 xdotool 点击。未配置 CFbox 时，`challenge.turnstile` 功能块不能保存/使用。CFbox 服务端未启用鉴权时，API Key 可以留空。

环境变量：

```dotenv
SITEFLOW_MASTER_KEY=CHANGE_ME    # 必需，AES-256-GCM 主密钥
SITEFLOW_PROXY_URL=socks5://user:pass@proxy.example.com:1080  # 配置后启用代理车道
SITEFLOW_TWOFAUTH_URL=https://2fa.example.com  # 2FAuth 地址
SITEFLOW_TWOFAUTH_TOKEN=CHANGE_ME  # 2FAuth Token
SITEFLOW_CFBOX_URL=http://cfbox:8000  # 可选，CF 验证截图识别服务
SITEFLOW_CFBOX_API_KEY=CHANGE_ME     # 可选，服务端启用鉴权时填写
BW_CLIENTID=CHANGE_ME                # Bitwarden 包装容器：API Key Client ID
BW_CLIENTSECRET=CHANGE_ME            # Bitwarden 包装容器：API Key Secret
BW_PASSWORD=CHANGE_ME                # Bitwarden 包装容器：主密码
BW_API_TOKEN=CHANGE_ME               # 可选，包装容器 HTTP Bearer Token
SITEFLOW_COOKIE_SECURE=false     # HTTP 本地用 false；HTTPS 用 true
SITEFLOW_LANE_IDLE_STOP_SECONDS=300  # 可选，单元格空闲自动关闭
```

### 通知设置

左侧菜单「通知设置」位于「系统设置」上方：

- 页面最上方配置所有通知共用的标题/正文布局，支持 `{{run_id}}`、`{{status_label}}`、`{{sites}}` 等变量和纯文本/HTML 格式。
- 邮件通知和 HTTP 通知各用一张卡片配置；支持环境中配置、页面配置和发送测试。
- 环境变量提供的字段只读，但启用开关始终可修改。
- 每个渠道可独立筛选：接收成功通知或只接收失败通知；接受手动运行结果或只接受自动调度运行结果。
- 每次运行进入最终状态后发送一次；发送失败只记录日志，不影响任务结果。

完整字段、占位符、HTTP 请求体和环境变量见 [通知设置](docs/notifications.md)。

### 功能管理

读取后端注册的功能块目录，展示分类、单例/多例、必填字段、默认值和能力依赖，并可直接查看字段 Schema。功能块由代码定义，页面不修改代码。

### 模板管理

- 新建或编辑模板时，配置流程直接显示在当前页面，不使用弹窗。
- 步骤的「启用」选项：继承父级状态、强制启用、或保留配置但禁用执行。
- 可选择父模板；子模板按 `step_key` 继承步骤。
- 步骤按分组展示并按位置顺序执行；模板步骤顺序固定，新增步骤在所属分组内上下移动。
- 每个可编辑步骤提供「代码配置」：粘贴 JSON 可一次修改名称、功能类型、分组、启停、条件、错误策略、超时和功能字段；应用前校验 JSON 语法、字段名和必填配置。
- 步骤 key 由程序生成，模板使用 `temp_xxxxxx`，站点使用 `site_xxxxxx`；页面不提供编辑入口。
- 模板列表支持勾选导入/导出模板配置（全选或多选），JSON 包含分组、步骤、信号和父模板引用；导入跳过同名模板，名称冲突和父模板缺失会在导入前提示。
- 模板导入的步骤会展开显示并标注来源模板；可点击「修改步骤」生成同 key 的子步骤卡片。
- 子步骤按模板继承顺序排列，当前配置修改显示在最后，用于合并覆盖。
- 功能步骤上方可配置「信号识别」；没有信号时执行条件只有「总是执行」。
- 被步骤条件引用的信号不能删除，但可以修改判断规则；信号无法判断时升级为人工接管。
- 选择器字段支持多个候选，按顺序尝试，第一个命中的元素生效；每行提供「拾取」按钮，可从页面直接取选择器。
- 元素拾取使用交互车道和 VNC：打开页面后可正常点击/登录，按住 Alt/Option 点击目标元素完成拾取；拾取会话 15 分钟自动过期。
- 添加步骤后选择功能类型，页面根据 Schema 自动生成字段：
  - 字符串、枚举、数字、布尔开关；
  - 功能字段每行最多 2 个，窄屏自动变为 1 列；
  - 数组字段可增删元素；
  - 键值对象字段可增删键值；
  - 必填字段显示红色星号，保存前校验。
- 父模板字段显示「继承」；修改后自动变为「覆盖」，可恢复继承。
- 列表可查看解析后的最终步骤、配置来源和车道。

### 站点管理

- 新增/编辑站点同样在当前页面展开配置流程。
- 选择模板后，模板步骤按模板分组展开并标注来源；模板分组不可修改。
- 站点使用「优先交互」和「使用代理」两个开关选择浏览器车道。
- 点击模板步骤上的「修改步骤」生成同 key 子卡片；当前站点修改显示在最后。
- 站点可以在模板分组之间新建本地分组并自定义名称；模板分组内部不能插入站点步骤。
- 步骤 key 自动生成（`site_xxxxxx`），不能手改。
- 站点字段默认显示模板继承值，编辑后成为站点覆盖值。
- 站点支持「支持补签」开关和「支持补签最大天数」（默认 30 天）；系统信号「断签」在模板和站点中始终可用，存在待补签日期时为真，站点不支持补签时恒为假。
- 购买补签卡和补签都使用站点配置的最大天数，不再在功能块里重复配置历史天数、购买数和补签数。
- 站点编辑页提供「登录凭据」卡片：用户名/密码独立保存，密码不回显；首次配置必须同时填写，之后可单独替换。
- 支持直接粘贴浏览器 Cookie 请求头或导出的 JSON 导入 Cookie；Cookie 加密保存，不导出、不回显。
- 保存时校验未知功能、必填字段、单例重复、模板循环和车道策略。
- 站点解析时替换 `{base_url}`；保存后供任务执行使用。
- 站点列表支持直接执行；执行后跳转到运行详情。
- 站点列表支持勾选导入/导出站点配置（全选或多选），JSON 包含站点字段、分组、步骤和信号；登录凭据不会导出，导入跳过同名站点。

### 运行分析

左侧菜单「运行分析」：

- 状态矩阵按站点 × 日期展示成功、失败、等待人工等状态，可点击进入运行详情。
- 趋势图展示每日状态数量和成功率。
- 失败分析按站点 + 日期只展示当天最后一次未成功的运行，支持按站点、状态、功能类型和消息搜索，可直接打开运行详情或失败截图。
- 时间范围支持 7/14/30/90 天。

### 任务调度

左侧菜单「任务调度」：

- 调度设置（关闭 / 定时 / 间隔 / 随机四种模式）位于「系统设置」；环境变量存在时只读。
- 任务调度页顶部显示下次自动运行时间；关闭自动调度时显示启用提示。
- 可选择站点执行、执行全部启用且有效的站点，或执行今日尚未成功且当前未在排队/运行的站点（按调度时区判断）。
- 每个站点一个任务，每个步骤显示状态、消息和页面地址；签到步骤输出成功布尔值和签到信息。
- 每个任务生成详细执行日志；运行详情可在「详细日志 / 状态日志」之间切换。步骤进入 `fail`/`error` 时会归档失败截图，并在站点标题行提供「查看失败截图」预览按钮。
- 日志和截图按保留期自动清理；系统设置中显示数量、空间占用，并支持立即清理。
- 运行列表 5 秒自动刷新；侧边运行详情通过 SSE 实时推送步骤进度，无需等待任务结束。
- 多站点运行时，详情顶部按站点显示子标签，可直接切换查看各站点进度；运行中可取消，当前步骤结束后停止。
- 不同车道可并发执行：普通默认 5 个、代理普通默认 3 个、交互车道固定 1 个；运行数和全局任务数可限流。
- 站点使用 `checkin_after` 作为当天最早可执行时间；系统调度器支持定时、间隔和随机时间范围三种模式。
- 每个站点任务使用独立标签页，完成后关闭；车道保留常驻欢迎页，避免关闭最后一个标签页导致浏览器退出。
- 运行详情中的车道名可点击进入「任务对照」：左侧约 1/3 显示站点状态步骤，右侧嵌入 VNC；运行中会自动定位到该任务标签页，也可手动点击「定位任务标签页」。
- 打开站点支持失败重试（默认 5 次，遇到网络错误时重试）；第一阶段支持登录、两步验证、Turnstile、滑块、图形验证码识别、导航、点击、填写、输出结果并结束、按钮签到、iframe 签到、UNIT3D Claim、补签卡购买和 NexusPHP 日历补签。

### 车道状态

左侧菜单「车道状态」：

- 初始所有车道显示为「并行/串行 - 已停止」。
- 点击「启动浏览器」启动对应 Xvnc + CloakBrowser；运行后显示「并行/串行 - 运行中 · 显示号:120 · N 正在使用」。
- 页面每 10 秒自动刷新车道状态。
- 点击「进入 VNC」打开实时画面；「退出 VNC」只断开画面，不停止车道。
- 点击「关闭浏览器」停止并释放资源。
- 任务运行中（lease）会显示「禁止关闭浏览器」。
- 无人使用（无任务 lease、无 VNC 连接）超过空闲时间后自动关闭。
- 车道设置卡片位于车道状态下方，可设置空闲关闭时间、普通车道并行任务数、代理车道并行任务数。
- 并行任务数范围 1–10；环境变量存在时对应字段只读并标注「环境变量」来源。

### 账户菜单

右上角显示当前管理员。鼠标悬停可修改密码或退出登录；密码由环境变量管理时修改密码不可用。

### 常用命令

```bash
# 日志
docker compose -f ops/docker-compose.yml logs -f api
docker compose -f ops/docker-compose.yml logs -f worker

# 重启
docker compose -f ops/docker-compose.yml restart

# 停止（保留数据）
docker compose -f ops/docker-compose.yml down
```

## 配置与安全

- `.env` 只保存在本机，已被 `.gitignore` 排除，权限保持 `600`。
- 不要把真实代理密码、2FAuth Token、主密钥提交到 Git。
- 记下 `SITEFLOW_MASTER_KEY` 并单独备份；丢失后数据库中的加密配置和站点登录凭据不可恢复。
- 站点用户名/密码只在任务执行时解密；API、解析预览和日志都不返回明文。
- 未修改初始密码前，`docker logs` 中会出现明文 bootstrap 密码，请限制日志访问权限并尽快改密。
- 生产 HTTPS 必须设置 `SITEFLOW_COOKIE_SECURE=true`。
- KasmVNC/worker 端口只在 Compose 内部网络可见，只有 `web:8088` 对外。

## API

交互式 API 文档：

```text
http://localhost:8088/api/docs
```

站点、模板、功能目录接口位于 `/api/v1/sites`、`/api/v1/templates`、`/api/v1/features`；只需要站点选择信息时使用 `/api/v1/sites?view=options`，需要有效性时加 `include_valid=true`。站点凭据使用 `/api/v1/sites/{id}/credential`，任务执行使用 `/api/v1/runs`。

## 文档

开发与设计文档：

- [架构](docs/architecture.md)
- [功能块与模板模型](docs/feature-model.md)
- [数据模型](docs/data-model.md)
- [配置说明](docs/configuration.md)
- [任务执行](docs/execution.md)
- [插件系统设计](docs/plugin-system.md)
- [通知设置](docs/notifications.md)
- [管理员认证](docs/authentication.md)
- [车道与 noVNC](docs/vnc.md)
- [部署与运维](docs/deployment.md)
- [技术验证结论](docs/verification.md)
- [路线图](docs/roadmap.md)
- [后端开发说明](backend/README.md)

## 目录结构

```text
backend/     FastAPI + SQLite API、认证、集成配置、站点/模板、车道代理
worker/      CloakBrowser / Xvnc / KasmVNC 车道生命周期
frontend/    Vue 3 + Element Plus + noVNC、单页管理界面
ops/         Dockerfile、Caddy、Compose、构建脚本
docs/        架构、设计、配置、部署、验证、路线图
data/        运行时数据（不跟踪）
```
# Cancellation Fault Verification

Disposable real backend/worker/gRPC/browser fault verification is documented in
[`plugins/examples/demo/FAULTS.md`](plugins/examples/demo/FAULTS.md). Page ownership
and cancellation limits are described in [`docs/execution.md`](docs/execution.md).
