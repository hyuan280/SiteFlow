# 从页面证据发现登录与签到配置

本文配合 [AI 助手](ai-assistant.md) 和 [草稿校验](workflow-drafts.md)，说明如何把单步观察转为人工审核的工作流，并在独立授权后导入、完整测试及根据安全反馈修正。示例是 [nexusphp-assistant-workflow.json](examples/nexusphp-assistant-workflow.json)，不是对某个网站的执行记录或通用适配保证。本文更新未执行生产测试或目标网站请求；仓库中的真实 JS/Chromium 本地 fixture 测试也不等于实际 Fengchao 登录已验证。

## 示例来源

经用户授权，通过运行中 API 容器的 Python 应用服务，只读检查生产库的 `NexusPHP站点登录签到`（本次库中 ID 3）及其父模板 `NexusPHP站点登录`（ID 2）。数据库连接使用 SQLite `mode=ro` 和 `query_only`；未启动应用、保存模板或运行工作流。ID 仅说明本次来源，不能用来绑定其他部署。

读取输出只投影需要的功能、条件、信号和配置白名单，不输出完整模板快照、凭据或运行日志。配置中的密码、用户名值、令牌、输入值、静态表单字段等均不应进入文档；`username_selector` / `password_selector` 是元素定位器，`username_field` / `password_field` 是 schema 字段名，不是账号值。`input[value='立即签到']` 中的 `value` 是公开按钮文案，不是读取用户输入值。

本次只读配置中发现的候选证据如下，**不是本次浏览器实测证据**：

| 用途 | 来源中的候选 |
| --- | --- |
| 登录正向文本 | `欢迎回来`、`歡迎回來`、`控制面板` |
| 登录排除文本 | `请登录`、`限制只能使用本IP登录`、`Sign in` |
| 登录表单 | JSON 中列出的用户名、密码与提交按钮选择器 |
| 登录后元素 | `a[href*='logout.php']`、`a[href*='userdetails.php']`、`#info_block` |
| 图片验证码 | `img[alt='CAPTCHA']`、`img[alt='图片验证码']`、`img[src*='imagehash']`；输入框 `input[name='imagestring']` |
| 登录/签到路径 | `{base_url}/login.php`、`{base_url}/attendance.php` |
| 签到入口 | `input[value='立即簽到']`、`input[value='立即签到']` |
| 今日签到文本 | `今天已经签到`、`今天已经簽到`、`签到成功`、`簽到成功`、`签到已得`、`簽到已得`、`已签到` |
| 签到排除文本 | `每日签到`、`签到得魔力`、`簽到得魔力` |

示例将继承链展开为独立 `TemplateWrite`，名称为 `NexusPHP 登录签到示例`，`parent_id: null`，默认 `enabled: false`。只包含本地 `steps`、`groups`、`signals`，不带站点、历史版本或生产步骤 ID；步骤键可读且稳定。源配置中的 `action.click` 被改写为 `sign.button`，复用实际候选按钮选择器并显式配置成功文本；这属于示例设计，不是声称源模板使用该功能。

**购买卡片与补签步骤已完全删除，不是默认行为，也没有留下禁用步骤。** 示例不包含 `post.*`、补签系统信号、任意填充值或真实站点主机名。绑定站点时还应人工关闭站点自己的 `support_resign`；它是 `SiteWrite` 的设置，不属于 `TemplateWrite`。

## 先观察再执行

1. 直接新建会话，在聊天中收集站点基础 URL、名称、账号来源、代理偏好及目标是否仅为当日签到，不要求用户预填目标设置。发送即同意将消息、有限历史及已授权观察结果交给模型，无发送前勾选。使用 `check_site` 检查是否已配置；需要时用 `list_templates` / `get_template_config` 或 `list_sites` / `get_site_config` 查看脱敏配置。已有配置只能提供线索，不能当作目标站点已经验证的选择器。
2. 助手用 `request_user_interaction(request={purpose: "target", target: {...}})` 提出目标候选。输入框上方的宿主面板只以可读字段展示网站名称与完整公共地址，不展示原始 JSON；认证方式、同源凭据绑定 `site_id` 与独立配置目的 `workflow_site_id`（空为新建）保留在私有载荷中。用户明确确认后原调用恢复，不需再发消息。目标确认只授权浏览观察，不授予业务执行；即使开启全局授权默认值也不能跳过。新目标代理关闭，保存运行车道默认 `general`。仅在用户明确要求对应设置变更后，才用 `update_site_config` 独立审核精确差异，不从 DOM、浏览失败或模型建议推断需要代理/信任。额外可信来源最多 20 个规范公共 HTTP(S) origin，不含路径、查询、片段、用户信息或正则，仅授权观察/导航，不扩大执行或凭据范围。聊天、DOM、模板和工具参数均不能授予权限。
3. 调用 `browser_open`、`browser_observe`，保留登录前快照。`browser_open` 打开宿主确认的完整 `base_url`；`browser_navigate` 除 `title` 外必须且只能提供 `path` 或完整 `url` 之一，后者限原始源或用户确认的额外源。`proxy=false` 固定走 `interaction`，`proxy=true` 固定走 `proxy_interaction`，车道租约会按需自动启动所选车道，不走 `general` / `proxy_general`；通用车道停止不说明助手无法打开。已出现可信账户/退出标记时跳过登录；表单已可见时不必强制访问 `/login.php`；只有实际发现并确认可安全读取、且来源获批的地址才导航，保留非敏感查询与片段。示例中的路径不代表每个 NexusPHP 站点都存在这些页面。
4. 从当前表单的扁平 DOM 证据提取用户名、密码、提交元素候选，用 `parent_id` 还原层级，并用 `browser_check_selector` 在对应 `frame_id` 实时检查数量、可见性和样本身份，再核对表单归属与目标源。无 ID 按钮也可使用采集时已验证唯一的 `dom_path`，不必猜 ID；具体见下一节。先 `list_features`，再为每个准备使用的功能查询 `get_feature_schema`，只使用目录中实际存在的字段。不要根据模板名称猜测选择器，也不要保留目标上未核实的宽泛 fallback。
5. 需要单步测试登录时，缺少权限的 `execute_step` 由宿主暂停并请求统一执行授权，批准后原调用继续；有已保存目标权限或用户明确勾选的 `auto_authorize_execution` 时可复用。宿主冻结并展示当前支持且凭据来源允许的单步清单，批准只授予这份清单，不逐功能重复确认；可选 `feature_type` 仅说明触发功能。同轮拒绝、超时、取消或失效覆盖整个执行能力，不能换功能重问。真实登录、签到、点击及挑战可能产生费用或其他副作用。凭据来源必须可用：手工存储凭据需完全同源且已保存凭据的站点；未绑定的 `manual` 目标仍可浏览和非认证测试，不能自动登录，应在站点管理保存凭据后通过目标交互绑定，或改选 Bitwarden。Bitwarden 使用目标完整域名匹配。API 私下解析凭据并交给 Worker，模型不得请求、直接填写或看到账号密码；`cookie` / `none` 不提供表单登录凭据。
6. 对比单步返回的 `before` / `after`，再做安全的 `browser_navigate` / `browser_observe` 检查登录状态是否保持。需要图像时，本轮支持视觉的模型调用 `browser_screenshot`；若缺少 `screenshot_consent` 且未明确开启全局截图默认值，宿主自动请求独立图像授权，批准后恢复同一调用，无需预先打开设置。已有截图权限及可用浏览器时，也可空闲点 UI「截图预览」，不要求视觉模型。`submitted: true`、工具 `ok: true`、按钮消失、单个通用欢迎词或截图转圈都不等于认证成功/失败。返回的静态日志不是原始运行日志，也不是成功证据。
7. 形成 `is_logged_in`：使用仅登录后出现的正向元素/文本，并加入仅未登录时出现的排除元素/文本。目标页面若始终含“请登录”或隐藏导航等文案，要重新检查实际信号检测语义，不能盲目照抄。`auth.form_login` 没有 `success_when_missing` 字段，不能把其他功能的字段拼进来。
8. 对签到重复这个过程：先观察今日已签到与待签到状态，仅在明确未签且当前目标授权覆盖该操作时，才考虑一次操作。统一授权自动派生签到、`action.click`、`challenge.altcha`、`challenge.slider` 所需的副作用权限，不要求额外勾选；旧目标不能据此自动扩大原有授权。即使是 GET 导航也可能签到；访问 `/attendance.php` 前必须先判断副作用和授权，不能为了“观察”无条件访问。
9. 比较签到前后证据，建立 `is_checked_in`，确认它针对当前账户的今日状态，而非历史记录、页面标题或其他用户。如果已签就跳过提交；结果不明先观察，不得换 UUID 自动重试。用户明确要求重试时，模型调用原操作 ID 的 `retry_step`，由宿主暂停同一调用并通过 SSE 显示运行中审批；owner 核对并批准后立即恢复冻结原步骤一次，不需再发消息。模型不应因旧 `no_retry` 文字拒绝请求或要求批准后另发消息。最终只有充分证据才输出成功，其他情况停止或交给人工。

发现浏览器的 `proxy` 字段只决定实际基础设施车道 `interaction` / `proxy_interaction`，完整任务另用 `run_lane_policy`。新目标的保存运行车道默认 `general`，旧/API 默认 `auto` 也解析为 `general`。用户明确要求后，用 `update_site_config` 的 `observation.proxy` 修改观察代理，用 `patch.lane_policy` 修改已保存目的站点的运行车道；后者支持 `auto`、`general`、`interaction`、`proxy_general`、`proxy_interaction` 全部五项，二者独立审核，不因失败自动切换。导入前只能以空 patch 修改观察设置，不创建站点。

保存车道选择不会把发现浏览器搬到通用车道：其实际仍为 `interaction` / `proxy_interaction`。完整任务的正常 `general -> interaction` / `proxy_general -> proxy_interaction` 同代理模式回退仍允许，不是配置漂移，也不意味着可自动切换直连/代理模式。旧 `purpose: "proxy"` / `purpose: "trusted_origins"` 仅保留既有持久化兼容行为，不再推荐新调用。

设置批准后原调用只应用冻结差异，不自动导航、导入、运行或重试；旧浏览器释放后需安全重开，旧提案须按当前目标修订重新校验提交。`auto_authorize_execution`、`auto_authorize_screenshot`、`auto_authorize_workflow` 是全局独立默认值，均为 false，只有用户明确勾选并保存才开启。执行默认值只控制浏览器单步；第三项同时覆盖工作流导入及绑定站点完整测试，不是仅保存，也不依赖单步开关。它们不跳过目标确认，**任何设置写入仍须真实用户批准本次精确差异**，不授予秘密输入或额外来源凭据权限。已有显式目标/会话权限保留，关闭默认值不撤销已授予权限。

`update_site_config` 使用稳定 `operation_id` UUID、可选 `patch` / `observation`（至少一项非空）及仅启用时可选的 `successful_run_id`，另有公共 `title`。patch 仅允许 **10 项**：`name`、`trusted_urls`、`enabled`、`lane_policy`、`support_resign`、`resign_max_days`、`checkin_after`（null 清除）、`timezone`、`tags`、`notes`；observation 仅允许 `proxy` 与替换完整列表的 `trusted_origins`。不接受任意站点 ID、`base_url`、模板、提供商、凭据/Cookie 或 `steps` / `groups` / `signals` 数组。目的地只能是已确认的 `workflow_site_id`，不是凭据源 `site_id`。站点设置面板用可读字段逐项展示变更前/后及风险，不用原始 JSON；`trusted_urls` 精确正则扩大的是本站点凭据/TOTP 使用范围，不是观察信任，宽泛规则可能泄露凭据。

设置工具复用既有最多 **180 秒**的同调用等待，实际用户等待暂停活动预算，批准处理仍计入。准备时冻结目标绑定、历史身份、配置修订和设置，批准后原调用复查并 CAS 写入；变更历史 actor 为 `assistant`，与加密 action 回执及更新后的绑定在同一事务提交。拒绝、超时、取消、重启、绑定漂移或复查失败不写入；相同 UUID/请求只重放结果，不重复写入。完整字段与边界见 [站点设置变更](ai-assistant.md#站点设置变更)。

交互面板支持授权、单选/多选和非秘密文本输入；通用 `purpose: "question"` 的答案不授予任何目标权限。每次最多等待 **180 秒**，独立于本轮剩余活动时间；活动预算已耗尽则不弹出。仅实际用户等待暂停活动计时，等量顺延共享截止时间及外层超时，不重置默认 180 秒活动预算；审批处理、网络请求和批准后的执行仍计入，提供商单请求超时、单步上限及浏览器 TTL 不变。停止及关闭服务仍立即取消。拒绝、超时、取消、失效后同一轮同一 subject 不可重复询问，不能换措辞或拆分请求绕过；缺少必需授权或必填信息时停止依赖操作。批准后原调用继续，不需另发消息。没有密码、Cookie、Token、OTP/TOTP 或验证码输入入口。页面标题操作区和输入区固定，时间线独立滚动；安全提示关闭状态仅存在本浏览器 localStorage，不改变任何宿主授权。

没有单步执行授权时，助手观察标签页不能提交登录表单。用户可在同车道独立人工标签页完成登录，然后要求助手重新导航并观察；不能要求模型绕过权限。挑战能力不支持、证据不充分或模型/Worker 版本不配套时，先停止，不假定登录成功。

### DOM 分页与选择器核验

观察不是只收集可见交互元素的白名单：普通 HTML、自定义元素、隐藏节点和 open shadow DOM 都可进入有界快照。`elements` 是扁平数组，每项有 `node_id`、`parent_id`、`frame_id`、`tag`、`visible`、过滤后的 `attributes`，可选直接子 `text` 和选择器证明。文本不聚合后代，按钮标题可能在子 `span` 中。隐藏节点标记 `visible: false`，不等于不存在；隐藏 input 节点可以保留，但不返回其值。输入/可编辑正文、script/style 等正文、value/敏感属性被省略或脱敏，不读取 Cookie/存储；普通隐藏文本及可见个人资料仍须审核。规则通用于页面结构，没有特定站点或挑战的专用识别保证。

每次扫描最多 5000 节点、深度 64、2 MiB，每浏览器缓存最多 4 份快照。普通分页含完整 JSON 最多 32 KiB、100 节点，字节上限可能让第一页少于 100 个。以下是**假设响应的分页示例，不是本次实测记录**；调用仍须由助手工具发出，`frame_id`、快照 ID、节点 ID 及 offset 必须来自实际结果：

```json
{"title":"采集主页面 DOM","offset":0,"limit":100}
```

若 `browser_observe` 返回 `frame_id: "main"`、`offset: 0`、`next_offset: 37`、`total: 142`，下一次传实际返回的 37，不是 100：

```json
{"title":"继续读取同一 frame 的快照","frame_id":"main","offset":37,"limit":100}
```

非零 offset 读取该 frame 最新的缓存快照，要核对两次 `snapshot_id` 相同；`offset: 0` 会重新采集。若需锁定原快照，假设其 ID 为 `4b030fd3-e0f2-467b-84b1-6304a468df39`、文档根节点为 `n0`，可用 `browser_inspect` 分页该根的已捕获子树：

```json
{"title":"固定快照继续读取文档子树","snapshot_id":"4b030fd3-e0f2-467b-84b1-6304a468df39","node_id":"n0","offset":37,"limit":100}
```

inspect 包含根节点，其 offset/total 相对于指定根的捕获子树；检查某个表单时须换成实际表单的 `node_id`，并从 offset 0 开始。它只读不可变的 **`captured_subtree`**，不访问实时子树，不能找回扫描数量、深度或字节上限之外的节点。`total` 只是已捕获数量，`next_offset` 仅表示缓存还有下一页；`truncated: true` 表示采集/元数据不完整，即使下一页为 null 也不能声称整页读完。重新 observe 不保证能扫描到原先遗漏的深层子树。导航、重载或缓存淘汰后出现 `browser_snapshot_stale`，应重新观察，不重用或猜测节点引用。

先查看 `frames` 中的实际 `frame_id`、`readable`、净化 URL 及不可读时明确的 `reason`；最多 64 个 frame / 12 KiB 元数据，注意 `frames_truncated`。同源或已批准来源且通过公共 URL/DNS 检查的可读 frame 才能观察，子 frame 要显式传 ID；不可读/未批准 frame 只有元数据，**不是其内容、登录表单或挑战不存在的证据**。frame 观察不授权 iframe 操作或凭据使用。open shadow root 可使用显式 CSS ` >>> ` 分段，closed shadow root 不可读；不会自动穿透 iframe。

典型登录表单中，用户名和密码框有 name，但提交 `<button>` 可能没有 id/name。若实际观察的按钮节点包含下列字段，就已有定位证据，不应回答“没有 ID 所以无法定位”，也不能自行编造 `#login` 或通用 `button[type=submit]`：

```json
{
  "node_id": "n12",
  "parent_id": "n7",
  "frame_id": "main",
  "tag": "button",
  "visible": true,
  "attributes": {"type": "submit"},
  "text": "Log in",
  "selector": "html:nth-of-type(1) > body:nth-of-type(1) > form:nth-of-type(1) > button:nth-of-type(1)",
  "selector_source": "dom_path",
  "selector_matches": 1
}
```

这是说明格式的假设节点，不是目标站点的选择器；实际应原样使用工具给出的 selector，不能从 id/name 重新拼接，重复值和 CSS 转义可能使重建结果错误。`selector_matches: 1` 只证明采集时唯一匹配这个节点，写配置或单步前还要调用 `browser_check_selector`：

```json
{"title":"实时核验观察到的登录按钮","frame_id":"main","selector":"html:nth-of-type(1) > body:nth-of-type(1) > form:nth-of-type(1) > button:nth-of-type(1)"}
```

核对 `match_count: 1`、`visible_count: 1` 和样本确为当前登录提交按钮后，用户/助手才可把这个已观察且实时核验的 selector 用于待审核配置或授权单步的 `submit_selector`。用户名、密码及成功标记也要独立核验；这不替代执行端的原始同表单、唯一可见、可编辑 typed password 输入和原始节点身份检查。若数量/身份不符或页面变动，重新采集，不猜 fallback。DOM path 随布局变化可能失效，需人工评估持久性。

`browser_check_selector` 是固定只读 CSS 查询，支持显式 open-shadow ` >>> `，不是任意 Playwright 引擎、JavaScript 或动作工具；不会点击、填写或提交。`match_count` 包括隐藏匹配，`visible_count` 单列可见数，例如 1/0 表示节点存在但隐藏，不能当作不存在；计数最多 10000、样本最多 10，`truncated` 也可能仅表示样本省略。实时查询不恢复旧快照遗漏的子树，也不保证检查之后 DOM 不变。无效 CSS 返回 `browser_selector_invalid`，未知 frame 返回 `browser_frame_unavailable`；先纠正/刷新证据，页面会保留。

以上均是结构化 DOM，不是图像；只看这些结果不能宣称已看过截图。执行的 `before` / `after` 每份最多 24 KiB，两份与执行外层共同受 API 64 KiB Worker 响应读取上限约束；普通观察的 32 KiB、通用工具结果的 128 KiB 是不同范围。独立截图为 viewport JPEG，最多 1600 × 1200、384 KiB，内部读取上限另为 544 KiB，不扩大 DOM/执行限制。

### 通用 JS 登录

登录不要求 `<form method="post">`，也不要求逐表单批准一个 POST action 端点。省略 method（默认 GET）或声明 GET 的原始表单，可由页面已有 JS 阻止默认提交并 fetch POST 到**原始 origin 的其他合规路径**。Worker 仍反复核对唯一可见的原始用户名/密码/提交节点及同一个原始 form：密码必须是可编辑 `input[type=password]`，用户名是可编辑的 text/email 输入，action/formaction 必须同源、目标为当前窗口；节点替换、跨表单或跨源不允许。这里没有模型 JS 执行能力，也没有 Fengchao 等特定网站补丁。

只有授权的非只读步骤活动时 `writes_authorized` 才开放同源 POST；`flow.open`、`action.navigate`、`action.wait` 从不开放 POST，PUT/DELETE 也不开放。JS 可以 POST 到不同路径，但不能把凭据发往跨源，哪怕该源已获 `trusted_origins` 观察授权。普通查询如 `/api/auth/login?redirect=%2F` 保持编码、不删除或重建；任何请求 URL 含已知秘密（包括编码/嵌套编码）仍阻止。因此原生 GET 表单把账号密码放进 URL 时会失败，不能把 DOM 校验通过误解为允许泄露。

失败先看 `diagnostics.reason`、`diagnostics.evidence_errorcode` 及最多 5 项的 `diagnostics.blocked_requests`。后者只有安全 `code`、HTTP `method`、投影 URL 和脱敏标记，不含 POST body、请求头、凭据、能力令牌或重试授权密钥；URL 可能整体为 `[redacted]`。旧 `browser_credential_endpoint_not_approved` 结果针对端点，不代表缺少 site ID 或 Bitwarden 故障，不能据此要求补无关配置。真实 Chromium fixtures 覆盖 JS/native POST、隐式 GET、查询保留和拒绝边界，不是实际网站认证成功证明。

### 登录时序与误判

以下是已有任务 329 的排查证据，不是本文更新时新发起的测试。该运行仍为 `trigger: "manual"`、`created_by: "admin"`，不能称为助手来源运行，也没有迁移其来源。相关站点 98 保持配置修订 **3**；用户当前要求修复实现，不是修改在线配置，本次文档更新不保存站点、不修改生产数据、不刷新页面或重跑任务。

| 时间或步骤 | 已有证据及含义 |
| --- | --- |
| `06:57:42.875` | 日志记录登录凭据已提交；提交不等于成功元素已可见。 |
| `06:57:52.940` | 旧实现已经等待约 10 秒，再按每个选择器首个匹配的可见性报告登录成功元素未出现；这是当时的成功证据检查失败，不证明服务端拒绝登录。 |
| `06:57:53.130` | 失败截图已呈现可见的已登录页面；截图晚于失败判定，不是判定瞬间的 DOM 证据。 |
| `settle_home` | 目标与当前 URL 相同，跳过 GET 导航，日志耗时约 `0.008` 秒；不能把它说成刷新首页后才登录成功。 |
| 后续等待与信号 | 再等待 3 秒后，`is_logged_in` 的选择器存在性检查命中；与提交步骤的可见性检查不同，且发生得更晚。 |

**可见性、存在性和检查时间必须分开解释。** 旧成功检查只看每个选择器的首个匹配是否可见；首个隐藏、后续可见是可能的误判模式，SPA 成功元素晚于等待期限渲染也是可能模式。稍后的信号只需元素存在，隐藏元素也能命中，因此它与较早的可见性失败并不矛盾。现有证据没有当时的 `match_count` / `visible_count`，无法确定任务 329 究竟属于隐藏首匹配、延迟渲染还是其他情况；不能补写数量或把某一种解释当成定论。截图能显示已登录的视觉状态，**不能证明具体 DOM 属性、选择器匹配身份、匹配顺序或可见性判定值**。

`auth.form_login.success_wait_seconds` 保持默认 **10 秒**，可配置为严格整数 **1-120**；任务 329 已经等待约 10 秒，不能把延长到 30 秒当作根因修复。`timeout_seconds` 必须另行覆盖凭据/秘密查询、定位、填写、点击和成功等待，不会因调高等待而自动延长；助手单步及轮次的外层上限仍有效。站点 98 的步骤总超时已为 **120 秒**，本次不修改在线配置，也不改动其他功能同名字段的默认值。

提交后只读轮询主页面及**可见同源 iframe**，所有祖先 iframe 须可见且同源，`about:blank` / `srcdoc` 沿父链继承来源，不使用凭据 `trusted_urls` 扩大成功证据范围。检查各成功候选的多个匹配，任一匹配可见即可，不限于 `.first`；每轮最多 100 个 frame、每个选择器在各 frame 中最多 100 个匹配，共用一个等待期限。隐藏节点单独存在不通过；`success_selector` 留空只表示已提交、不证明登录成功。等待不自动刷新、导航或再次提交，超时也不是可安全重试的证明。这处理隐藏首匹配等通用模式，但不能证明任务 329 的具体 DOM 原因，亦不代表已在生产复测成功。

超时后的诊断原因和匹配/可见计数是**超时后才采集的有界只读样本**，不是整个等待期间的历史记录，也不是同一瞬间的原子快照；采样间 DOM 仍可能变化。选择器索引按原步骤 `config.success_selector` 的配置索引（从 0 开始）核对，不是某个 DOM 匹配的序号；诊断不回显选择器值、表单值或凭据。即使稍后采样已有可见匹配，也不能证明等待期限内曾可见；采样仍无匹配也不能证明之前从未出现。

诊断 `reason` 中，`visible_after_timeout` 表示采样时已有可见匹配，`matches_hidden` 表示采样读到匹配但未读到可见匹配，`selector_missing` 表示本次采样范围未读到匹配；`frame_scope_excluded` 表示有 frame 因范围限制被排除，`read_incomplete` 表示读取错误或达到边界，不能按完整零匹配解释。`selectors[].selector_index` 对应原配置索引；结合 `checked_count`、`read_errors`、`truncated` / `evidence_truncated` 和各 frame 的 `scope` / 静态错误码核对，不把这些原因当作任务 329 的历史根因或新的登录成功判定。

工作流信号的 `include_selectors` / `exclude_selectors` 默认只判断存在，隐藏元素也可能命中；需要可见证据时可显式写 Playwright `:visible`，不自动追加、不改写已有配置或新增数据库列。`include_texts` / `exclude_texts` 则读取页面标题和各 frame 的 body `innerText`，通常不含 body 内隐藏后代文本，不应套用选择器存在性语义。助手 CSS 检查工具不支持 Playwright `:visible`，核验时使用其 `match_count` / `visible_count`，不能把零可见当作零匹配。

### 图像与恢复

本节重试审批只处理不明单步，不能解除未知完整运行的门禁；当前没有完整运行审批/核销 UI，不应承诺用户批准后即可继续。

UI「截图预览」仅在当前页面内存展示、关闭清除，不调用模型或持久化预览；模型 `browser_screenshot` 另要求本轮 `supports_vision=true`。两个入口都要求目标中独立的 `screenshot_consent`，来自宿主交互或用户明确启用的对应全局默认值，不能由统一执行授权或聊天代替。截图遮罩表单输入区域和全部 iframe/frame，并移除 JPEG 嵌入元数据，不是遮住整张表单或保证无秘密；可见个人资料等其他 PII 仍可能保留。工具/SSE 仅含图像 ID、owner URL、尺寸等元数据，模型像素由宿主在整批工具结果后作为独立 user 多模态证据追加，并加密持久化；摘要/摘录不含像素。撤销同意或目标修订变化后，历史像素不再重放给模型，关闭整理也不能绕过；owner 的历史图片 GET 仍可读取加密记录解密后的图像。

截图不能证明原操作失败或没有副作用，也不能证明成功；可用来询问用户是否确认刷新/重试，不得自动据图行动。「刷新网页」/`browser_refresh` 是对当前归属页面去掉片段、保留查询的地址发起**新 GET**，不是 `reload()` 重提 POST；会清除旧 DOM 缓存，但保留不确定性和账本。GET 也可能有副作用，spinner 不构成自动刷新理由。「刷新会话」则只读聊天状态。

正常重试流程由模型 `retry_step` 只传原操作 ID（及公共 title）发起；非只读 `execute_step` 的规范化步骤若与当前绑定下唯一可恢复的不明原操作完全相同，宿主也会转入同一审批，不把换 UUID 当作新操作直接执行。步骤不符/匹配不唯一时应改用原 ID 的 `retry_step`，不能改写原步骤。宿主暂停原工具调用，会话保持 `running`，详情/SSE 的安全 `pending_approval` 驱动输入框上方的内联风险面板；owner 经登录、CSRF 检查批准后，**原调用立即继续，精确执行服务端冻结原步骤一次**，不要求另一条消息或另一个模型执行调用。运行中可点输入区「停止」，聊天或模型参数不能批准，也没有“始终允许重试”权限；原始统一执行授权、跨源和秘密边界不变。

每次审批最多等待 **180 秒**，不按剩余活动时间缩短；活动预算已耗尽则不弹出并返回超时。实际用户等待不消耗默认 180 秒活动预算，等量顺延共享截止时间及外层超时；审批处理及批准后的执行仍消耗剩余活动预算，不延长提供商单请求超时、单步上限或浏览器 TTL。拒绝为 `retry_denied`，到期为 `retry_approval_timeout`，绑定/权限失效为 `retry_approval_invalidated`，均不执行。同一轮对同一操作缓存决定/结果，重复调用不反复弹窗或重复执行；停止及关闭服务立即取消，重启取消 pending，不自动续跑。下一轮用户明确再要求时可申请新审批。旧版空闲「允许重试一次」预授权仍可选，5 分钟、当前浏览器绑定、单次使用，此入口本身不执行，后续消息中的 `retry_step` 可消费它；它不是主流程，也不增加轮次活动时间。过期约束准入，已消费且绑定仍有效的结果可在到期后接收。更换浏览器/目标不清除不确定性，重新批准仍需原 origin/凭据绑定及当前权限有效。

只有关联的已派发终态 `success` 重试解除对应门禁，原 `unknown` 历史/受保护安全事实永不抹除；`no_retry: true` / `requires_host_approval: true` 表示禁止自主重复、必须宿主批准，不是永久禁止重试。当前宿主授权与 resolution 另外提供给模型，不能因旧文字忽略有效人工批准、拒绝发起审批或要求批准后另发消息。重试 `failed`、`error` 或目标缺失不能证明原操作失败；已知失败终态可重新明确批准，不自动续授权，重试仍未知则针对新的不明重试记录另行确认。完整准入、绑定和幂等规则见 [截图与恢复](ai-assistant.md#截图与恢复)。

### 聚焦读取与历史结果

助手当前代码定义共 **28 个工具**，包括 `request_user_interaction`、`update_site_config`、`import_workflow_draft`、`run_site`、`get_run_results`、`get_run_log`，实际工具名由 `assistant_prompt.tool_definitions()` 动态取得，完整清单见 [工具与限制](ai-assistant.md#工具与限制)。其中 `get_tool_result` 专门读取本用户、本会话已保存的原始工具结果，不是私有运行 artifact 日志读取器。开启 `auto_compact` 后，长对话达到阈值才摘要旧历史，工具轮次中也可能压缩旧批次的模型输入；这不删除或改写原始记录，但**不保证模型始终保有全部页面证据**。摘要有损且不具备授权效力，不应把省略的登录/签到结果猜成成功，也不能因细节已压缩就重新执行。

为控制输入，先用适度或较低的 `limit` 观察相关 frame；得到实际节点引用后，优先用 `browser_inspect` 读取表单的已捕获子树，或用 `browser_check_selector` 做聚焦的实时核验，避免反复读整页。可在消息中明确要求范围和较低分页数量；系统不会为每次提问自动另启一次全量读取历史的模型调用。同一轮已查询的功能 schema 可复用，不必每次重复查询；服务端 schema 缓存减少的是本地 CPU 构造开销，不等于上游免收输入 Token。每次无状态请求仍发送一份代码维护的助手系统提示、一份宿主目标系统消息及工具定义，不把系统提示追加保存到聊天历史。

详情的用量区展示后端从每次 `conversation` / `summary` / `title` 响应顶层 `usage` 提取的输入、输出、总计、缓存命中和推理 Token；缺失/无效字段为 null，UI 显示 `--`（未返回），不估算或补出总计。缓存命中属于输入、推理属于输出，不能重复相加；缓存优先取 `prompt_tokens_details.cached_tokens`，缺失/无效才回退 DeepSeek `prompt_cache_hit_tokens`，不相加。HTTP/网络失败等实际发送尝试计请求数、用量可未知，本地构建或大小检查失败不计。账本加密保存最近 100 次明细及会话生命周期累计已知值，不保存提供商原始推理，也不发送给模型；不做费用换算。

标题旁的上下文百分比使用最近有效 `conversation` 报告的 **prompt + completion**，除以该请求的 `context_max_tokens` 快照，不是累计消耗或 `total_tokens`；摘要/标题只计累计，不覆盖它。该次答案已计入 completion，后续工具结果和未来输入仍需下次真实报告。缺少有效 prompt/completion 或预算时跳过占用更新，保留整份最后有效读数，设置 `last_context_usage_missing: true`，前端明确标为最后已知、最近请求未上报；无已知值显示 `--`。下一次有效报告更新读数并清除此标记，缺失报告仍计入调用明细和累计已知值。整理或目标/上下文修订变化后读数失效、显示 `--` 等待匹配当前修订的新报告，不能将整理估算当作测得占用下降。输出上限仍沿用共享配置及本轮 `max_tokens`，不是上下文容量。完整口径见 [模型用量](ai-assistant.md#模型用量)。

如果模型输入中的工具投影给出了 `source_message_id`，或已有原始工具消息 ID，可要求用 `get_tool_result` 按该 ID 读取。下面只是参数格式示例，`123` 必须替换为当前会话的真实工具消息 ID，不是 `snapshot_id`、节点 ID 或执行 UUID：

```json
{"title":"读取先前登录测试的原始结果片段","message_id":123,"offset":0,"limit":2000}
```

`offset` 默认 0，`limit` 默认/最多 10000、最少 1，均按字符而非字节、Token 或 DOM 节点计数。按返回的 `next_offset` 继续，并核对 `message_id`；返回的 `content` 可能是 JSON 片段，拼接后才能解析完整结果。此工具仅读取已保存的结果正文及标题/工具名/状态，不返回私有调用参数，不执行动作、不重放请求、不访问当前页面，也不单独发起新的模型请求；正常后续工具对话仍受本轮预算限制。即使该消息已不在详情最新 200 条窗口内，只要原始记录仍在且 ID 正确，仍可读取。

读取历史 `browser_observe` 或 `execute_step` 结果，只能证明当时捕获的内容；不能找回有界扫描之外的节点、恢复已淘汰的浏览器快照，或证明当前仍登录/未签到。当前状态和选择器仍按上一节重新核验；`outcome_uncertain` 的禁止重试安全记录独立保留，改目标或摘要不清除此记录。

「较早上下文已整理，完整记录仍保留」标记及修订号表示存在已保存摘要，不表示所有上下文仍逐字送给模型，也不证明关闭整理时仍在使用摘要。开启整理时，有旧记录可整理且超过 40 条或旧历史超过预算 60% 的本地估算时尝试摘要，保护最后两条真实用户消息及近期记录；最多两次、每次 12 秒，计入本轮默认 180 秒，沿用已解析的模型及 `max_tokens`/秘密策略，失败通常回退确定性摘录。工具轮次软水位为预算 60%（默认 38400 估算 Token），保留整批调用/结果配对、调用 ID、计数与最后一批结构；必要时仍可缩减正文。

`context_max_tokens` 默认 **64000**、范围 **16000-1000000**。完整旧版设置的 `context_max_bytes` 只读迁移按 `ceil(bytes / 4)` 并钳制到新范围，新增三个默认授权为 false；下一次 CAS 保存/恢复默认才写新结构。当前发送前估算包含系统提示、工具定义、消息框架和图像标记，ASCII 约按字符数/3 向上取整、非 ASCII 按 UTF-8 字节保守计数，不是提供商精确分词，也不填补 usage。`[context_limit]` 是本地估算/保护预算提示。整个线协议请求仍固定最多 **1536 KiB**、响应 **256 KiB**，工具参数/结果分别 **64/128 KiB**；截图 base64 仍计传输大小，不可截断。调高 Token 预算不能绕过字节限制或保证上游接受。缩小问题或分页范围，必要时新建会话，不能靠重复副作用操作补证据；完整规则见 [上下文整理](ai-assistant.md#上下文整理)。

关闭 `auto_compact` 后忽略已有摘要和边界，恢复读取其前后的原始历史，同时禁用新摘要、轮次开始/轮中的缩减及旧工具结果的有损正文投影；不会删除或改写已保存摘要、修订或记录。仍保留安全包装、禁止重试事实及中断调用的静态失败配对修复，不重放旧工具。恢复后超出本地估算预算就返回 `[context_limit]`，线协议字节上限仍另行检查，不暗中裁剪或改写历史。

新记录以允许字段保存规范模型消息，设置、目标/系统提示和工具定义不变、未实际整理且无需中断修复时，后续请求保持前次 `messages` 的精确前缀；开启整理但低于阈值也如此。实际压缩会必要地改变前缀；旧版记录仍安全包装读取，未保存的原始调用 ID 无法靠迁移还原，不承诺旧历史精确重建。提供商原始推理字段不保存、不重发。稳定前缀不保证缓存命中，还取决于模型路由、缓存到期、请求头和提供商对提示/工具的规范化；离线/mock 测试不是完整真实网关流量验证。详见 [稳定请求历史](ai-assistant.md#稳定请求历史)。

### URL 证据与跨源确认

观察中的 `url` / `href` 保留公开查询与片段的完整原始编码，不统一删除，也不重排参数。例如 `?tab=foo&next=%2fhome#/login?tab=foo` 可以保留。`token`、`password`、`session`、`code`、`state`、`sig` 等敏感键的值替换为 `[REDACTED]`，通过 `url_redacted` / `redacted_parameters` 或 `href_redacted` / `href_redacted_parameters` 标记。普通 `return_to` 和不含秘密的嵌套 URL 保留；嵌套 URL 中只擦除敏感值并保留其余编码，例如嵌套 `token` 的值可变成 `%5BREDACTED%5D`。**脱敏 URL 只作证据，不得直接导航、写入步骤或保存为工作配置，也不能猜测或索取其秘密值。** 按键过滤不是通用 DLP，路径、可见个人资料和公开参数值仍须审核。

公共跨源导航/重定向尚未获批时，`browser_trust_required` 返回脱敏后的完整 `source_url`、`target_url`、各自的 `source_url_redacted` / `target_url_redacted`、`redacted_parameters`，以及 `target_origin` 和精确 origin 转义得到的 `trusted_url_pattern`。这只是被阻止的候选，不是已访问或已获信任的证据。工具卡片展示完整地址、待核对来源和脱敏警告；只有用户明确要求变更观察来源后，助手才可用 `update_site_config` 的 `observation.trusted_origins` 提交完整目标列表供精确审核，不再推荐旧 purpose 或打开目标设置。批准恢复原调用、替换来源列表、更新目标修订并释放旧浏览器，不自动导航、执行或重试；模型可在同轮安全重开/导航，不需另发消息。Worker 仍校验公共 DNS；额外来源不能用于单步执行、凭据发送或同源站点绑定，也不写入站点 `trusted_urls`，单步后仍保留原始同源限制。完整边界见 [完整 URL 与可信来源](ai-assistant.md#完整-url-与可信来源)。

打开失败时，记录工具结果中安全的 `code`、`lane_key`、`reason`：`browser_dns_denied` 指向 Worker 本地 DNS/公共地址检查，**不证明需要代理**；`browser_url_denied` / `browser_redirect_denied` 指向 URL 或重定向边界，`browser_lane_busy` / `browser_lane_unavailable` 指向所选交互车道；不要输出原始异常或秘密。读取遇到 DNS/策略失败而 `page_retained: true` 时，没有证据但保留页面，不是登录结果；含已知秘密的当前 URL 拒绝仍致命，不套用保留页面例外。此前生产环境两次打开返回 403，尚无证据归因于通用车道停止，也没有生产复测证明已完整修复；没有具体原因时应说明未知，详见 [API 与排查](ai-assistant.md#api-与排查)。

## 单步工具示例

下面是 `execute_step` 的**完整模型工具参数示例**，不是需要执行的请求。选择器来自上述源模板，必须先替换为目标页面已核实的候选。示例不包含凭据、会话令牌或站点 ID；工具的 `title` 由分发器移除后交给执行服务。

```json
{
  "title": "用已核实的表单候选测试一次登录",
  "operation_id": "6d0ca06e-e6b2-46a4-8d67-b8494c51fe0a",
  "step": {
    "step_key": "test_login",
    "feature_type": "auth.form_login",
    "label": "单次登录证据采集",
    "enabled": true,
    "condition": "always",
    "on_error": "stop",
    "timeout_seconds": 60,
    "config": {
      "username_selector": ["input[name='username']"],
      "password_selector": ["input[name='password']"],
      "submit_selector": ["input[id='submit-btn']"],
      "username_field": "username",
      "password_field": "password",
      "success_selector": ["a[href*='logout.php']"],
      "success_wait_seconds": 10
    }
  }
}
```

单步不运行完整工作流，也不评估模板信号条件，必须是启用的无条件步骤；不能直接把示例中带 `signal:` 条件的登录步骤拿来测试。每次**当前目标授权内的新操作**生成新的 `operation_id` UUID，不复用本文 UUID；统一授权不要求每个正常单步另勾权限。操作 ID 在派发前登记；超时、取消、失败或响应丢失不意味着网站没有收到请求，不得换新 ID 重放不明操作。结果不明后先保留页面只读核验，明确要求重试时由 `retry_step` 发起宿主审批并暂停同一调用，批准后立即恢复冻结原步骤一次，不必再发消息，也不是强制重开浏览器或自主重试。工具参数不支持把 `credentials` 作为附加字段传入。

## 挑战处理

独立 JSON 只演示没有待处理挑战的流程。它保留来源中的 `has_image_captcha` 候选信号，用作登录/签到提交的阻断条件；未处理验证码时不把失败当成功。该检测不是全能挑战探测器，不能发现所有验证码、Turnstile 或 ALTCHA。

源模板配置了 `challenge.image_captcha`（`captcha_method: nexusphp_gd5`，`input_selector: ["input[name='imagestring']"]`，父模板图片候选如上，`min_confidence: 0.95`），还包含 Turnstile 和 ALTCHA。示例没有复制这些挑战步骤，也没有默认启用自动解题。当前 `execute_step` 的受限列表不包含图片验证码或 Turnstile，不能用一次工具测试推断它们可用。

当前实时单步仅支持导航/等待、两项认证、签到/点击及 ALTCHA/滑块共 10 项，完整清单见 [单步验证](ai-assistant.md#单步验证)。统一授权不开放任意代码、通用填充、插件、`post.*` 购买/补签，也不把密码、Cookie、令牌或挑战秘密交给模型；不支持的挑战仍需人工处理。

若目标确实需要图片验证码，应查询 `challenge.image_captcha` schema，先在独立草稿中添加 `enabled: false` 的候选步骤并人工审核。`auth.form_login` 会填写后立即提交，不能简单在其后追加验证码就假定能工作；必须观察实际挑战出现时机、提交顺序和成功状态，并相应重设计条件。验证码处理后图片仍可能保留，不能只切换步骤开关而保留本示例的阻断条件。不要记录验证码答案、隐藏 token 或输入值。

没有复制一个禁用的 Turnstile 步骤是有意的：完整草稿仍会校验禁用步骤的配置，运行能力还取决于集成设置；“禁用”不是绕过配置校验或权限的机制。不需要挑战的流程不应因此要求 CFbox。

## 完整草稿校验

JSON 的顺序是打开首页、按需登录、缺少登录证据则失败结束、已登录且未签时导航、按条件提交一次、证据充分时成功结束、否则失败结束。`flow.result` 会终止工作流；最后的无条件失败不会覆盖前面的成功结果。信号未知会走 `needs_human`，不是自动视为 false。所有导航 `retry_count` 为 0，避免隐式重试副作用。

登录成功选择器、登录信号和签到成功文本是不同层次的证据，必须一起核实。示例保留来源的签到排除文本，即使它们可能与成功文案同时存在；这种情况应修正为目标专用证据，不能为了得到 true 就删除排除项。`已签到`、`#info_block`、通用 submit 等候选尤其容易误匹配。示例不承诺任意站点上的幂等性、完整挑战处理或端到端成功。

在正常编辑流程中，将此文件作为新模板的 `TemplateWrite` 请求体，调用 `POST /api/v1/templates/validate` 做只读校验（需要登录及 CSRF），不要调用生产保存接口来“测试”。`TemplateWrite` 不是助手工具的参数包装；`validate_workflow_draft` 的工具参数是 `title` 和 `draft`，其中 `draft` 仅含完整的 `steps`、`groups`、`signals` 数组；`submit_workflow_draft` 还需 `explanation` 和 `unresolved_questions`。不能把模板名称、`enabled` 或 `parent_id` 放进 `draft`，也不能把管理端原始解析预览直接转发给模型。提交提案不等于保存、执行或调度授权。

助手 `validate_workflow_draft` 失败为顶层 `ok: false`、最多 10 项 `diagnostics`，每项有静态 `code`、`message`、安全 `path`，可另含 `step_key`、`feature_type`、配置 `field` 和数值/长度约束 `hint`。例如缺失信号定位到 `["draft", "steps", 0, "condition"]`（`signal_missing`），策略要求但仍缺失的提交选择器定位到 `["draft", "steps", 0, "config", "submit_selector"]`（`field_required`），不返回条件/选择器的实际值或任何秘密。已知语义规则也有针对性反馈：`flow.jump` 目标含冒号时 `flow_jump_target_invalid` 指向 `target_step_key`，提示移除冒号；未知规则/故障仍静态兜底，不解析原始异常。

这最多 10 项只来自**首个失败校验阶段**，不是所有步骤、所有阶段的全部错误。按路径查询受控 schema、修复并迭代校验，直到通过后再提交；管理端错误字符串与 local/resolved 预览不是模型安全诊断，不得原样转发，工具失败文字/日志也不得输出输入、管理员默认值或秘密。完整 JSON 示例见 [草稿修复诊断](ai-assistant.md#草稿修复诊断)，共享字段定义见 [Validation Diagnostics](config-ai.md#validation-diagnostics)。

仓库测试通过 `TemplateWrite.model_validate` 和 `WorkflowService.validate_draft` 在临时 SQLite 中验证完整模板，关闭模型及 CFbox 配置并禁止网络调用。这只证明当前 schema、功能配置、信号引用、分组和顺序可解析，不会访问真实网站或证明候选正确。

```bash
cd backend
../.venv/bin/pytest tests/test_assistant_example.py
```

助手的校验/提案结果还会给出 `required_site_settings`，含认证来源、提供商、保存运行车道及说明性的 `trusted_origins`；这些不属于 `draft` 的三个工作流集合，提案本身不保存它们。跨源观察提示中的 `trusted_url_pattern` 是由精确 origin 转义得到的建议，例如 `https://auth.example.com` 对应 `^https://auth\.example\.com(?:[/?#].*)?$`；不会自动写入站点 `trusted_urls`。后者涉及跨源凭据授权，须在正常编辑器另行核对收窄并保存，或用户明确要求后通过 `update_site_config` 精确审核已保存目的站点的规则；不能因为助手获准观察某源就默认授权发送凭据或完整工作流导航。

人工审核后可走正常编辑器，或使用下节的独立授权导入流程；不得保存含 `[REDACTED]` 或其编码形式的工作 URL，不要修改原生产模板来验证本文。

## 导入与反馈闭环

1. 先完成 schema 查询、完整草稿校验和 `submit_workflow_draft`。提交必须是该响应中唯一工具调用，只产生持久化提案，不等于保存。用户要求导入/测试时模型可同轮继续；缺少 `workflow_consent` 且未明确开启对应全局默认值时，宿主请求独立工作流授权，批准即恢复原调用，不需另发消息或预填设置。只需提案时也可结束。更换目标/可信来源/代理改变目标修订，旧提案需在当前目标下重新校验提交；单独授予能力不要求更换目标。
2. `import_workflow_draft` 只传宿主返回的 `{proposal_message_id}`（另有公共 `title`），精确导入当前 owner/会话/目标修订下成功保存且无待确认问题的原提案，不接受另一份 JSON。目的站点由宿主 `workflow_site_id` 决定，不能拿凭据 `site_id` 代替。已有站点按冻结身份和修订做 CAS，只替换本地三集合及确认的运行车道，保留模板、凭据、Cookie、调度、启用状态及其他元数据；配置变更生成新修订及不可变历史。遇到隐藏/受保护本地配置以 `protected_config_changed` 拒绝，转人工编辑，不覆盖为占位值。
3. 新建站点默认 `enabled: false`，不复制凭据/浏览器 Cookie，不启用自动调度。用户可在正常编辑器自行启用，或在当前配置完整成功测试后明确要求助手单独审核启用；不能主动建议启用。`update_site_config` 的 `enabled: true` 必须是唯一 patch 字段且无 observation，准备和应用两次均须同 owner、同会话、当前历史身份/配置修订的已完成助手完整 Run 及其唯一 SiteJob 均为 `success`、均已结束，Run 未请求取消、关联 run action 已完成且严格配置守卫有效。可选 `successful_run_id` 指定证据，省略由服务端查找；排队、跳过、单步成功、手动/其他会话运行或旧配置证据均不算，配置变更后须重新完整测试。完整测试可经私有入口运行禁用站点而不启用它，公开运行 API 没有对应 force 参数；该助手启用门禁不改变正常编辑器/手动 API。已有 enabled 保留。manual 需站点私下保存密码且凭据绑定就是目的站点；Cookie 模式须目的站点已有真实保存的 Cookie，不能从发现浏览器复制。无凭据就先通过正常 UI 配置，不向聊天提供秘密。
4. `run_site` 除公共 `title` 外只能传 `{}`，只把单个绑定已保存站点交给共享 executor，没有任意站点 ID、全部运行或 force。完整解析（包括继承）仍拒绝 `post.*`、插件/插件提供商和 `action.fill`；URL 必须为原始目标同源的公共、无敏感值完整地址，额外可信观察 origin 不扩大完整运行或凭据授权。新建运行的守卫哈希覆盖当前本地/解析配置、祖先模板和功能设置/策略，执行前漂移以静态 `assistant_configuration_changed` 拒绝；正常同代理模式通用到交互回退仍有效。
5. 通过 `get_run_results` 读取 `{run_id, cursor: 0, limit: 50, wait_seconds: 0, snapshot_id?}`，limit 1-50、wait_seconds 0-20，建议 10 或 20 秒有界等待。只返回本会话关联运行的安全状态、白名单 reason/field/stage/cause 等诊断、布尔值和计数，不把任意错误正文当证据。步骤会原地更新，cursor 只相对于当前安全投影；非首页携带返回的哈希 `snapshot_id`，遇到 `run_snapshot_changed` 从 0 重读，不拼接不同投影的页。
6. `get_run_log` 是 **curated structured diagnostic**，参数 `{run_id, offset: 0, limit: 16000, snapshot_id?}`，limit 1-16000 字节，返回 `content` 分页及 `next_offset`，非首页同样需 snapshot。它不是原始日志 artifact，不回传页面文字、完整站点 URL、原始异常 dump 或凭据；原始日志只在私有运行 UI 查看，不要复制给模型。snapshot/run ID 不能扩大到其他用户或会话的运行。
7. 完成后若是证据明确的已知失败，可按安全诊断调整验证条件/选择器，validate、submit、import 新修订，再测试；不是对旧绑定重复调用来“再跑”。同一提案导入、同一绑定修订加提案运行均幂等，返回原站点/运行身份，不重新提交。提交确认丢失返回 `ok: false`、`run_submission_uncertain` 并持久保留 run ID/unknown action，不自动补投，`submitted: false` 不是未执行证明。
8. Worker 的 `assistant_run` 模式对可能已生效的异常/超时记录 unknown，不当作普通失败。未知完整运行阻止新完整运行及主动单步，不能用换提案/修订、换 UUID 或改用单步重做；自动调度也跳过未决站点。需人工在网站/VNC/运行 UI 核清；当前没有完整运行审批/核销 UI，单步 `retry_step` 审批不适用，普通手动运行也不会自动清除此状态，不承诺“批准后继续”。完整边界见 [导入与完整测试](ai-assistant.md#导入与完整测试)。

新的助手导入在新建/更新工作流时使用精确 actor `assistant`，生成的配置历史 `created_by` 为 `assistant`；新的完整任务同时保存 `Run.created_by: "assistant"` 与 `trigger: "assistant"`。真实人类身份保留在助手会话 owner 及关联 action owner 中，而非新运行的 `created_by`；会话/消息及 action 载荷加密保存，owner 索引元数据并非全是密文。UI 创建人显示英文 `assistant`，tooltip 为「来源：智能助手」，通知 `trigger_label` 也为英文 `assistant`，不展示人类发起人作为运行 actor。旧记录不迁移，任务 329 仍是 `manual` / `admin`，不能改称助手来源。

工具等待有界且占本轮预算；轮次结束后不会自动监看任务或主动回到聊天汇报。若尚在排队/运行，只报告 run ID、当前状态及运行入口，用户可下一条消息要求读取完成结果，不虚构成功或承诺后台助手稍后回答。
