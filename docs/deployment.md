# 部署与运维

## 1. 前置条件

- Docker + Docker Compose。
- Compose 默认使用自带 Chromium 的官方 CloakBrowser Manager keyless 镜像，新部署无需许可证。覆盖为新版 Manager 时，免费版也需要上游签发的许可证，并需能完成首次浏览器下载及许可证检查。
- GHCR 镜像部署无需 Node.js；源码构建另需 Node.js 22/npm。
- 可选：可达的 SOCKS5/HTTP(S) 代理、2FAuth 服务、Bitwarden 自托管保险库。
- 图形验证码的 ddddocr 和 NexusPHP GD 5 模板识别已内置在 worker 镜像中，无需额外服务或配置；视觉模型方式需要配置大模型。
- 元素拾取器使用交互车道 + VNC；拾取会话默认 15 分钟过期，可用 `SITEFLOW_PICKER_TTL_SECONDS` 调整。
- 运行详情 SSE 依赖反向代理不缓冲：Caddy 已配置 `flush_interval -1`；换成其他代理时需关闭对应路径的响应缓冲。

## 2. 配置

```bash
cp .env.example .env
chmod 600 .env
openssl rand -base64 32   # 填入 SITEFLOW_MASTER_KEY
openssl rand -hex 32     # 填入 SITEFLOW_BROWSER_TOKEN（不是许可证密钥）
```

最少配置（以下选择环境变量管理许可证；也可留空并在启动后通过 [SiteFlow 许可证卡片](browser-backends.md#siteflow-license-card)保存到 Manager）：

```dotenv
SITEFLOW_MASTER_KEY=<base64 32 bytes>
SITEFLOW_BROWSER_TOKEN=<random token>
CLOAKBROWSER_LICENSE_KEY=<upstream-issued free or paid license key>
```

可选：

```dotenv
SITEFLOW_PROXY_URL=socks5://user:pass@host:port
SITEFLOW_TWOFAUTH_URL=https://2fa.example.com
SITEFLOW_TWOFAUTH_TOKEN=CHANGE_ME
# Bitwarden 包装容器（compose 中的 bitwarden 服务）
BW_SERVER=https://vault.example.com
BW_CLIENTID=
BW_CLIENTSECRET=
BW_PASSWORD=
BW_API_TOKEN=
# SITEFLOW_ADMIN_USERNAME=admin
# SITEFLOW_ADMIN_PASSWORD=
SITEFLOW_COOKIE_SECURE=false
# SITEFLOW_LANE_IDLE_STOP_SECONDS=300
SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES=1
```

- 一般集成配置的环境变量优先于数据库；浏览器许可证由环境变量或 Manager `/data` 管理，不在 SiteFlow 数据库重复保存。
- `SITEFLOW_BROWSER_TOKEN` 是内部 HTTP/CDP/VNC 鉴权令牌，不能替代 `CLOAKBROWSER_LICENSE_KEY`；许可证只在私有配置中提供，不能提交到仓库。
- `.env` 已被 `.gitignore` 排除，必须保持 600。
- 首次启动自动按当前模型创建数据库表并提供默认设置。现有部署会自动补齐 `sites.trusted_urls_json` 列，旧站点默认为空数组；更新前备份数据库，避免多个 API 进程并发更新结构。不提供其他旧开发版本的通用数据库升级或移植保证。

## 3. 构建与启动

根目录使用发布镜像，无需本地构建：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

镜像版本、GHCR 访问权限和组件标签发布见 [镜像发布](releases.md)。

根目录主 Compose 默认不挂载 Docker Socket。需要可选的 SiteFlow 项目容器资源拆分时，先更新到支持此功能的 API 镜像，再使用 `docker compose -f docker-compose.yml -f docker-compose.resources.yml up -d`，并在系统设置中开启「显示系统资源占用」。挂载具有高权限风险，启用、项目名匹配、统计口径与撤销步骤见 [系统资源监控](system-resources.md)；不启用挂载时只显示 host 指标。

源码构建与启动仍使用 `ops/`：

```bash
./ops/build.sh
docker compose -f ops/docker-compose.yml up -d
docker compose -f ops/docker-compose.yml ps
```

访问 `http://localhost:8088`。

`ops/build.sh` 在宿主机执行 `npm ci && npm run build`，再直接使用 `docker build` 构建并加载四个 SiteFlow 镜像。浏览器官方镜像由 Compose 直接拉取，不在项目内构建。默认使用标准构建网络；需要 host 构建网络时运行 `bash ops/build.sh --host`，脚本同时传入 `--network=host` 和 `--allow=network.host`，也不改变容器运行网络。需要支持 Buildx/BuildKit 的 Docker。Python 包默认使用清华 PyPI 源，npm 使用公共 registry。

### Manager 镜像与兼容边界

两份 Compose 默认固定为 `cloakhq/cloakbrowser-manager@sha256:04b82e42fe74ec34352580da47b096695e6a8d08745f88e3f5fd821be5ebf989`，使用随镜像附带的 keyless Chromium `146.0.7680.177.5`。不设置 `SITEFLOW_BROWSER_IMAGE` 和 `CLOAKBROWSER_BINARY_PATH` 即可使用这组默认值。`/root/.cloakbrowser` 不挂载卷，以保留镜像自带的二进制。

#### 使用其他 Manager 镜像

如需使用不附带浏览器、要求许可证的新版 Manager，请同时覆盖镜像和二进制路径，例如：

```dotenv
SITEFLOW_BROWSER_IMAGE=cloakhq/cloakbrowser-manager@sha256:d3d9b86cfd94347f7356811cf8222c8bc48152725b053dc6ce2e68f541e00c14
CLOAKBROWSER_BINARY_PATH=
```

显式空值让新版使用 `browser_cache:/var/cache/cloakbrowser`，再按[配置许可证](#配置许可证)填写上游签发的许可证。若所选镜像自带浏览器，则将该路径设为镜像实际提供的路径。镜像 digest 不固定单独下载的浏览器；`CLOAKBROWSER_AUTO_UPDATE=false` 不阻止首次下载或许可证检查。健康检查提供 10 分钟启动宽限期；Manager 健康不代表 profile 已可启动。

已有部署切换镜像前，先等待任务和 viewer 结束并备份 `browser_data` 与 `worker_data`。使用当前部署的 Compose 文件执行 `docker compose pull browser`；本地构建部署加上 `-f ops/docker-compose.yml`。如果同时设置了许可证环境变量，按[配置许可证](#配置许可证)中的命令重建服务；仅调整镜像和二进制路径时，重建 `browser` 即可。

Worker 保留 `cloakbrowser==0.6.3` / `playwright==1.64.0`，不按精确二进制版本拦截；接受 `keyless` / `free` / `pro`，静态拒绝 `none` 或未知 tier。启动时 HTTP 402 按 profile 容量拒绝处理，HTTP 403 按许可证拒绝处理，均使用静态提示，不透传服务商响应正文。

免费版只允许一个并发运行的 profile，SiteFlow 通过有限容量队列自动安全切换车道，无需每次手动停止其他车道。同一车道的多个任务标签页共享 profile，不额外占席位；不同车道使用不同 profile，因此任务并发与 profile 容量是两个独立限制。

### 缓存挂载切换

这次只移动容器内挂载目标，必须复用**同一个实际命名卷**。两份 Compose 默认 project name 都是 `siteflow`，对应 `siteflow_browser_cache`；已有部署若使用 `-p`、`COMPOSE_PROJECT_NAME` 或卷名覆盖，继续保留原值。换 Compose 文件也不能意外换 project name 或实际卷名。卷内内容保持不变，包括隐藏标记与 `license.key`；不需要复制缓存、目录迁移、独立新卷或删除旧卷。profile/Manager 数据仍在 `browser_data:/data`，Worker 映射仍在 `worker_data`，Worker 不包含浏览器二进制。

1. 等待真实任务及 viewer 结束，成对备份 `browser_data` 与 `worker_data`，另行保留 `browser_cache`。Manager/cache 备份可能含许可证密钥，备份文件权限设为 `0600`，不要将 key 写入日志或共享诊断。
2. 同时应用新的缓存环境变量与挂载目标，删除或调整仍挂载 `/root/.cloakbrowser` 的旧 Compose override；即使已有新缓存挂载，残留的根目录挂载仍会遮蔽旧 bundled binary。保持原部署文件、project name 与实际卷名。
3. 按实际部署方式二选一重建 browser；仅 `restart` 不会加载挂载及环境变更。已有 project name 覆盖继续用于以下命令：

```bash
# GHCR 部署，仅缓存挂载变更
docker compose up -d --no-deps --force-recreate browser
# 源码部署，二选一
docker compose -f ops/docker-compose.yml up -d --no-deps --force-recreate browser
```

仅缓存挂载变更不要求重建 API/Worker；若同时修改许可证 ENV，仍须按下文重建三个服务。以上仅为操作说明，本次未执行部署、下载或运行验证。

### 有限容量与调度预检

Worker 的 `SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES` 只接受整数 `1..4`，默认 `1`，不存在无限模式。免费版即使配置更高也始终限制为 `1`。Manager 没有可靠的 pro/未知容量席位元数据，默认保守为 `1`；旧 keyless 同样默认 `1`。运维人员配置更高有限值只是本地资源预算，不代表许可证赋予该席位数，也不绕过未知许可 tier 的拒绝。两份 Compose 均显式向 Worker 映射 `SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES: "${SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES:-1}"`，统一 shell/`.env` 来源。

后端一次批量注册 Run 的所有初始非空车道，Worker 按 `general`、`interaction`、`proxy_general`、`proxy_interaction` 优先级非抢占式授予 dispatch 预留；获准后才创建该车道批次的执行任务并竞争全局任务槽，pending ticket 不占全局任务槽。有效上限为 `2` 时，同一个 Run 的两个不同车道可以重叠执行；免费版上限 `1` 时不同车道仍顺序执行。其他 Run、助手和 viewer 共用同一 profile 池。同车道任务并发跨 Run/助手共享：普通默认 `5`、代理普通默认 `3`（均可配置 `1..10`），交互固定 `1`；短时前台输入锁仍在，不锁住整个任务。所需车道数量大于容量本身不是预检失败原因。

获准 ticket 的 pin 租约保留到整个车道批次结束，包括等待全局槽的任务。跨车道 fallback 先释放源任务的全局槽并排队，源批次结束后进入目标批次；目标先前已完成时可以用新 ticket 重开，不扩大既有业务重试/fallback 预算。ticket TTL 为 **60 秒**，后端每 **1 秒**续期；绑定 owner、Run、车道，并校验 Worker generation 和固定的 session。取消/收尾只清理本 owner 的预留，延迟续期不能复活已释放 ticket；过期只释放 dispatch pin，不释放普通任务租约、隔离清理所有权或 viewer 保护。generation 变化或预留失效时保守失败，不自动重放。公开 `in_use` 包含 dispatch pin 和任务/清理租约，不是精确的运行中任务数。

普通运行时任务租约单次等待容量最多 **120 秒**；pending dispatch ticket 在持续续期时可再次尝试容量获取，这不是 dispatch 队列总时限。容量压力下，Worker 可以立即停止空闲且属于 SiteFlow 的 profile，不必等空闲定时器，即使 `SITEFLOW_LANE_IDLE_STOP_SECONDS=0` 也如此。只停止、不删除，复用原 profile ID、Cookie 和持久化数据；不会停止有活跃租约（含 dispatch pin）、viewer、任务页或隔离清理所有权的 profile，未确认停止/清理仍占容量。关闭空闲定时停机不代表常驻锁定，打开 viewer 才会固定占用该 profile。UI 手动启动车道受同一容量保护，但快速失败，不像任务租约等待席位。助手完整运行前只关闭自己的探索浏览器会话并释放租约，不能关闭其他会话或任务抢占容量。

自动调度在创建任何 Run、SiteJob、步骤记录之前，调用令牌认证的 `POST worker:/browser/preflight`，仅启动或复用规范顺序中的首个所需 profile，验证 CDP 与本地 `about:blank` 主页；其余车道只检查可排队资格。预检不访问业务站点、不登录、不点击、不执行工作流。远端 HTTP 402、容量阻塞或预检失败时延后调度，不创建 Run。Worker 预检外层预算为 120 秒，但席位不足时快速失败，不等待活跃用户排空；运行时 120 秒席位等待是另一个机制。

原 `GET /browser/readiness` 仍仅为只读元数据，不启动浏览器，也不是启动成功证明或生产调度门禁。预检只预热首个浏览器，不持有租约或预留容量，随后仍可能与其他运行、助手或 viewer 竞争；成功不保证后续车道、连接或业务执行一定成功。业务步骤执行后，不因容量变化自动重放业务。详见[执行](execution.md#scheduled-browser-admission)及[浏览器后端](browser-backends.md#finite-profile-admission)。本次仅更新工作区文档/配置，不执行部署。

在部署使用的 `.env` 中修改 profile 预算；浏览器页面不提供修改入口，接口也不返回当前配置值。修改后等待任务及 viewer 结束，再按实际部署方式二选一重建 Worker。仅 `restart` 不会加载新的环境配置：

```bash
# GHCR 部署
docker compose up -d --force-recreate worker
# 源码部署
docker compose -f ops/docker-compose.yml up -d --force-recreate worker
```

若同时修改许可证环境变量，按下节重建三个服务。

### 配置许可证

仅在选用要求许可证的 Manager 时需要配置。从 https://cloakbrowser.dev/free 申请免费 key，或使用上游签发的付费 key，在部署使用的 `.env` 中配置：

```dotenv
CLOAKBROWSER_LICENSE_KEY=<your license key>
```

两份 Compose 都显式将 `CLOAKBROWSER_LICENSE_KEY: '${CLOAKBROWSER_LICENSE_KEY:-}'` 映射到 `api`、`worker`、`browser`，保证 shell 插值来源一致，不仅依赖 API/Worker 的 `env_file`。API 和 Worker 分别按各自本地非空环境变量拒绝修改。新增、修改或移除该环境变量都必须重建这三个服务，而不只是 browser；同时清理可能覆盖 `.env` 的 shell 导出值。先等待真实任务结束并关闭 viewer，再按实际部署方式执行；单纯 `docker compose restart` 不会加载修改后的 `.env`：

```bash
# GHCR 部署
docker compose up -d --force-recreate api worker browser
# 源码部署，二选一，不要混用 Compose 文件
docker compose -f ops/docker-compose.yml up -d --force-recreate api worker browser
```

浏览器重建会中断已有 CDP/VNC 连接；完成许可证验证及下载后，再手动启动所需车道，不自动重跑历史任务。

也可以将该变量留空或不设置，在 SiteFlow **浏览器/车道页面顶部（BrowserView）的 CloakBrowser Manager 卡片**输入新 key。卡片显示连接状态、来源、只读状态、是否已配置、许可等级及浏览器版本；不会读取或显示已有密钥或服务商掩码。**已配置不代表许可证有效**，也不代表下载完成。环境变量留空不是 keyless 模式，而是允许使用 Manager 已保存的 key 或通过卡片管理。普通保存仅接受非空新 key；点击独立的“清除许可证”并确认，才会以空字符串删除 Manager 保存的 key。环境变量锁定时保存和清除均禁用，API/Worker 同样拒绝。清除不删除环境变量或 wrapper 的 `license.key`，不停止已有浏览器，也不删除 profile/Cookie。详见 [SiteFlow License Card](browser-backends.md#siteflow-license-card)。

卡片以 `146.0.7680.177.5` 为已知旧版 keyless 参考版本：更高版本提示需要 key，不高于该版本仅在 Manager/二进制支持旧版 keyless 时可以免 key 工作，不保证任意旧版本可用；`free`/`pro`/`none` 授权状态优先，版本未知时不会推断为免 key。新版清除后可能无法再启动浏览器，切换旧镜像也必须同时处理已保存 key、缓存与 profile 降级兼容性。

卡片通过已认证的 `GET/PUT /api/v1/lanes/browser-settings`（PUT 另校验 CSRF）访问 API，再经带令牌的 Worker `/browser/settings` 访问 Manager。只转发 `license_key` 到 Manager `PUT /api/settings`，不改其他 Manager 设置；key 持久化在 `browser_data:/data`，API/Worker 不在数据库或 Worker 数据中重复保存。读取或保存不创建、修改、启动、停止或删除 profile，不改变任务/viewer 租约，也不自动启动浏览器或重跑任务；保存期间 Manager 可以验证授权并下载二进制。SiteFlow 不代理 Manager Settings 页面，不要直接对公网发布 Manager/CDP 端口。

环境变量非空时卡片只读，Manager 启动时优先采用环境变量。若选择页面管理，应移除所有非空环境变量覆盖并重建 `api worker browser`。自定义外部 Manager 必须在 **API、Worker、Manager 三处配置相同的 key 环境变量**，或三处都留空/不设置以使用 Manager 保存值。服务商 API 无法报告环境变量来源；`env` 标签及锁定依据 API/Worker 本地配置，不是远程检查 Manager 的实际环境，无法可靠发现仅在外部 Manager 上设置的 ENV。热更新用于后续启动，不保证给已运行的浏览器重新授权。缺少 settings API 的 Manager 在卡片中只读。

### 保存超时或结果未知

Manager 热保存涉及许可证检查及首次下载，可能需要数分钟，请等待结果；不会自动重试写入。PUT 已发送后超时、取消或响应异常，不代表 Manager 已停止处理。此时 Worker 会保留结果未知的粘性保护，`license.editable=false` 在 GET 刷新或重载页面后仍然有效；即使看到有效 tier/版本，也不证明原保存已完成。

先通过私有 Manager 状态/日志确认原写入和下载**实际完成**，不要公开密钥或服务商原始响应；随后等待任务排空、viewer 结束，再重启 Worker 清除保护。**不要为绕过仍在进行的下载而重启，不要在结果核实前重复提交，也没有自动重试。** 未修改环境变量时按实际部署方式二选一：

```bash
# 仅在 Manager 完成且任务/viewer 排空之后，GHCR 部署
docker compose restart worker
# 源码部署，二选一
docker compose -f ops/docker-compose.yml restart worker
```

重启后刷新卡片，核实状态再决定是否需要手动保存；如果同时改了环境变量，必须改用前述重建 `api worker browser` 命令。

### 从新版切回默认 Keyless 镜像

删除 `.env`、shell 或 Compose 覆盖中的 `SITEFLOW_BROWSER_IMAGE` 与 `CLOAKBROWSER_BINARY_PATH`；二进制路径不要保留显式空值。按原有部署使用的 Compose 文件拉取 `browser`，再按[缓存挂载切换](#缓存挂载切换)中的命令重建。如同时移除许可证环境变量，按[配置许可证](#配置许可证)重建三个服务。

该旧镜像无需许可证，不代表新镜像可以绕过许可证。升级/回退前先等待任务与 viewer 结束，成对备份 `browser_data` 和 `worker_data`（包含 `browser-profiles.json`），另行保留 `browser_cache`。浏览器 profile/数据不保证支持降级，必要时恢复匹配的升级前备份；移动缓存挂载并不解决数据降级兼容性。新布局中，显式旧 binary path 绕过 `/var/cache/cloakbrowser`，使用未挂载的镜像内路径，同一个缓存卷不会再遮蔽它；只有仍挂载 `/root/.cloakbrowser` 的旧 override 会继续遮蔽，应删除或调整该挂载，而不是换新缓存卷或删缓存。

仅将许可证环境变量留空不会切换镜像。已有 Manager `/data` 与 wrapper 缓存中保存的许可证应一并核查；卡片清除只移除 Manager 保存值，不删除 wrapper 文件。不要为清除许可证而删除整个缓存。含密钥备份保持 `0600`，禁止在日志中输出 key。详见 [Browser Backends](browser-backends.md#legacy-keyless-rollback)。

## 4. 服务与端口

| 服务 | 宿主机端口 | 内部端口 | 说明 |
|------|------------|----------|------|
| web | 8088 | 80 | Caddy，SPA + `/api` 反代 |
| api | 无 | 8000 | FastAPI，SQLite |
| worker | 无 | 6300 | 工作流与浏览器后端客户端 |
| browser | 无 | 8080 | 官方 Manager，CDP/VNC 转发与浏览器 |
| cfbox | 无 | 8000 | `ghcr.io/hyuan280/cfbox`，截图识别与内置 ONNX 模型 |
| bitwarden | 无 | 8090 | 可选，`bw` CLI HTTP 包装服务；API 自动检测 |

- 不使用 host 网络。
- 只有 web 对宿主机暴露；cfbox、bitwarden、browser 仅在 compose 内部网络提供端口。
- KasmVNC 端口不要发布；VNC 通过 API WebSocket 代理访问。

## 5. 首次登录

- 未配置 `SITEFLOW_ADMIN_PASSWORD` 且数据库无用户时，API 启动日志打印随机 admin 密码。
- 每次重启会重新生成，直到在页面修改密码写入 SQLite。
- 查看：

```bash
docker compose -f ops/docker-compose.yml logs api | sed -n 's/^api-1  | password: //p' | tail -1
```

- 配置了 env 管理员时使用 env 凭据，页面禁止修改密码。
- 首次登录后建议立即修改密码或配置 env 管理员。

## 6. 数据与备份

- `siteflow_data`：SQLite、API 数据。
- `bitwarden_data`：Bitwarden CLI 登录状态与本地配置；保险库数据仍在远端 Bitwarden 服务。
- `worker_data`：浏览器 profile ID 映射及车道运行数据。
- `browser_data`：外部浏览器 profiles、Manager 数据及页面保存的许可证（`/data`），需与 worker 映射一起备份；备份含敏感配置，文件权限应为 `0600`。
- `browser_cache`：`/var/cache/cloakbrowser` 下载缓存，包含隐藏标记及可能的 wrapper `license.key`，不是纯可丢弃二进制；备份含密钥，文件权限应为 `0600`，不要在日志中输出 key。挂载切换复用同一个实际卷，无需缓存迁移或删除；不能替代 profile 数据与映射备份。
- `browser_x11`：仅供 browser/worker 的 X11 socket，不是持久化业务数据。
- `siteflow_artifacts`：任务日志和失败截图，API/worker 共享挂载到 `/app/artifacts`。
- 备份时需要同时备份 `.env` 中的 `SITEFLOW_MASTER_KEY`；主密钥丢失则数据库中的加密配置不可恢复。
- 如需保留日志和截图，同时备份 `siteflow_artifacts` 卷（实际卷名通常为 `siteflow_siteflow_artifacts`）。
- 日志/截图默认保留 30 天，清理任务每 6 小时运行；可在系统设置中查看统计并手动清理。
- 示例：

```bash
docker run --rm \
  -v siteflow_siteflow_data:/data:ro \
  -v "$PWD/backups:/backup" \
  alpine:3.20 tar czf /backup/siteflow-data-$(date +%F).tgz -C /data .
```

## 7. TLS 与生产

- 生产建议在 Caddy 前加 HTTPS 域名，或修改 Caddyfile 启用 TLS。
- HTTPS 下设置 `SITEFLOW_COOKIE_SECURE=true`。
- 限制 `docker logs` 访问权限，因为未改密前日志含 bootstrap 密码。

## 8. 常用操作

```bash
# 查看日志
docker compose -f ops/docker-compose.yml logs -f api
docker compose -f ops/docker-compose.yml logs -f worker

# 重启（不加载环境变量修改；先等待任务/viewer 结束）
docker compose -f ops/docker-compose.yml restart

# 新增、修改或移除许可证 ENV 后重建三个服务，不是只重启 browser
docker compose -f ops/docker-compose.yml up -d --force-recreate api worker browser

# 停止但保留数据
docker compose -f ops/docker-compose.yml down

# 停止并删除数据（谨慎）
docker compose -f ops/docker-compose.yml down -v
```

## 9. 故障排查

- `worker` 不可达：检查 `api` 内 `SITEFLOW_WORKER_URL`、Compose 网络、worker 健康状态。
- 车道启动失败：检查内部令牌是否一致、许可证是否有效、tier 是否为 `keyless` / `free` / `pro`，以及下载网络/缓存。`none` 或未知 tier 会被拒绝；HTTP 402/403 提示许可证问题（包括可能的并发 profile 容量），不要通过粘贴服务商正文或密钥排查。
- 容量等待超时或手动启动快速失败：检查活跃任务、viewer、任务页、隔离清理及 Worker profile 上限；先让自有工作安全结束，关闭不再需要的 viewer，不强停其他任务或删除 profile。`idle_stop_seconds=0` 不会阻止安全压力停机，但 viewer 会阻止。自动调度预检失败不创建 Run，后续轮询可再检查；不要因此自动重跑已执行业务步骤。
- 许可证卡片保存结果未知或刷新后仍不可编辑：按[保存超时或结果未知](#保存超时或结果未知)核实 Manager 完成，再排空任务/viewer 并重启 Worker；刷新不会解除保护，不要绕过下载或自动重试。
- VNC 黑屏：检查 `.vnc-screen` 容器、noVNC target 是否为 div、WebSocket 是否建立。
- 代理站点失败：确认 `SITEFLOW_PROXY_URL`；系统不会回退直连。
- 2FAuth 失败：确认 URL/Token，或查看 `docs/verification.md` 的绑定说明。
