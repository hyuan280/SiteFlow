# 配置与安全

本页汇总环境变量、部署安全、模型配置与升级兼容，以及智能助手的授权、执行与失败接管规则。部署模板见 [`.env.example`](../.env.example)，各项设置见[集成配置](configuration.md)与[部署与运维](deployment.md)。

## 环境变量

下表覆盖 `.env.example` 中的配置项。「是」表示默认部署需要填写；「按条件」表示只有对应功能或部署方式需要。部分集成也可在页面配置，这时无需重复设置环境变量。示例域名、镜像版本和 `CHANGE_ME` 不是真实配置值。

### 部署与镜像

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_MASTER_KEY` | 是 | API 启动所需的加密主密钥，使用 `openssl rand -base64 32` 生成，可加 `base64:` 前缀。 |
| `SITEFLOW_DOMAIN` | 否 | 通知模板可引用的完整外部访问地址；未设置时该变量渲染为空。 |
| `SITEFLOW_IMAGE_OWNER` | 否 | 根目录 Compose 的 SiteFlow GHCR 镜像所有者，默认 `hyuan280`。 |
| `SITEFLOW_API_VERSION` | 否 | API 后端镜像标签，默认 `latest`。 |
| `SITEFLOW_WORKER_VERSION` | 否 | Worker 镜像标签，默认 `latest`。 |
| `SITEFLOW_WEB_VERSION` | 否 | Web 前端镜像标签，默认 `latest`。 |
| `SITEFLOW_BITWARDEN_VERSION` | 否 | SiteFlow Bitwarden 封装镜像标签，默认 `latest`。 |
| `SITEFLOW_CFBOX_VERSION` | 否 | CFbox 镜像标签，默认 `latest`。 |
| `SITEFLOW_WEB_PORT` | 否 | 根目录 Compose 对外发布的 Web 端口，默认 `8088`。 |

### 浏览器与车道

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_BROWSER_TOKEN` | 是 | Compose 中浏览器后端与 Worker 共用的访问令牌，可用 `openssl rand -hex 32` 生成。 |
| `SITEFLOW_BROWSER_BACKEND` | 否 | 浏览器后端类型，默认 `cloak-manager`。 |
| `SITEFLOW_BROWSER_URL` | 否 | Worker 访问 Manager 的内部地址；两份 Compose 固定为 `http://browser:8080`。 |
| `SITEFLOW_BROWSER_DESKTOP` | 否 | 桌面传输方式，默认 `x11`；设为 `none` 关闭桌面操作，保留 CDP 页面操作。 |
| `SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES` | 否 | Worker 的并发 profile 预算，整数 `1..4`，默认 `1`；有许可证的免费版硬上限为 `1`。 |
| `SITEFLOW_BROWSER_IMAGE` | 否 | Manager 镜像；默认使用固定的官方 keyless 镜像。 |
| `CLOAKBROWSER_BINARY_PATH` | 按条件 | 未设置时使用镜像内置 Chromium 路径；选用不自带浏览器的新版镜像时，显式设为空。 |
| `CLOAKBROWSER_LICENSE_KEY` | 按条件 | 默认 keyless 无需许可证。选用要求许可证的镜像时，通过此变量或 Manager 设置提供有效许可证。 |
| `SITEFLOW_LANE_IDLE_STOP_SECONDS` | 否 | 车道空闲自动停止时间，默认 `300` 秒；`0` 仅关闭定时停止。设置后页面只读。 |
| `SITEFLOW_PICKER_TTL_SECONDS` | 否 | 元素拾取会话过期时间，默认 `900` 秒。 |

### 管理员与会话

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_ADMIN_USERNAME` | 否 | 环境变量管理员账号名，默认 `admin`。 |
| `SITEFLOW_ADMIN_PASSWORD` | 否 | 设置后以该密码为准，页面不能修改；未设置时使用数据库管理员或首次启动生成的密码。 |
| `SITEFLOW_SESSION_TTL_HOURS` | 否 | 会话有效期，默认 `168` 小时。 |
| `SITEFLOW_COOKIE_SECURE` | 按条件 | 通过 HTTPS 对外访问时设为 `true`；本地 HTTP 保持默认 `false`。 |
| `SITEFLOW_LOGIN_MAX_ATTEMPTS` | 否 | 登录保护时间窗内允许的失败次数，默认 `5`。 |
| `SITEFLOW_LOGIN_LOCKOUT_SECONDS` | 否 | 登录失败统计与锁定时间，默认 `300` 秒。 |

### 代理、2FAuth 与 CFbox

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_PROXY_URL` | 按条件 | 使用代理车道时须配置代理，可在页面配置。支持 HTTP、HTTPS、SOCKS5 和 SOCKS5H。 |
| `SITEFLOW_TWOFAUTH_URL` | 按条件 | 使用 2FAuth 时必需，可在页面配置。 |
| `SITEFLOW_TWOFAUTH_TOKEN` | 按条件 | 使用 2FAuth 时必需，须与服务地址配套，可在页面配置。 |
| `SITEFLOW_CFBOX_URL` | 按条件 | 使用 CFbox 时须指定地址；模板使用 Compose 内部服务 `http://cfbox:8000`。 |
| `SITEFLOW_CFBOX_API_KEY` | 按条件 | CFbox 开启认证时须与服务端密钥一致。 |
| `CFBOX_IDLE_UNLOAD_SECONDS` | 否 | CFbox 模型闲置卸载时间，Compose 默认 `300` 秒。 |
| `CFBOX_LOAD_ON_STARTUP` | 否 | CFbox 是否在启动时加载模型，Compose 默认 `true`。 |

### Bitwarden

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `BW_SERVER` | 按条件 | Bitwarden 封装服务访问的服务器地址；使用自托管 Vault 时填写。 |
| `BW_CLIENTID` | 按条件 | 使用内置 Bitwarden 封装服务时必填，Vault API 客户端 ID。 |
| `BW_CLIENTSECRET` | 按条件 | 使用内置 Bitwarden 封装服务时必填，Vault API 客户端密钥。 |
| `BW_PASSWORD` | 按条件 | 使用内置 Bitwarden 封装服务时必填，用于解锁 Vault 的主密码。 |
| `BW_API_TOKEN` | 否 | 为封装服务的 `/lookup` 启用 Bearer 认证。 |
| `BW_CACHE_SECONDS` | 否 | 封装服务缓存 Vault 条目的秒数，默认 `60`。 |
| `SITEFLOW_BITWARDEN_URL` | 否 | 覆盖 SiteFlow 使用的封装服务地址；默认可探测 `http://bitwarden:8090`。 |
| `SITEFLOW_BITWARDEN_TOKEN` | 按条件 | 封装服务启用 Bearer 认证时须提供对应令牌，可在页面配置。 |
| `SITEFLOW_BITWARDEN_ENABLED` | 否 | 从环境覆盖集成启用状态；未设置时由页面设置或自动探测规则决定。 |
| `SITEFLOW_BITWARDEN_AUTO_DETECT` | 否 | 是否探测默认 Compose 服务，默认 `true`。 |
| `SITEFLOW_BITWARDEN_TIMEOUT` | 否 | Bitwarden 请求超时，默认 `30` 秒。 |
| `SITEFLOW_BITWARDEN_PROBE_TIMEOUT` | 否 | Bitwarden 健康探测超时，默认 `2` 秒。 |

### 大模型与智能助手

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_CONFIG_AI_ENABLED` | 否 | 启用智能助手和文本配置生成，默认 `false`；执行仍需单独授权。 |
| `SITEFLOW_MODEL_BASE_URL` | 按条件 | 从环境添加模型条目时须填写 OpenAI 兼容 API 根地址，例如以 `/v1` 结尾的地址。 |
| `SITEFLOW_MODEL_API_KEY` | 按条件 | 从环境添加模型条目时须填写 API Key。 |
| `SITEFLOW_MODEL_ID` | 按条件 | 从环境添加模型条目时须填写上游模型 ID。 |
| `SITEFLOW_MODEL_SUPPORTS_VISION` | 否 | 环境模型是否支持视觉，`false` 表示仅文本；省略时使用已保存值，无已保存值时默认 `true`。 |
| `SITEFLOW_MODEL_MAX_TOKENS` | 否 | 模型输出 token 上限，非负整数；`0` 表示不传 `max_tokens`，由上游决定。 |
| `SITEFLOW_MODEL_TIMEOUT` | 否 | 所有模型共用的请求超时，默认 `30` 秒，最大 `300` 秒。 |

### 邮件与 HTTP 通知

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_NOTIFICATION_EMAIL_ENABLED` | 否 | 邮件通道的初始启用状态，默认关闭；已保存的页面开关优先。 |
| `SITEFLOW_NOTIFICATION_EMAIL_SMTP_HOST` | 按条件 | 使用邮件通道时须提供 SMTP 主机，可在页面配置。 |
| `SITEFLOW_NOTIFICATION_EMAIL_SMTP_PORT` | 否 | SMTP 端口，默认 `587`。 |
| `SITEFLOW_NOTIFICATION_EMAIL_SECURITY` | 否 | SMTP 传输加密方式，默认 `starttls`。 |
| `SITEFLOW_NOTIFICATION_EMAIL_USERNAME` | 按条件 | SMTP 服务器要求认证时提供账号。 |
| `SITEFLOW_NOTIFICATION_EMAIL_PASSWORD` | 按条件 | SMTP 服务器要求认证时提供密码或授权码。 |
| `SITEFLOW_NOTIFICATION_EMAIL_FROM` | 按条件 | 使用邮件通道时须配置发件人，可在页面配置。 |
| `SITEFLOW_NOTIFICATION_EMAIL_TO` | 按条件 | 使用邮件通道时须配置收件人，多个地址用逗号分隔，可在页面配置。 |
| `SITEFLOW_NOTIFICATION_HTTP_ENABLED` | 否 | HTTP 通道的初始启用状态，默认关闭；已保存的页面开关优先。 |
| `SITEFLOW_NOTIFICATION_HTTP_URL` | 按条件 | 使用 HTTP 通道时须配置 Webhook 地址，可在页面配置。 |
| `SITEFLOW_NOTIFICATION_HTTP_METHOD` | 否 | Webhook 请求方法，默认 `POST`。 |
| `SITEFLOW_NOTIFICATION_HTTP_TITLE_FIELD` | 否 | 请求载荷中的标题字段名，默认 `title`。 |
| `SITEFLOW_NOTIFICATION_HTTP_BODY_FIELD` | 否 | 请求载荷中的正文字段名，默认 `content`。 |
| `SITEFLOW_NOTIFICATION_HTTP_IMAGE_FIELD` | 否 | 图片字段名，默认 `none`，即不附带图片。 |
| `SITEFLOW_NOTIFICATION_HTTP_HEADERS` | 按条件 | Webhook 需要认证或自定义请求头时配置，值为 JSON 对象。 |
| `SITEFLOW_NOTIFICATION_HTTP_TIMEOUT` | 否 | HTTP 请求超时，默认 `10` 秒，上限 `120` 秒。 |

### 插件运行时

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_PLUGIN_TOKENS` | 按条件 | 注册自定义插件时配置插件 ID 到注册令牌的 JSON 映射，每个插件使用独立令牌。 |
| `SITEFLOW_PLUGIN_DEMO_TOKEN` | 按条件 | 本地部署覆盖 Demo 插件令牌时须设置，并与上项映射中的 `demo` 值一致。 |
| `SITEFLOW_PLUGIN_HEARTBEAT_TTL_SECONDS` | 否 | 插件心跳有效期，默认 `90` 秒。 |
| `SITEFLOW_PLUGIN_ENDPOINT_ALLOWLIST` | 否 | 允许注册的插件地址，可填主机名、主机和端口或 CIDR，多个值用逗号分隔。 |
| `SITEFLOW_PLUGIN_VERIFY_ENDPOINT` | 否 | 是否校验插件端点，默认 `true`。 |
| `SITEFLOW_PLUGIN_RESOLVE_ENDPOINT` | 否 | 是否解析插件主机名并校验地址范围，默认 `true`。 |
| `SITEFLOW_PLUGIN_METADATA_TIMEOUT` | 否 | 请求插件元数据的超时，默认 `3` 秒。 |

### 执行、调度与运行文件

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_WORKER_URL` | 否 | API 访问 Worker 的内部地址；两份 Compose 固定为 `http://worker:6300`。 |
| `SITEFLOW_ARTIFACT_ROOT` | 否 | 运行日志和截图的存储根路径，两份 Compose 使用 `/app/artifacts`。 |
| `SITEFLOW_ARTIFACT_RETENTION_DAYS` | 否 | 运行文件保留天数，默认 `30`。 |
| `SITEFLOW_ARTIFACT_CLEANUP_INTERVAL_SECONDS` | 否 | 运行文件清理检查间隔，默认 `21600` 秒。 |
| `SITEFLOW_ARTIFACT_CLEANUP_ENABLED` | 否 | 是否启用运行文件自动清理，默认 `true`。 |
| `SITEFLOW_EXECUTION_TIMEOUT` | 否 | API 等待单个站点任务完成的超时，默认 `600` 秒。 |
| `SITEFLOW_EXECUTION_MAX_RUNS` | 否 | 全局并发运行批次数量，默认 `2`。 |
| `SITEFLOW_EXECUTION_MAX_JOBS` | 否 | 全局并发站点任务数量，默认 `10`。 |
| `SITEFLOW_LANE_GENERAL_CONCURRENCY` | 否 | 普通车道的任务并发上限，默认 `5`，范围 `1..10`。 |
| `SITEFLOW_LANE_PROXY_GENERAL_CONCURRENCY` | 否 | 代理普通车道的任务并发上限，默认 `3`，范围 `1..10`。 |
| `SITEFLOW_SCHEDULER_ENABLED` | 否 | 从环境锁定全局调度开关；未设置时可在页面修改。 |
| `SITEFLOW_SCHEDULER_POLL_SECONDS` | 否 | 调度器轮询间隔，默认 `30` 秒。 |
| `SITEFLOW_SCHEDULER_MODE` | 否 | 覆盖调度模式，可选 `cron`、`interval` 或 `random`。 |
| `SITEFLOW_SCHEDULER_CRON` | 否 | 覆盖 Cron 表达式，用于 `cron` 模式；也可在页面配置。 |
| `SITEFLOW_SCHEDULER_INTERVAL_SECONDS` | 否 | 覆盖固定调度间隔（秒），用于 `interval` 模式。 |
| `SITEFLOW_SCHEDULER_RANGES` | 否 | 覆盖随机调度时间段，用于 `random` 模式；多个时间段用逗号分隔。 |
| `SITEFLOW_SCHEDULER_TIMEZONE` | 否 | 调度及相关时间显示使用的 IANA 时区，默认 `Asia/Shanghai`。 |

### Docker 资源监控

| 环境变量 | 是否必填 | 中文说明 |
| --- | --- | --- |
| `SITEFLOW_DOCKER_SOCKET` | 否 | API 容器内访问的 Docker socket 路径，默认 `/var/run/docker.sock`。 |
| `SITEFLOW_DOCKER_PROJECT` | 否 | 监控时筛选的 Compose 项目名；根目录 Compose 从 `COMPOSE_PROJECT_NAME` 映射，默认 `siteflow`。 |
| `COMPOSE_PROJECT_NAME` | 否 | 自定义的 Compose 项目名，默认 `siteflow`；已有部署应保留原项目名。 |
| `SITEFLOW_DOCKER_SOCKET_HOST` | 按条件 | 启用可选监控且宿主机 socket 不在默认位置时指定宿主机路径。 |

## 部署安全

- `.env` 不提交到 Git，文件权限保持 `600`。不要公开主密钥、账户密码、Cookie、后端令牌和集成服务凭据。
- 单独备份 `SITEFLOW_MASTER_KEY`；丢失后无法恢复数据库中的加密配置。数据库与浏览器数据备份见部署文档。
- 使用 HTTPS 对外访问时设置 `SITEFLOW_COOKIE_SECURE=true`；本地 HTTP 使用 `false`。
- 只发布 Web 端口。不要公开 Worker、Manager、CDP、VNC 或共享 X11 socket。
- 初始密码可能出现在 API 日志中，需限制日志访问并及时改密。
- 各浏览器车道使用独立 profile，同车道任务使用独立标签页；更新服务前确认没有活动任务或人工浏览器会话。涉及远程后端接入的更新需同时更新 API 和 Worker。

## 集成与模型配置

- 代理、CFbox、2FAuth、Bitwarden、大模型等按需在「系统设置」配置；模型列表上移/下移、临时禁用或删除须显式保存，配置弹窗可一并保存列表。保存前请测试连通性，测试不代表保存配置。行内「测试」只测指定 ID 的已保存连接；弹窗「测试当前配置」可测试草稿，新条目或填写新 API Key 时由浏览器直连当前地址，不把该密钥交给 SiteFlow 后端；已有条目密钥留空则由 API 在内存中复用该条已保存密钥。浏览器失败不自动转交后端，成功也不证明 API/Worker 的生产连通性。可选 `SITEFLOW_MODEL_*` 连接设置形成一条带「环境变量」来源标签的普通条目：整条连接只读、不能打开配置弹窗或删除；UI 可保存排序和启用状态，名称/用途仅可通过 API 更新元数据，不能在 UI 编辑。其他模型不继承这些值。详见[模型设置与测试边界](vision-feature-settings.md#model-registry)。
- 升级须配套更新 **API、Worker、Web**，不新增 SQL 列。模型环境变量已改名为 `SITEFLOW_MODEL_*`，**旧 `SITEFLOW_VISION_*` 名称被忽略，没有别名回退，部署方必须重命名**；上游模型名变量为 `SITEFLOW_MODEL_ID`。`SITEFLOW_MODEL_TIMEOUT=30` 是所有模型共用的请求超时（秒），单独设置不产生环境变量条目。无注册表且无旧连接/环境连接时列表为空；存在旧连接时只读合成普通条目，不迁移或改写原 `vision.*` 密文。旧 `/vision` API 仅兼容内部旧配置条目，与列表首选无关；其内部 ID `primary` 不是 UI 角色。数据库旧配置条目可删除，删除同时清理活动旧连接，避免重现；加密 `models.registry` 最多保留 512 个仅供历史日志脱敏的退役密钥，不用于运行时或返回 API，删除不承诺永久擦除。CAS 冲突保留草稿、不自动重试，刷新前核对未保存改动。

## 智能助手：授权与执行

- 使用智能助手需配套升级 Web、API 和 Worker，复用 `SITEFLOW_CONFIG_AI_ENABLED` 与大模型配置。API 注册新模型后用 `create_all()` 创建会话、消息、`assistant_workflow_actions`、`assistant_run_guards`、`assistant_job_config_snapshots` 和 `assistant_takeover_actions` 表；不是已有列迁移机制，工作流授权/标题控制字段在加密 JSON 内，不承诺迁移历史开发版表结构。新会话写入固定欢迎语、不调用模型；前 3 条用户消息内成功轮次可独立生成标题，失败仍消耗窗口，人工重命名永久锁定，旧会话默认锁定。
- 助手通过对话收集站点名称和地址，再由宿主面板确认浏览观察；执行、截图、导入/完整测试分别按需授权，也可在全局助手设置中明确开启对应默认值。凭据 `site_id` 与配置目的站点 `workflow_site_id` 独立，均须完全同源并绑定身份/修订。浏览观察使用交互/代理交互车道；保存运行车道独立，新目标默认 `general`，变更须用户确认。新导入站点默认 `enabled: false`，明确授权的助手试跑不会自动启用调度。
- 助手普通会话共 **38 个工具**，注册表另含 1 个接管专用工具，合计 **39 个**，接管会话使用受限子集。当前 **23 项内置功能**在助手及独立生成 API 中均可编写提案，目录/schema 按当前代码及服务端策略提供，包括公开非秘密 `action.fill` 和四项 `post.*`；插件、别名及未知键不开放。孤立单步 **18 项**，排除 `flow.jump` 和四项 `post.*`。后者尚未实现孤立测试，因为依赖真实 SiteJob 消费检查点，不是用户拒绝权限。`action.fill` 不能填密码、凭据、Cookie、Token 或 OTP/TOTP 认证动态码，具体字段/内容仍受宿主校验；图片 CAPTCHA 不等于 OTP。`flow.result` 单步只返回配置布尔值、不做模型判断，false 是步骤失败，不证明网站操作失败。完整工作流结果判断可按宿主策略使用模型，运行配置/提示词/提供商密钥由宿主私下传给 Worker，助手只获安全布尔结果/状态，不获原始判断答案、理由或密钥。
- 助手新浏览器首次导航前可私有注入显式有效绑定站点的已保存 Cookie，优先 `workflow_site_id`、否则凭据 `site_id`，优先绑定失效不回退、不按主机找站点；所有凭据模式均可，不增加 auth/OTP/完整运行授权，原始 Cookie 不进模型或助手会话。`clear_bound_site_cookies` 仅应用户明确要求，在原始绑定浏览器已 READY 时逐次审核清除该站点保存 Cookie 及当前共享车道精确 host/.host 的所有路径 Cookie，不清父域/其他车道、不改账号密码/localStorage、不刷新；同主机同车道标签页可能退出。无人值守接管不可清除，部分/未知结果不自动重试，也不自动打开浏览器满足清除前提。需 API、Worker、Web 配套升级；范围、generation 防旧种子回放及隐私限制见[绑定站点 Cookie](ai-assistant.md#绑定站点-cookie)。
- 类别只信任代码内置 `Feature.category` 的 flow/auth/challenge/sign/action/post，不按键前缀、描述、模型参数或插件声明授权。`max_auth_uses_per_turn` 默认 **3**、严格整数 **1-20**，助手单步及完整保存运行共用发起真实用户轮次的登录/密码验证及重试额度，解析凭据前预留、失败计次；换目标/浏览器、内部模型轮次不重置。完整运行在运行时签发每次 auth 凭据租约时计一次，兑换同一租约不重复计次；条件跳过不计，循环重入及执行器重启/回退尝试申请新租约仍计次，后台运行不随新聊天轮次重置原额度。含 auth 的新 `run_site` 派发前检查剩余额度，运行中耗尽则拒绝后续 auth 租约；普通手动/调度任务不受此助手额度限制。`challenge.2fa` 属于 challenge，不占 auth 额度，OTP 始终私有。支持的 flow/challenge/sign/action 单步可在当前原始同源授权内继续，不因历史 unknown 一概拒绝；普通会话 `retry_step` 自动签发一次精确存储步骤能力，以新 UUID 执行、不弹 UI 审批，auth 重试另计额度。旧空闲/审批 API 保留人工兼容，不代表所有重试必需审批；自动接管仍拒绝 `retry_step`，新 execute_step 按类别授权检查。原 unknown 来源事实不清除，配置写入/导入及完整运行门禁不变。类别许可不是无副作用保证，物理点击、导航和脚本仍可能写网站。插件可声明六类，但元数据未验证、危险实际实现需审核，不因此开放助手。
- 导入只接受当前会话成功保存、当前目标修订且无待确认问题的 `proposal_message_id`。`update_site_config` 仅按用户要求修改受限字段及观察设置，逐次展示精确差异供确认，全局授权不能跳过；启用调度另需同会话当前配置完整成功任务。配置更新使用 CAS 并生成历史，凭据、模板和工作流集合不被设置工具重写。`run_site {}` 只经共享 executor 执行绑定的单个已保存站点，无任意 ID/全部/force。全部 23 项内置功能可用于完整运行，插件功能/方法/提供商仍拒绝，包括继承项；可信观察来源不授予完整运行或凭据权限。配置守卫核对本地/解析配置、祖先模板和功能策略；无消费计划时正常同代理模式通用到交互回退仍允许。
- 助手完整运行中的 `post.buy_card`、`post.resign`、`post.resign_loop`、`post.resign_dialog` 每次实际新派发都须 owner **逐 post 步骤**审核冻结消费计划，每个步骤全新面板、全部批准后才启动一次保存的完整工作流，不为每个日期或内部点击再弹窗。含条件启用步骤、全部过去待补签日期（最多 365 天窗口）、时区/当天、数量及费用上限。购卡数量为 0-365 整数，0 按缺口减库存补齐且仍有最大数量，`max_price` 必须为正整数，显示最大总花费而非实时报价；宿主私下生成日期/授权及 Worker 计划。全局自动授权不能跳过，无自动 fallback/restart/重试 attempt。相同配置/提案重放只返回原 run ID，不再弹审核或提交。通用点击/导航效果不总能识别，禁止用点击、填充、导航或循环绕过审核，不保证识别所有网页消费。**导入已有启用站点保留调度，可能影响后续自动任务；新消费审核仅约束助手调用，不是手动/调度的新门禁，后者按既有策略执行。**
- 页面循环补签适用于选日期后再点击独立提交按钮的页面，动作后交给下游验证码等步骤，再通过同 job 回跳复核；全部日期 done 才清除断签信号。默认每日期首次提交加最多 3 次额外重试，不读取卡数、不安装跨源网络拦截，也不支持跨进程自动续跑。旧页面补签步骤需手动重配，无旧键别名或自动迁移；11 字段配置和循环示例见[使用指南](usage.md#页面循环补签)。
- 完整运行的重复调用只返回原 run ID、不重新提交；提交确认丢失为 `run_submission_uncertain`，可能已产生副作用的异常保留 unknown，禁止新完整运行或借单步重放整次运行，调度跳过未决站点。当前类别授权内的非 post 单步不因此一概拒绝，也不核销配置写入/完整运行门禁。当前无完整运行审批/核销 UI，须人工核清，不能套用单步重试能力。`get_run_results` / `get_run_log` 仅限本会话关联运行，分页使用当前投影哈希、变化重读；后者是受控结构化诊断而非实际 artifact 日志，历史文件另用专用工具。交互确认最多等待 180 秒，等待时间不占对话执行预算；普通轮次结束后不自动监看或回来回复，独立失败接管不是轮次续跑。助手创建的任务和配置历史操作人为英文 `assistant`，真实用户保留为会话/操作授权 owner，不改写历史手动任务。详见 [智能助手](ai-assistant.md#导入与完整测试)。
- 模板编辑器「从站点导入配置」只替换表单本地三集合及父模板（来源 template_id 映射为 parent_id），保留目的名称/描述/enabled，不复制站点元数据/秘密，不自动保存或运行。助手 `create_template_from_site` 则从已绑定 workflow_site_id 经独立精确审核保存，可新建或以目的 revision/history_key 替换已有模板并保留其元数据；有效且无活动/未知任务的来源**不要求成功助手完整运行**。冻结来源/目的快照与依赖，批准复查并 CAS，漂移不写，相同 UUID/请求幂等，不改来源或自动执行，见[模板导入](ai-assistant.md#从站点保存模板)。
- 助手模板替换是精确来源本地三集合及父模板替换，**不是隐式合并**。准备及应用均检查含继承项的解析结构：原分组不得删除/改名/重排，原步骤不得删除/换组/重排；可更新步骤配置、任意插入新分组/步骤，数值位置/锚点可变但须保留原项相对顺序。审核可展开完整本地候选及按 key 的前后差异，`before` 为目的原本地三集合及父模板（新建 null）；受保护配置无法安全展示则拒绝，不用隐藏标记代替，旧审批缺基线时明示不可用。人工编辑器导入行为不变，完整约定见[模板导入](ai-assistant.md#从站点保存模板)。
- `get_scheduler_settings` 读全局调度；`update_scheduler_settings` 仅在用户要求后审核 enabled/mode/cron/interval_seconds/ranges 精确稀疏补丁，不改站点或 timezone。调度环境覆盖锁定编辑，时区仅环境管理；冻结配置/环境并做修订 CAS，动态 next_run_at 不计绑定，成功重算，相同操作幂等。`cancel_run` 另需精确任务确认，批准后提交取消并联系真实 executor；cancel_requested/通知送达不等于已停止，不撤回副作用，聊天「停止」也不取消完整任务。见[调度与取消](ai-assistant.md#调度与取消)。
- 历史版本按 history_key 读取脱敏本地/解析配置及依赖，artifact 工具覆盖已授权管理 UI 的历史运行，不限本会话创建，也不接受任意文件路径。`read_artifact_log` 读实际文件（原文件最多 2 MiB），对全文替换已知密钥/凭据模式后按字符分页（每页最多 16000），不是 curated `get_run_log` 或通用 DLP。`read_artifact_image` 需 supports_vision，读取**未遮罩的真实历史像素**，可能含秘密；原 PNG/JPEG 最多 2 MiB，移除元数据、按比例缩至最多 1600 × 1200、重编码 JPEG 最多 384 KiB，不等于像素脱敏。历史资料发送授权独立于实时截图同意，见[历史资料边界](ai-assistant.md#历史版本与-artifact)。
- 历史发现可从 `list_sites` / `get_site_config` 返回的 `history_key` 开始。站点/模板、近期运行及运行摘要 jobs 均可分页，聊天列表也提供「加载更多会话」，不再只限前 100 个；实时偏移可能重叠/遗漏，应按 ID 去重并刷新。分页范围、导航 path 2048 / url 4096、CFbox URL 必需/Key 可选及状态工具 `model_config_scope=environment_only` 的完整契约见[数据查询](ai-assistant.md#数据查询)与[历史与状态](ai-assistant.md#历史与状态)；工具数仍为普通 38、合计 39。

## 智能助手：失败接管与通知

- 顶部「助手接管」位于「助手设置」之前，新策略默认 enabled=false、live_access=true、analysis、site_ids=[]、max_attempts=1（1-3）；整体仍默认关闭。唯一的实时复选框须核对并保存才授权，无独立浏览器功能开关；完全匹配旧版已存结构的策略只读补 live_access=false，不自动扩权，须重新勾选并保存。列表仅覆盖所选启用站点，live 策略保存事务冻结所选站点 history_key/规范 origin 到独立 AppSetting 键 `assistant.takeover.site_grants`，准入复查策略修订、完整 ID 集合及绑定；旧 live 策略缺绑定或绑定无效时 fail closed，须核对并重新保存。null 明确授权所有当前及未来启用站点，无需预存未来站点绑定，但实际 action 冻结具体身份/origin/来源。owner 从登录设置，策略 CAS、审计及 effective_from 持久化，打开/刷新/保存不调模型、不启动浏览器或任务。接管失败、错误和需要人工的任务；已推迟不接管。只处理生效后新建、已结束且状态为 fail/error/waiting_human 的手动/调度任务，不回填历史；取消、unknown、中断、deferred、未完成任务和助手运行均排除。analysis 不保存配置，**不等于网站只读**；live_access 可在用户不在线时浏览、按视觉能力截图及执行冻结清单内最多 18 项真实同源单步，包括私有凭据登录、签到和点击。代理由已保存车道映射：proxy_general/proxy_interaction 到 proxy_interaction，其余到 interaction，无 fallback；首次 Worker open 即携带严格同源约束，首次导航前生效，不自动添加来源。autofix 仍仅在失败快照匹配时一次导入来源工作流，不自动完整重跑、重试未知动作或取消，不改凭据、站点元数据、模板或全局设置。插件/提供商仍拒绝，秘密不进模型或日志，输入省略与截图遮罩不变；人工设置/模板/全局变更及完整运行消费仍须各自精确确认。见[失败接管](ai-assistant.md#失败接管)。
- 历史失败记录或与当前来源无关的 unknown 记录本身不阻止符合条件的新故障入队和分析；有效 live_access 下可浏览并按当前类别授权尝试支持的同源单步，auth 受共享额度限制。配置保存/导入及完整运行的不确定性门禁仍有效，不自动重放历史部分消费。waiting_human 挑战须通过实时授权及宿主检查，不保证无副作用或全部可自动处理；暂不复用旧挑战分析。以 Piggo 为通用示例：旧失败或 unknown 不等于已调查，只有有效的同版本同故障结论才可复用；这不是对 Piggo 生产环境或修复成功的声明。每日聊天路由复用不是结论复用。
- 重启或重新部署不会因新规则扩大准入而回填旧任务。周期扫描只发现本次 API 进程创建接管管理器时或之后结束的 Run（UTC `Run.finished_at >= _scan_started_at`），仍须满足策略生效后创建等条件；启动前已开始、启动后才结束的运行可以入队。已有 queued action 仍恢复处理；dispatched 有精确已接受用户消息及有效同 owner 会话时保留供持久证据完成恢复，否则启动回退仍按导入回执记 updated、无回执记 unknown。完成恢复不重排队、不重放模型或网站动作；通知网络派发不明仍 unknown、不重发，见[结论与通知](ai-assistant.md#结论与通知)。不改变历史结论去重范围或显式终态通知规划入口。
- 接管按 owner、站点 `history_key`、来源 SiteJob 的 `finished_at` 在站点时区换算的本地日期复用每日会话，不是按星期几或处理时间分组。自动轮次结束清除 action 委托，但 live_access 已授予的浏览/执行/可用凭据/截图权限保留在该会话供人工继续，关闭策略不追溯撤销；人工后续 `workflow_consent=true` 无论自动阶段为 analysis/autofix 都保留。人工明确要求可走普通导入/完整运行流程，购买/补签每次新派发仍须消费确认；自动 analysis 不能保存，自动阶段不开放 post.*、flow.jump 单步或完整运行。自动接管成功、错误或取消均在 `finally` 释放自己的页面和租约，清理后才将会话置为 idle，不保留到 15 分钟到期；历史与有效权限保留。人工后续可复用已授予且仍有效的权限重开新页面，无需重复授权，不保证原页面/DOM/临时状态恢复，也无需先手动完整运行才能单步验证；身份、origin 或绑定变化须重确认，不借用全局自动授权。跨日最多带入 3 条以往结论及字段级变更证据，排除当前日，历史内容不是授权。
- 自动接管的浏览器排队由 API/Worker 协调：`browser_open` 使用 `wait_for_lane: true`，非阻塞返回并轮询 `queued/opening/ready`，每车道 FIFO、最长 **1200 秒（20 分钟）**，实际排队不占活动预算；同一工具调用内 await 一次，等待期间不额外调 LLM。超时/取消/策略撤销只清理自己的排队项、页面和租约，不抢占人工浏览器或 viewer；来源活动任务、拾取器共享执行信号量。两车道队列独立，但当前主自动消费者仍串行，不同时运行两个自动浏览接管，不承诺跨车道并行。人工助手浏览器仍从准入/创建起固定 **15 分钟**，消息不续租，不是最后消息闲置计时；可配置车道 `idle_stop_seconds` 默认 **300 秒**，无 lease/viewer 后才计时，不是助手 TTL，也不参与结果通知判断。详见[排队与所有权](ai-assistant.md#排队与所有权)。
- 相同失败去重另按 **owner + 站点历史身份/origin + 仍匹配当前配置的实际派发完整指纹/修订 + v2 失败签名**，不是每日聊天复用。签名包含实际车道、失败步骤/功能/状态、reason/field/stage 等事实及规范化 message 的哈希，不在去重元数据保存原文；无稳定事实的通用错误保守不去重。仅有效 completed_analysis/updated 正式结论及明确成功的类型化轮次结果可复用，失败/unknown/排队或只有签名不算处理成功。无关历史 unknown 不阻止有效结论复用，因为复用不派发新动作，也不解除变更安全门禁。站点重新保存产生新修订、快照任意字段实际变化或依赖/功能策略变化均失效，此前 updated 不抑制修复后新版本。新 action 仍留审计，reused、attempts=0，旧会话存在则关联，不新调模型/开浏览器/修复；UI 标「相同失败，复用已有分析」，通知明确「复用此前分析，当前未重新分析/未修复」，当前任务仍失败。详见[相同失败去重](ai-assistant.md#相同失败去重)。
- 去重安全要求还包括：失败步骤之外应同时区分完整 Run/Job 终态原因及明确 false 的结果；在不同配置下运行的过期分析，即使后来恢复旧配置也不得作为原版本已处理的依据；明确未接受的 busy 等发送仍是可处理的排队工作，不是成功标记，也不得抑制后续合格分析。这些约束不声明新增公开字段，详见[相同失败去重](ai-assistant.md#相同失败去重)。
- 接管面板 `conclusion_ttl_days` 为严格整数 1-30，默认 7 天；跨日复用仍受原正式结论的 UTC 滚动年龄限制，复用不续期。过期后下一个合格新来源正常调查，不自动回填、重排队或删除历史，已有历史/冻结通知仍可读。旧设置只读兼容、精确到期边界及原有 CAS/生效时间规则见[失败接管](ai-assistant.md#失败接管)与[相同失败去重](ai-assistant.md#相同失败去重)。
- 性能修正通过 `_phase` 将接管启动恢复、后台 reconcile、process_one 的选择/运行时预检/会话创建/认领/发送失败处理/完成等同步阶段移出事件循环；executor 准备/终态、接管策略保存和助手 SSE 详情/编码也在线程处理。各数据库 helper 自行管理 session，模型发送/任务等待/唤醒仍在事件循环；通知规划、reconcile、选择和认领各用独立阶段缓存，只限同一事务/session/策略视图，不跨 await、提交或授权/配置变化。未变化的 running SSE 也仅 heartbeat，工具 JSON 完整格式化/展示按展开惰性生成，原结果、提案和安全/审批功能保留；不保证任何负载下绝不卡顿。范围见[性能与边界](ai-assistant.md#性能与边界)。
- 接管记录在 `DATABASE_URL` 指定数据库，Compose 默认 SQLite `/app/data/siteflow.db`（`siteflow_data` 卷）：审计 `assistant_takeover_actions`、派发快照 `assistant_job_config_snapshots`、每日路由 `assistant_takeover_daily_conversations`、候选 `assistant_takeover_conclusion_candidates`、正式结论 `assistant_takeover_conclusions`，另有独立调查通知账本 `assistant_takeover_notices`；结论/通知独立于聊天保留，备份数据库还须单独保管 `SITEFLOW_MASTER_KEY`。任务结束立即发送原任务通知，不等待接管或额外调用模型；注册时本地规划符合 fail/error/waiting_human 准入的 action（deferred 排除）并去重。需要新模型复查的站点在原始脱敏消息前添加「助手复查中，结果稍后发出。原任务结果：」，复用则直接包含有效历史结论，不发第二条调查通知。事件、渲染正文和通道目标冻结加密保存为 ready；原任务自定义布局、数据库原消息、状态、统计和筛选不变，waiting_human 不改为 success。实际接管完成且有有效简短 completed_analysis/updated 结论时另发独立纯文本调查简报，不使用用户任务布局，HTTP 事件为 `assistant.investigation`。邮件/HTTP 各自的「接收助手调查通知」开关 `notify_assistant` 默认 true，只走已启用且开关开启的通道，不依赖原任务成功/手动筛选；读取或保存设置不调用模型。failed、unknown、revoked 或无有效结论不发，updated 只表示配置已保存、未重新运行验证。完成/正式结论/通知账本原子提交，原任务通知先完成发送尝试才派发调查通知；独立账本按 action ID 唯一登记，网络前认领、最多一次尝试，重启不明状态记 unknown、不重发，不保证恰好一次送达或收件顺序。旧版等待意图保留冻结事件/消息并释放一次，不回填历史任务，见[运行通知](run-notifications.md)。
- 数据库备份须同时包含 AppSetting 的 `assistant.takeover.settings`（规范授权 owner、修订、授权/生效时间）和 `assistant.takeover.site_grants`（同修订所选站点历史身份/origin）。owner、规范每日会话路由及绑定元数据不全加密，不能只备份聊天或从当前数字 ID 重建委托。新记录表由 `create_all()` 创建，live_access 和绑定使用现有 JSON/AppSetting，不新增旧表列；这不是已有列迁移机制，不提供历史开发版表结构迁移。

## 智能助手：凭据与浏览器隐私

- 单步手动凭据需绑定已保存同源站点；Bitwarden 可私下按新域名匹配，Cookie 观察可在同车道独立标签页人工登录，但完整测试要求目的站点实际保存 Cookie。2FA 仅用 `builtin.twofauth`，需凭据同意及凭据可用的同源 manual 绑定或 Bitwarden。**图片 CAPTCHA 的 OCR 文本/解题答案不一概视为秘密；真实 OTP/TOTP、密码、Cookie、Token、私有提示词及 API/提供商密钥仍是秘密，绝不索取或回显。** 用户“它不是秘密”可指图片验证码答案，不能据此公开真实动态码或凭据。配置的识别/视觉提供商可能处理图像，不能声称任何模型都不处理。验证码后可继续 observe/inspect、选择器检查、独立授权截图（含 UI 预览）和同页连续授权单步，**不锁浏览器、不要求重开，也不强制完整运行**；DOM/选择器证据仍省略输入值。截图遮罩主页面及可读同源 frame 内的输入控件，其余 frame 保守地整体遮罩；不保证所有秘密像素已清除，可见个人资料仍可能保留，页面反映的图片验证码答案可以保留。私有 Turnstile 单步限当前页唯一、可见且明确获信任的 Cloudflare challenge iframe，CFbox 非挑战坐标不触发动作，不开放任意全页坐标或桌面操作；私有回包回显已知提供商密钥时拒绝，输入在 focus 后复查并原子校验/插入。可用单步前后证据是结构化 DOM 而非截图，非 post 重试按当前类别策略及工具范围执行，重开不清除未知来源事实或账本。浏览器检查不是完整网络沙箱，仍有 DNS、service worker、弹窗及 GET 副作用风险，建议配置出站策略。
