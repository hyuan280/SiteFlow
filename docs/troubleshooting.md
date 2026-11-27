# 故障排除

先从「任务调度 → 运行详情」记录任务 ID、站点任务、失败步骤、时间、组件版本和脱敏日志。浏览器、模型和通知各有独立状态；任务结束、取消请求已发送或收到失败通知，都不能单独证明网站操作没有生效。涉及签到、购买或补签结果不明时，先只读核对网站状态与库存，再决定是否重新执行。

快速定位：[服务与集成](#服务部署与集成) · [车道与调度](#车道启动与调度等待) · [许可证](#保存许可证超时或结果未知) · [VNC](#vnc-已连接但黑屏) · [CookieCloud](#cookiecloud-拉取或域名匹配失败) · [模型](#模型调用失败) · [登录](#登录时序与误判) · [多标签等待](#多标签等待排查) · [智能助手](#智能助手中断权限或浏览器失败) · [通知与资源监控](#通知未送达与资源监控不可用)

## 服务部署与集成

- Web 无法访问：核对实际发布端口、`web` / `api` 容器状态及日志。HTTP 环境无法登录时，核对 `SITEFLOW_COOKIE_SECURE`；仅通过 HTTPS 访问时设为 `true`。首次登录见[管理员认证](authentication.md#2-初始随机管理员)。
- Worker 不可达：检查 API 实际使用的 `SITEFLOW_WORKER_URL`、Compose 网络和 Worker 健康状态。
- 代理站点失败：核对 `SITEFLOW_PROXY_URL` 或页面保存的代理地址、认证和连通性；系统不会回退直连。
- 2FAuth 失败：核对服务 URL、Token、步骤中的服务名与账户提示，确认能匹配唯一账户；不要自动选用其他账户。配置见[集成配置](configuration.md#3-2fauth)。
- 插件不可用：检查插件容器、注册、心跳及「插件管理」检查结果。连接故障不能靠反复审批解决；更新后按引用和影响提示修复不兼容配置。[插件系统](plugin-system.md)

## 车道启动与调度等待

- 车道无法启动：核对 API、Worker 与 Manager 的内部令牌和服务连通性。默认镜像使用内置 Chromium，无需许可证；检查镜像与 `CLOAKBROWSER_BINARY_PATH` 是否成对配置，有无旧卷覆盖内置路径。若使用需许可证的镜像，检查许可证、下载网络与缓存；Worker 接受 `keyless`、`free`、`pro`，拒绝 `none` 或未知等级。HTTP 402 表示 profile 容量被拒绝，403 表示许可证被拒绝。参见[部署与运维](deployment.md#manager-镜像与兼容边界)。
- 容量等待超时或手动启动快速失败：核对活跃任务、viewer、任务页、隔离清理及 Worker profile 预算。让自有任务结束并关闭不再需要的 viewer；不强停他人任务或删除 profile。`idle_stop_seconds=0` 只关闭定时停止，仍允许安全的压力停机；viewer 会保护对应 profile 不被停掉。
- 自动调度预检失败时不会创建 Run；后续轮询可再次检查。已经执行过业务步骤的任务不适用这条规则，不要因此重新派发。[调度预检](execution.md#scheduled-browser-admission)

## 保存许可证超时或结果未知

Manager 热保存涉及许可证检查及首次下载，可能需要数分钟，请等待结果；不会自动重试写入。PUT 已发送后超时、取消或响应异常，不代表 Manager 已停止处理。此时 Worker 会保留结果未知的粘性保护，`license.editable=false` 在 GET 刷新或重载页面后仍然有效；即使看到有效 tier/版本，也不证明原保存已完成。

先通过私有 Manager 状态/日志确认原写入和下载**实际完成**，不要公开密钥或服务商原始响应；随后等待任务排空、viewer 结束，再重启 Worker 清除保护。**不要为绕过仍在进行的下载而重启，不要在结果核实前重复提交，也没有自动重试。** 未修改环境变量时按实际部署方式二选一：

```bash
# 仅在 Manager 完成且任务/viewer 排空之后，GHCR 部署
docker compose restart worker
# 源码部署，二选一
docker compose -f ops/docker-compose.yml restart worker
```

重启后刷新卡片，核实状态再决定是否需要手动保存；如果同时改了环境变量，必须改用[许可证配置](deployment.md#配置许可证)中的重建命令。

## VNC 已连接但黑屏

黑屏但状态显示「已连接」：

1. 打开浏览器控制台，检查 `.vnc-screen canvas` 的 `width/height` 是否为 1920×1080；如果是 300×150，优先检查 RFB 绑定的元素或弹窗初始化时机。
2. 确认 `VncViewer` 使用的是 `<div ref="screen">` 容器，而不是 `<canvas>`。
3. 确认 WebSocket 只建立一次；重复连接会互相抢占同一个画布。
4. 检查 API 日志是否出现 RFB 消息；如果开启了相应诊断日志，可核对 ServerInit 和 FramebufferUpdate。

## CookieCloud 拉取或域名匹配失败

| 现象 | 检查方式 |
| --- | --- |
| `get_decrypted_data()` 返回 `None` | 先用 `cloud.check_connection()` 检查端点是否可达。预检失败时检查服务器 ID、反向代理和部署版本；预检成功后仍失败，核对 UUID 和加密密码。 |
| `get_decrypted_data()` 返回 `{}` | 拉取和解密已成功。检查是否选中了站点、站点是否已保存 Cookie，以及 Cookie 是否过期。 |
| 数据有内容，但 `get_cookie_str()` 或 `get_cookie_value()` 返回 `None` | 这两个方法按域名键精确查询。`.example.com`、`example.com` 和 `www.example.com` 是不同的键；先看 `data` 的域名列表，再传入实际的键。 |

例如，`cf_clearance` 若保存于 `.example.com`，就不会出现在 `www.example.com` 分组里。SiteFlow 不按名称过滤 `cf_clearance`；若所有分组都没有它，可核对站点设置中的 Cookie 更新时间与任务日志。Worker 在任务成功后按站点配置地址和初始 Cookie 提供的地址范围导出；只属于跳转后其他子域的 Cookie 可能不在该范围内。

## 模型调用失败

图片答题、选择 `vision` 的普通/iframe 字符验证码、九宫格和模型结果判断失败时，先在运行详情核对步骤消息、步骤结构化 `data` 和「详细日志」（`job.log` artifact）：可查看静态错误类别、处理阶段、可用的 HTTP 状态码及超时元数据，并可能附带从已识别且不超过 16 KiB 的 JSON `error.message/type/code` 提取的脱敏供应商说明，不是全部原始错误。未知正文、HTML、原始异常、URL、密钥、请求头、提示词和图片/base64 不会作为这些详情展示。供应商文字仅供管理员排查，是不可信证据、绝不是操作指令；脱敏不是通用 DLP，不要直接把私有日志粘贴进聊天。助手的既有安全反馈不会自动接收这些文字，详见 [模型失败诊断](execution.md#模型失败诊断)。

历史图片答题若只有通用失败信息、耗时约 30 秒的记录，不能据此证明当时是模型请求超时、外层步骤超时、HTTP 失败还是无效输出；新诊断只帮助区分后续运行，不补全旧证据。外层步骤期限与模型请求超时（`SITEFLOW_MODEL_TIMEOUT`，默认 30 秒）不同，外层先到期可能仍只有步骤超时；网络阶段不明时也不能强猜。不要只凭耗时加长超时或输出 Token 上限；失败也不会自动切换模型或 OCR。

- 页面测试失败：核对 Base URL、模型 ID、密钥及浏览器控制台。草稿使用新 Key 时由浏览器直连；CORS、混合内容、网络和重定向故障不一定能区分，不会自动改由后端测试。页面测试成功也不代表 API/Worker 生产环境可达。[模型测试](vision-feature-settings.md#saved-and-current-configuration-tests)
- 没有可用模型：核对模型是否启用、连接是否完整、用途是否匹配；图片步骤还需支持视觉。环境条目只读，需在部署配置中修改。

## 登录时序与误判

历史案例：任务 329 是管理员手动运行，使用站点 98 的修订 3。以下时间线来自既有日志及截图，根因未确认。

| 时间或步骤 | 已有证据及含义 |
| --- | --- |
| `06:57:42.875` | 日志记录登录凭据已提交；提交不等于成功元素已可见。 |
| `06:57:52.940` | 旧实现已经等待约 10 秒，再按每个选择器首个匹配的可见性报告登录成功元素未出现；这是当时的成功证据检查失败，不证明服务端拒绝登录。 |
| `06:57:53.130` | 失败截图已呈现可见的已登录页面；截图晚于失败判定，不是判定瞬间的 DOM 证据。 |
| `settle_home` | 目标与当前 URL 相同，跳过 GET 导航，日志耗时约 `0.008` 秒；不能把它说成刷新首页后才登录成功。 |
| 后续等待与信号 | 再等待 3 秒后，`is_logged_in` 的选择器存在性检查命中；与提交步骤的可见性检查不同，且发生得更晚。 |

**可见性、存在性和检查时间必须分开解释。** 旧成功检查只看每个选择器的首个匹配是否可见；首个隐藏、后续可见是可能的误判模式，SPA 成功元素晚于等待期限渲染也是可能模式。稍后的信号只需元素存在，隐藏元素也能命中，因此它与较早的可见性失败并不矛盾。现有证据没有当时的 `match_count` / `visible_count`，无法确定任务 329 究竟属于隐藏首匹配、延迟渲染还是其他情况；不能补写数量或把某一种解释当成定论。截图能显示已登录的视觉状态，**不能证明具体 DOM 属性、选择器匹配身份、匹配顺序或可见性判定值**。

`auth.form_login.success_wait_seconds` 保持默认 **10 秒**，可配置为严格整数 **1-120**；任务 329 已经等待约 10 秒，不能把延长到 30 秒当作根因修复。`timeout_seconds` 必须另行覆盖凭据/秘密查询、定位、填写、点击和成功等待，不会因调高等待而自动延长；助手单步及轮次的外层上限仍有效。案例中的步骤总超时已经设为 **120 秒**。

提交后只读轮询主页面及**可见同源 iframe**，所有祖先 iframe 须可见且同源，`about:blank` / `srcdoc` 沿父链继承来源，不使用凭据 `trusted_urls` 扩大成功证据范围。检查各成功候选的多个匹配，任一匹配可见即可，不限于 `.first`；每轮最多 100 个 frame、每个选择器在各 frame 中最多 100 个匹配，共用一个等待期限。隐藏节点单独存在不通过；`success_selector` 留空只表示已提交、不证明登录成功。等待不自动刷新、导航或再次提交，超时也不是可安全重试的证明。这处理隐藏首匹配等通用模式，但不能证明任务 329 的具体 DOM 原因，亦不代表已在生产复测成功。

超时后的诊断原因和匹配/可见计数是**超时后才采集的有界只读样本**，不是整个等待期间的历史记录，也不是同一瞬间的原子快照；采样间 DOM 仍可能变化。选择器索引按原步骤 `config.success_selector` 的配置索引（从 0 开始）核对，不是某个 DOM 匹配的序号；诊断不回显选择器值、表单值或凭据。即使稍后采样已有可见匹配，也不能证明等待期限内曾可见；采样仍无匹配也不能证明之前从未出现。

诊断 `reason` 中，`visible_after_timeout` 表示采样时已有可见匹配，`matches_hidden` 表示采样读到匹配但未读到可见匹配，`selector_missing` 表示本次采样范围未读到匹配；`frame_scope_excluded` 表示有 frame 因范围限制被排除，`read_incomplete` 表示读取错误或达到边界，不能按完整零匹配解释。`selectors[].selector_index` 对应原配置索引；结合 `checked_count`、`read_errors`、`truncated` / `evidence_truncated` 和各 frame 的 `scope` / 静态错误码核对，不把这些原因当作任务 329 的历史根因或新的登录成功判定。

工作流信号的 `include_selectors` / `exclude_selectors` 默认只判断存在，隐藏元素也可能命中；需要可见证据时可显式写 Playwright `:visible`，不自动追加、不改写已有配置或新增数据库列。`include_texts` / `exclude_texts` 则读取页面标题和各 frame 的 body `innerText`，通常不含 body 内隐藏后代文本，不应套用选择器存在性语义。助手 CSS 检查工具不支持 Playwright `:visible`，核验时使用其 `match_count` / `visible_count`，不能把零可见当作零匹配。

## 页面导航、选择器与站点提交

- 页面导航偶发 `net::ERR_CONNECTION_CLOSED` 或连接重置：先核对网络、站点和代理。对于确认可重复访问、没有副作用的页面，可将导航步骤的「失败重试次数」设为 `2`（最多尝试 3 次，默认 `0`）。带签到、购买等副作用的地址不要启用。[任务执行](execution.md#4-已支持功能块)
- 关闭引导弹窗后又出现相似弹窗：检查是否先后出现了不同引导，例如 `data-tour-root="shell"` 与 `data-tour-root="home"`。分别限定选择器和出现/消失条件；不要用宽泛的 `button.ghost` 或反复点击来补偿误判。
- 验证步骤成功，但提交后出现 `Captcha verification failed`：查看提交后的截图和日志，确认站点是否接受验证、是否已换题。别仅凭组件仍显示就判失败，也不要无限重试。
- 登录或 2FA 报 `Secret origin denied`：核对页面真实地址和站点同源设置。确需跨域登录时，在站点编辑器审核并保存范围尽可能窄的可信网址；助手的观察来源不授予凭据权限。[跨域登录与可信网址](usage.md#跨域登录与可信网址)
- 购卡或补签未能核验结果：检查失败步骤所用的库存、目标日期和完成标记。库存显示初始 `0` 时先确认页面已就绪；弹窗补签显示日期按钮缺失时，核对是否有明确的就绪或合法空列表证据。仅点击完成或见到旧成功提示都不是成功证明。先确认网站状态，避免为补日志重复消费。[执行边界](execution.md#购卡与补签的执行边界)

## 多标签等待排查

先区分业务执行、输入等待和资源清理，不要仅凭 VNC 中某个标签页暂时未动就认定整条通用车道串行：

1. 核对任务/车道并发额度及当前步骤；完整输入动作期间其他标签页的输入或焦点切换仍可能等待。人工 VNC 输入也可能干扰焦点，不保证与自动操作协调。
2. 尚未开始步骤时，查看详细日志的 `browser_setup_cookies`（仅有待注入 Cookie 时）、`browser_setup_new_page`、`browser_setup_focus` 及进度 artifact。每个设置阶段最多等 10 秒；失败使用静态 `browser_setup_timeout` / `browser_setup_failed` 和 `stage`，不要从空步骤列表推断没有申请浏览器资源。
3. 出现 `Browser lifecycle pending` 表示原生创建/焦点调用仍未结算。输入门的 10 秒预算包含等门和原生调用；超时释放锁不等于允许不安全地继续输入，后续输入、创建与焦点切换会失败关闭。读取与关闭清理仍可进行。已经派发的 `new_page()` 不因任务超时/取消而丢弃，晚返回的页面仍归原任务清理；也可能持续阻塞该 context 后续输入。
4. 查看 `browser_cleanup_quarantined` / `browser_cleanup_released`。自有页面先原生关闭最多 3 秒，未确认时再对该页精确 target 做最多 3 秒 CDP 关闭；未确认则保留租约，后台每轮后等 5 秒重试。不会为清理一个任务关闭其他任务页、切回欢迎页或关闭共享浏览器。任务可已结束、执行额度可已释放，但车道 `in_use` 仍非零，不能当作没有资源占用。
5. 业务已完整结束时，结果在可选 Cookie 导出之前就已交接保留，而非等导出后才保留。导出为只读查询，使用 `JobPages.setup()` 的 10 秒预算；`cookie_export_timeout` / `cookie_export_failed` 仅为静态警告，不改业务结果，成功导出才补充 Cookie 并刷新保留结果。成功 Claim 不因随后的导出或清理超时、取消、断连而改成 worker 业务失败；成功与清理隔离可以并存。旧数据库记录不会自动修正，父运行的显式取消状态也不因此撤销；未完成的工作流不适用此完成保证。不要为了清理日志或补成功证据自动重跑签到、购买或补签。

`SITEFLOW_EXECUTION_TIMEOUT` 默认 600 秒。它限制 API 到 worker 的 HTTP 等待，流式执行时主要是无数据等待，不是整个任务或每个步骤总时长。增加它不能解除输入隔离；步骤 `timeout_seconds`、10 秒设置/焦点预算和关闭预算各自独立。

若远端已创建目标却始终没有返回页面/target 身份，系统不能安全证明没有遗留，也不能通过关闭任意标签页找回它；即使本地取消或关闭服务，未确认租约仍需保留，可能需要管理员在不干扰其他任务的维护窗口核查后端/profile。关闭服务使用保留的 shielded shutdown task，调用方在前置清理中取消也不会跳过后续 teardown；原生输入门子任务按原任务 owner 跟踪。关闭期间或之后晚返回的页面仍归原 owner，执行一次有界关闭，仅确认后释放租约，不重启任务或无限 reaper。这不保证远端所有未决操作自动消失。不要强行清零 `in_use` 或把等待结束当作全部恢复。实现与残余边界见 [Cancellation and Owned Pages](execution.md#cancellation-and-owned-pages)。

## 智能助手中断、权限或浏览器失败

遇到中断先「刷新会话」核对状态和工具结果，再决定是否发送新消息；这不是刷新网站或重试步骤。`browser_reopen_required` 需请模型重新打开，`target_confirmation_required` 需让助手发起目标交互并由用户在 UI 确认；`browser_trust_required` 需核对完整 URL 与 `target_origin`，只有用户明确要求变更来源后才用 `update_site_config` 审核完整观察来源列表，不主动扩权或换通道绕过。`redacted_url_not_usable` 表示脱敏证据不能当作工作配置。持续 `browser_unavailable` 应检查配套 Worker 版本、交互车道占用、代理及 Worker 本地 DNS。`credential_binding_changed` 需重新核对绑定；`operation_already_used` 不可复用 ID，精确重试用原 ID 的 `retry_step`。`outcome_uncertain` 保留未知来源事实，当前非 post 单步按类别策略；`retry_authorization_required` 应核对当前宿主工具范围与绑定，不因旧文字声称必需人工审批。`auth_budget_exhausted` 等待真实新用户轮次，不换目标/浏览器重置。`screenshot_consent_required` 需独立图像同意，`model_vision_required` 需支持视觉的本轮配置，不能据此推断登录失败。不要粘贴凭据排障；已知 Key 检测不是通用 DLP，请自行审核请求、DOM、链接和输出。

浏览器打开失败的工具结果返回安全的 `code`、`lane_key` 和 `reason`，例如 `code: "browser_unavailable"`、`lane_key: "proxy_interaction"`、`reason: "browser_dns_denied"`。按实际返回值排查，不把 Worker 原始异常、响应正文、凭据或未净化 URL 发送给模型：

| `reason` | 排查含义 |
| --- | --- |
| `browser_dns_denied` | Worker 本地 DNS 解析失败或解析结果不符合公共地址限制；代理车道也受此限制，不证明需要代理。读取失败若 `page_retained: true` 则无证据但保留页面，不是登录失败结论 |
| `browser_credential_endpoint_not_approved`（旧结果） | 旧端点策略诊断，不是缺少站点 ID、Bitwarden 故障或需网站专用适配；当前实现不要求逐表单批准 POST action |
| `browser_url_denied` / `browser_redirect_denied` | URL 或导航/重定向违反公共地址、原始源执行等边界；不要绕过限制。可供用户确认的公共跨源候选另以 `browser_trust_required` 返回 |
| `browser_lane_busy` | 所选交互车道占用或获取容量超时；核对该车道，不是通用车道 |
| `browser_lane_unavailable` | 所选车道租约、启动或浏览器上下文不可用；检查该车道及 Worker 配套版本 |
| `browser_access_denied` | Worker 返回 403，但未提供可识别的更具体安全原因；不能据此断言 DNS、代理或车道停止 |
| `browser_response_interception_failed` | CDP Fetch 响应拦截处理失败；仅保留静态阶段 `response_event`、`continue_response` 或 `fail_request`，不包含原始异常或秘密 |
| `browser_session_unavailable` | API 收到 Worker 404 的安全归一化原因，不再笼统归为 `browser_worker_error`；不是重启、代理或 URL 策略故障的证据 |
| `browser_closed` / `browser_session_expired` | 已关闭或租约到期；不证明之前的网站操作失败或没有副作用 |
| `browser_observation_unavailable` | 本次观察不可用，不等于页面没有登录表单、验证码或站点问题 |
| `browser_cookies_invalid` / `browser_cookie_import_failed` | 私有 Cookie 种子校验/导入失败的静态原因；不要把 Cookie 原文交给模型排查 |

### 生命周期诊断

`browser_open` 或轮询返回 `ready` 只证明当时就绪，不保证页面在后续异步响应拦截期间仍存活。Fetch 失败仍 **fail closed**：隔离并清理本会话页面、CDP 和租约，撤销 ready 状态，不自动重试，也不放宽请求方法、同源、URL/DNS 或授权策略。安全结果保留 `browser_response_interception_failed` 及上述阶段；不记录或回传原始 CDP 异常、响应头/正文、Cookie、令牌或未净化 URL。

关闭后，在 Worker 保留的有界 tombstone 窗口内，仍先验证能力令牌：正确令牌可取得已知静态关闭原因，未记录具体原因时返回 410 `browser_closed`；错误令牌仍只返回通用 404，不泄露关闭原因。tombstone 过期后不保证原因仍可查。API 将 Worker 404 归一化为 `browser_session_unavailable`，并只透传允许的静态原因/拦截阶段，不把未知响应正文当诊断。

来源任务的认证选择器失败，与接管期间实时浏览器失效是两件事，应分别说明证据。若生产现场的精确异常未记录，只能报告原因未知；不能由通用 worker 错误、会话消失或拦截阶段断言 CDP 不支持、需要代理或 URL 有错。无法观察也不能据此排除验证码或站点自身问题。发现流程中的使用方式见[URL 证据与跨源确认](assistant-discovery.md#url-证据与跨源确认)。

此前生产环境观察到两次 `browser_open` 返回 403；没有证据表明由 `general` 停止导致，具体根因尚未确认。该历史故障没有生产复测结论。缺少具体原因时应明确原因未知，保留安全诊断供排查。

Cookie 清除是上述重开建议的例外：`clear_bound_site_cookies` 返回 `browser_reopen_required` 时只说明缺少 READY 原始绑定浏览器，不自动 `browser_open`。`cookie_binding_changed` 需重新核对显式绑定，不能改用同主机其他站点；`cookie_restore_unavailable` 不能通过把原始 Cookie 交给模型解决。generation 握手失败现在保留安全的 `reason`、`lane_key` 和 `stage: "cookie_restore"`，握手失败不解密 Cookie；私有导入的 `browser_cookies_invalid` / `browser_cookie_import_failed` 也保留静态原因，而非一律丢失为通用 worker 错误。清除后的部分/未知结果及同 ID 重放按[绑定站点 Cookie](ai-assistant.md#绑定站点-cookie)处理，不套用 `retry_step`。

- `assistant_settings_conflict`：保留草稿以便核对，刷新设置并按最新内容重新编辑；不要直接重发覆盖。`assistant_settings_invalid` 表示持久化设置需要管理员检查，恢复默认也不会覆盖损坏记录。
- `[context_limit]`：缩小观察或历史读取范围，或按[助手上下文设置](ai-assistant.md#助手设置)检查预算。输出上限与上下文预算是不同设置。
- 接管未发生或复用了结论：检查是否启用接管、是否覆盖该站点，来源任务是否符合生效时间和状态；再看审计记录是否复用同版本同故障的有效结论。见[失败接管](ai-assistant.md#失败接管)及[相同失败去重](ai-assistant.md#相同失败去重)。

## 通知未送达与资源监控不可用

- 邮件连接超时：核对 SMTP 端口与加密方式（常见 465 用 SSL，587/25 用 STARTTLS），确认云主机或 ISP 没有阻断该出口。
- 未收到通知：先用页面测试检查连接，再核对通道启用状态和手动/成功筛选。助手调查另需开启对应通道的调查通知开关，并确实产生可发送结论。通知失败不改变任务结果；结果未知的调查通知不会自动重发。[通知设置](notifications.md) · [运行通知](run-notifications.md)
- 只有主机指标或容器拆分不可用：检查是否启用了 Docker socket 的 Compose 覆盖、容器连接权限、daemon 状态及 Docker stats 是否可采样，再核对实际项目标签和项目名。旧版 API 只有主机指标，不能据此认定 socket 已连通；不可用也不等于占用是 `0`。[系统资源监控](system-resources.md)
