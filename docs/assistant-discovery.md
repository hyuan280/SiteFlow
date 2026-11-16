# 从页面证据发现登录与签到配置

本文配合 [AI 助手](ai-assistant.md) 和 [草稿校验](workflow-drafts.md)，说明如何把单步观察转为人工审核的工作流，并在独立授权后导入、完整测试及根据安全反馈修正。示例是 [nexusphp-assistant-workflow.json](examples/nexusphp-assistant-workflow.json)，不是对某个网站的执行记录或通用适配保证。本文更新未执行生产测试或目标网站请求；仓库中的真实 JS/Chromium 本地 fixture 测试也不等于实际 Fengchao 登录已验证。

当前共 **23 项内置功能**可编写提案并用于授权完整运行，包含公开非秘密 `action.fill` 和四项 `post.*`；孤立单步为 **18 项**，排除 `flow.jump` 和四项 `post.*`。插件仍不开放，普通交互工具数为 **38**，另有 1 个接管专用 `submit_takeover_conclusion`（注册表共 39 个）；自动接管只开放委托过滤后的子集。目录与 schema 按当前代码及服务端策略提供，不保证生产站点适用或业务成功。

新模型输出遵循「真实用户明确指定语言 > 真实用户消息语言 > 安装级 `Settings.scheduler_timezone`（`SITEFLOW_SCHEDULER_TIMEZONE`）白名单默认值」，不是按网站语言或站点时区切换。自动生成的英文接管指令、工具/日志和助手输出不代表人类偏好；最终回复、可见工具标题、提案说明及接管结论模型正文均适用。完整映射、简体中文兜底及固定宿主文案/历史记录不翻译的边界见[回复语言](ai-assistant.md#回复语言)；这是提示策略，不是语言校验或送达保证。

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
6. 对比可用的 `before` / `after`，再做安全的 `browser_navigate` / `browser_observe` 检查登录状态是否保持；验证码后仍可观察、检查选择器及连续执行已授权单步，不需重开，输入值仍省略，见下文「挑战处理」。需要图像时，本轮支持视觉的模型调用 `browser_screenshot`；若缺少 `screenshot_consent` 且未明确开启全局截图默认值，宿主自动请求独立图像授权，批准后恢复同一调用，无需预先打开设置。已有截图权限与可用浏览器时，也可空闲点 UI「截图预览」，不要求视觉模型。`submitted: true`、工具 `ok: true`、按钮消失、单个通用欢迎词或截图转圈都不等于认证成功/失败。返回的静态日志不是原始运行日志，也不是成功证据。
7. 形成 `is_logged_in`：使用仅登录后出现的正向元素/文本，并加入仅未登录时出现的排除元素/文本。目标页面若始终含“请登录”或隐藏导航等文案，要重新检查实际信号检测语义，不能盲目照抄。`auth.form_login` 没有 `success_when_missing` 字段，不能把其他功能的字段拼进来。
8. 对签到重复这个过程：先观察今日已签到与待签到状态，仅在明确未签且当前目标授权覆盖该操作时，才考虑一次操作。统一授权派生签到、点击、公开填充及挑战所需的副作用权限，不要求额外勾选；旧目标不能据此自动扩大原有授权。即使是 GET 导航也可能签到；访问 `/attendance.php` 前必须先判断副作用和授权，不能为了“观察”无条件访问。验证码可与后续登录/签到在同页连续单步测试，完整工作流另用于验证编排，不是串接的强制前提。
9. 比较签到前后证据，建立 `is_checked_in`，确认它针对当前账户的今日状态，而非历史记录、页面标题或其他用户。如果已签就跳过提交；结果不明先观察。普通会话以原操作 ID 调用 `retry_step`，宿主按当前非 post 类别策略自动签发单次能力并执行冻结原步骤，不弹人工重试审批；不改写步骤或以换 UUID 冒充原操作失败。历史 `no_retry` 不覆盖当前宿主策略，原 `unknown` 来源事实仍保留。最终只有充分证据才输出成功，其他情况如实说明。

发现浏览器的 `proxy` 字段只决定实际基础设施车道 `interaction` / `proxy_interaction`，完整任务另用 `run_lane_policy`。新目标的保存运行车道默认 `general`，旧/API 默认 `auto` 也解析为 `general`。用户明确要求后，用 `update_site_config` 的 `observation.proxy` 修改观察代理，用 `patch.lane_policy` 修改已保存目的站点的运行车道；后者支持 `auto`、`general`、`interaction`、`proxy_general`、`proxy_interaction` 全部五项，二者独立审核，不因失败自动切换。导入前只能以空 patch 修改观察设置，不创建站点。

保存车道选择不会把发现浏览器搬到通用车道：其实际仍为 `interaction` / `proxy_interaction`。无消费计划的完整任务正常 `general -> interaction` / `proxy_general -> proxy_interaction` 同代理模式回退仍允许，不是配置漂移，也不意味着可自动切换直连/代理模式；带消费计划的助手运行不自动回退/重试。旧 `purpose: "proxy"` / `purpose: "trusted_origins"` 仅保留既有持久化兼容行为，不再推荐新调用。

设置批准后原调用只应用冻结差异，不自动导航、导入、运行或重试；旧浏览器释放后需安全重开，旧提案须按当前目标修订重新校验提交。`auto_authorize_execution`、`auto_authorize_screenshot`、`auto_authorize_workflow` 是全局独立默认值，均为 false，只有用户明确勾选并保存才开启。执行默认值只控制浏览器单步；第三项同时覆盖工作流导入及绑定站点完整测试，不是仅保存，也不依赖单步开关。它们不跳过目标确认，**任何设置写入仍须真实用户批准本次精确差异**，不授予秘密输入或额外来源凭据权限。已有显式目标/会话权限保留，关闭默认值不撤销已授予权限。

`update_site_config` 使用稳定 `operation_id` UUID、可选 `patch` / `observation`（至少一项非空）及仅启用时可选的 `successful_run_id`，另有公共 `title`。patch 仅允许 **10 项**：`name`、`trusted_urls`、`enabled`、`lane_policy`、`support_resign`、`resign_max_days`、`checkin_after`（null 清除）、`timezone`、`tags`、`notes`；observation 仅允许 `proxy` 与替换完整列表的 `trusted_origins`。不接受任意站点 ID、`base_url`、模板、提供商、凭据/Cookie 或 `steps` / `groups` / `signals` 数组。目的地只能是已确认的 `workflow_site_id`，不是凭据源 `site_id`。站点设置面板用可读字段逐项展示变更前/后及风险，不用原始 JSON；`trusted_urls` 精确正则扩大的是本站点凭据/TOTP 使用范围，不是观察信任，宽泛规则可能泄露凭据。

设置工具复用既有最多 **180 秒**的同调用等待，实际用户等待暂停活动预算，批准处理仍计入。准备时冻结目标绑定、历史身份、配置修订和设置，批准后原调用复查并 CAS 写入；变更历史 actor 为 `assistant`，与加密 action 回执及更新后的绑定在同一事务提交。拒绝、超时、取消、重启、绑定漂移或复查失败不写入；相同 UUID/请求只重放结果，不重复写入。完整字段与边界见 [站点设置变更](ai-assistant.md#站点设置变更)。

交互面板支持授权、单选/多选和非秘密文本输入；通用 `purpose: "question"` 的答案不授予任何目标权限。每次最多等待 **180 秒**，独立于本轮剩余活动时间；活动预算已耗尽则不弹出。仅实际用户等待暂停活动计时，等量顺延共享截止时间及外层超时，不重置默认 180 秒活动预算；审批处理、网络请求和批准后的执行仍计入，提供商单请求超时、单步上限及浏览器 TTL 不变。停止及关闭服务仍立即取消。拒绝、超时、取消、失效后同一轮同一 subject 不可重复询问，不能换措辞或拆分请求绕过；缺少必需授权或必填信息时停止依赖操作。批准后原调用继续，不需另发消息。没有秘密输入入口，不得索取或回显密码、Cookie、Token、提供商密钥或 OTP/TOTP 认证动态码；图片验证码答案不一概视为秘密，实际输入仍受宿主校验。页面标题操作区和输入区固定，时间线独立滚动；安全提示关闭状态仅存在本浏览器 localStorage，不改变任何宿主授权。

已保存 Cookie 不要求先做登录单步：有效显式绑定下，新 `browser_open` 在首次导航前私有注入精确主机 Cookie，优先 `workflow_site_id`、否则凭据 `site_id`，优先绑定失效不回退，也不按 hostname 找站点。适用于所有凭据模式，但不增加 auth、OTP 或完整运行权限，不把原始 Cookie 交给模型，也不自动导出登录后 Cookie 回写。Cookie 属于共享车道 context，非标签页隔离；TTL 不变。仅用户明确要求时可调用普通工具 `clear_bound_site_cookies({operation_id})`（UUID4，另需 title），已有原始绑定 READY 浏览器和全新 `cookie_clear` 范围审核才可清除，不能自动开页；只删绑定站点保存 Cookie 及当前车道 host/.host 所有路径 Cookie，同主机标签页可能退出，父域/其他车道、账号密码/localStorage 保留且不刷新。部分/未知结果不自动重试，无人值守接管不可清除。完整范围、generation 防旧种子回放及隐私限制见[绑定站点 Cookie](ai-assistant.md#绑定站点-cookie)，需 API、Worker、Web 配套升级。

没有单步执行授权时，助手观察标签页不能提交登录表单；私有 Cookie 注入也不改变此限制。无可用登录态时，用户可在同车道独立人工标签页完成登录，然后要求助手重新导航并观察；不能要求模型绕过权限。挑战能力不支持、证据不充分或模型/Worker 版本不配套时，先停止，不假定登录成功。

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

本节类别策略只处理单步，不能解除配置保存/导入或未知完整运行的门禁；当前没有完整运行审批/核销 UI，不应承诺用户批准后即可继续。验证码本身不锁浏览器，后续 DOM/选择器观察、独立授权截图和已授权单步可继续，输入值仍省略；历史未知不一概禁止非 post 单步，重开也不清除不确定性。

UI「截图预览」仅在当前页面内存展示、关闭清除，不调用模型或持久化预览；模型 `browser_screenshot` 另要求本轮 `supports_vision=true`。两个入口都要求目标中独立的 `screenshot_consent`，来自宿主交互或用户明确启用的对应全局默认值，不能由统一执行授权或聊天代替。截图遮罩主页面与可读同源 frame 内的输入控件，其余 frame 保守地整体遮罩，并移除 JPEG 嵌入元数据；不是遮住整张表单或保证所有秘密像素均已清除。可见个人资料等 PII 仍可能保留，页面反映的图片验证码答案可以保留。工具/SSE 仅含图像 ID、owner URL、尺寸等元数据，模型像素由宿主在整批工具结果后作为独立 user 多模态证据追加，并加密持久化；摘要/摘录不含像素。撤销同意或目标修订变化后，历史像素不再重放给模型，关闭整理也不能绕过；owner 的历史图片 GET 仍可读取加密记录解密后的图像。

上述遮罩与撤销规则仅针对实时浏览器截图；独立授权的 `read_artifact_image` 读取未遮罩历史像素，不因实时截图同意撤销或目标变化失去其历史授权，见下文历史排查边界。

截图不能证明原操作失败或没有副作用，也不能证明成功；可用来询问用户是否确认刷新/重试，不得自动据图行动。「刷新网页」/`browser_refresh` 是对当前归属页面去掉片段、保留查询的地址发起**新 GET**，不是 `reload()` 重提 POST；会清除旧 DOM 缓存，但保留不确定性和账本。GET 也可能有副作用，spinner 不构成自动刷新理由。「刷新会话」则只读聊天状态。

唯一可信类别是代码内置 `Feature.category`：`flow`、`auth`、`challenge`、`sign`、`action`、`post`，不按键前缀、描述、模型参数或插件元数据判定。助手单步及完整保存运行的 `auth` 登录和密码验证及其重试共用发起真实用户轮次的 `max_auth_uses_per_turn`，默认 3、严格整数 1-20；解析凭据前预留，失败也计次，内部模型轮次、换目标/浏览器不重置。完整运行按运行时新签发的 auth 凭据租约计一次，兑换不重复计次，条件跳过不计；循环重入、执行器重启/回退的新租约仍计次，后台任务即使跨聊天轮次也沿用发起轮次和上限。含 auth 的新 `run_site` 派发前检查剩余额度，运行中耗尽拒绝后续 auth 租约，不是无限认证额度；普通手动/调度任务不受此助手额度限制。`challenge.2fa` 属于 challenge，不占 auth 额度，但 OTP 私有及凭据同意不变。受支持 flow/challenge/sign/action 可在原始同源和当前授权内继续，不因历史未知一概拒绝。

普通会话 `retry_step` 只传原操作 ID（及公共 title），宿主自动签发单次能力，以新 UUID 执行**存储的精确原步骤**，无需 UI 审批；auth 重试另计一次额度。原 origin、浏览器/目标、凭据绑定、授权和幂等检查仍有效。旧空闲「允许重试一次」及审批 API 保留兼容，人工仍可使用，不表示所有重试都需审批。自动接管当前仍拒绝 `retry_step`；新 `execute_step` 按类别/额度检查，不能宣称被拒工具可用。

原 `unknown` 历史/受保护安全事实不抹除，当前类别策略及宿主 resolution 优先于旧 `no_retry` / `requires_host_approval` 文字。失败、缺失元素、批准或无关成功都不证明原操作失败或已核销。类别许可是用户策略，不是物理点击、导航或脚本不会改变网站的保证；应提示真实副作用，不宣称只有 post 能写网站。完整规则见 [截图与恢复](ai-assistant.md#截图与恢复)。

### 聚焦读取与历史结果

助手普通交互有 **38 个工具**，包括 `clear_bound_site_cookies`、站点转模板、全局调度、任务取消、历史版本及 artifact 专用读取；注册表另有 1 个接管专用结论工具。实际工具名由 `assistant_prompt.tool_definitions()` 动态取得，完整清单见 [工具与限制](ai-assistant.md#工具与限制)。其中 `get_tool_result` 专门读取本用户、本会话已保存的原始工具结果，不是私有运行 artifact 日志读取器；实际历史日志使用独立 `read_artifact_log`。开启 `auto_compact` 后，长对话达到阈值才摘要旧历史，工具轮次中也可能压缩旧批次的模型输入；这不删除或改写原始记录，但**不保证模型始终保有全部页面证据**。摘要有损且不具备授权效力，不应把省略的登录/签到结果猜成成功，也不能因细节已压缩就重新执行。

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

`ready` 不是页面后续存活保证：异步 Fetch 失败可随后关闭页面，保留 `browser_response_interception_failed` 与 `stage: response_event / continue_response / fail_request`，仍 fail closed 清理、不自动重试或放宽请求策略。正确令牌在有界关闭 tombstone 内可查已知原因或 410 `browser_closed`，错误令牌仍为通用 404；API 将 Worker 404 映射为 `browser_session_unavailable`，不再一律报 `browser_worker_error`。Cookie generation/导入失败也保留静态原因，详见[生命周期诊断](ai-assistant.md#生命周期诊断)。来源任务认证选择器失败与实时浏览器失效须分开；精确生产异常未记录时，不能宣称 CDP 不支持、代理/URL 故障，也不能因观察不可用就排除验证码或站点问题。

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

单步不运行完整工作流，也不评估模板信号条件，必须是启用的无条件步骤；不能直接把示例中带 `signal:` 条件的登录步骤拿来测试。每次**当前目标授权内的新操作**生成新的 `operation_id` UUID，不复用本文 UUID；统一授权不要求每个正常单步另勾权限。操作 ID 在派发前登记；超时、取消、失败或响应丢失不意味着网站没有收到请求。普通会话精确重试使用原 ID 的 `retry_step`，自动取得一次原步骤能力，不弹人工审批，auth 另计额度；新的授权非 post 步骤不受历史未知的一概禁止。验证码和普通未知状态均不强制重开，导航、刷新或重开本身也不清除未知来源事实。工具参数不支持把 `credentials` 作为附加字段传入。

## 挑战处理

独立 JSON 只演示没有待处理挑战的流程。它保留来源中的 `has_image_captcha` 候选信号，用作登录/签到提交的阻断条件；未处理验证码时不把失败当成功。该检测不是全能挑战探测器，不能发现所有验证码、Turnstile 或 ALTCHA。

源模板配置了 `challenge.image_captcha`（`captcha_method: nexusphp_gd5`，`input_selector: ["input[name='imagestring']"]`，父模板图片候选如上，`min_confidence: 0.95`），还包含 Turnstile 和 ALTCHA。示例没有复制这些挑战步骤，也没有默认启用自动解题；这只是示例范围，不是当前助手禁止这些功能。它们已纳入受限私有单步，实际可用性仍需宿主授权、相应内置服务及有效页面证据。

单步 **18 项**的完整清单见 [单步验证](ai-assistant.md#单步验证)。`action.fill` 仅填显式公开非秘密文本，不能填密码、凭据、Cookie、Token 或 OTP/TOTP 认证动态码，具体字段和内容仍受宿主校验；`flow.result` 单步只返回配置布尔值，不做模型判断，false 是该步骤失败，不是网站操作失败的证明。四项 `post.*` 孤立测试尚未实现，因为需要真实 SiteJob 消费检查点，不是用户拒绝权限；购买/补签改走保存的完整工作流，每次新派发前每个 post 步骤分别弹全新审核，全部批准后才开始，不为每个日期或内部点击再弹窗。插件虽可声明同样六类，元数据未验证、实际危险实现需另行审核，仍不开放助手访问或任意代码。

全部 `challenge.*`、`sign.iframe.image_captcha`、`sign.image_quiz` 通过宿主配置的提供商/运行时处理。2FA 仅支持 `builtin.twofauth`，需要凭据同意及已绑定、凭据可用的同源 manual 站点或 Bitwarden 来源；Cookie / none 不授权 2FA。真实认证动态码、TOTP 种子、密码、Cookie、隐藏 token、私有提示词和提供商密钥仍是秘密，绝不索取或回显。图片 CAPTCHA 的 OCR 文本和解题答案不一概归为秘密；用户“它不是秘密”可澄清图片验证码答案，不能解除真实 OTP/凭据的保护。配置的识别/视觉服务可能处理挑战图像，不能声称任何模型都不处理图像。

验证码后可继续读取安全 DOM、子树、选择器证据及独立授权截图（含 UI 预览），并在同页连续进行已授权的导航、等待、登录、填充、点击或结果单步，不要求重开，也不强制改用完整工作流。输入值仍从所有 DOM/选择器证据中省略，不能用截图恢复；主页面及可读同源 frame 的输入控件被遮罩，其余 frame 整体遮罩。可见页文反映图片验证码答案可以接受，不保证对任意秘密做完整像素脱敏；权限、同源、账本和不确定性门禁照常生效。

私有 Turnstile 单步还需明确批准 `https://challenges.cloudflare.com` 观察来源，并在当前助手页唯一定位可见的受支持 Cloudflare challenge iframe。Worker 绑定其原始节点、来源、文档及边界，仅允许 CFbox 坐标在该固定目标内且命中该目标时行动；非挑战坐标不执行。不开放任意全页坐标、全页挑战替代绑定、桌面/X11 或其他标签页操作。私有回包回显已知提供商密钥时拒绝，不把密钥作为答案填入网页；私有输入在 focus 后复查目标并原子校验/插入，不能因焦点事件替换节点而转填秘密。完整边界见 [私有验证边界](ai-assistant.md#私有验证边界)。

完整工作流的 `flow.result` 可按宿主策略使用模型判断，其运行配置、提示词和提供商密钥仅由宿主私下传给 Worker；助手只读安全布尔结果/状态，不读取原始模型答案、判断理由或密钥。这与不调用模型的孤立布尔测试不同。

若目标确实需要图片验证码，应查询 `challenge.image_captcha` schema，在独立草稿中按证据设计并人工审核；证据或服务尚未就绪时可先保留 `enabled: false` 候选。`auth.form_login` 会填写后立即提交，不能简单在其后追加验证码就假定能工作；必须观察实际挑战出现时机、提交顺序和成功状态，并据此测试连续单步及设计完整工作流。验证码处理后图片仍可能保留，不能只切换步骤开关而保留本示例的阻断条件。不要读取输入值或记录隐藏 token、OTP 等秘密；可见图片验证码答案本身不是秘密，也不应硬编码成可复用答案。

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
4. `run_site` 除公共 `title` 外只能传 `{}`，只把单个绑定已保存站点交给共享 executor，没有任意站点 ID、全部运行或 force。完整解析支持全部 23 项内置功能，包括公开非秘密 `action.fill` 和需逐次消费审核的四项 `post.*`；插件/插件方法/提供商仍拒绝，包括继承项。URL 必须为原始目标同源的公共、无敏感值完整地址，额外可信观察 origin 不扩大完整运行或凭据授权。新建运行的守卫哈希覆盖当前本地/解析配置、祖先模板和功能设置/策略，执行前漂移以静态 `assistant_configuration_changed` 拒绝；无消费计划时正常同代理模式通用到交互回退仍有效，带消费计划不允许自动 fallback/restart/重试 attempt。
5. 通过 `get_run_results` 读取 `{run_id, cursor: 0, limit: 50, wait_seconds: 0, snapshot_id?}`，limit 1-50、wait_seconds 0-20，建议 10 或 20 秒有界等待。只返回本会话关联运行的安全状态、白名单 reason/field/stage/cause 等诊断、布尔值和计数，不把任意错误正文当证据。步骤会原地更新，cursor 只相对于当前安全投影；非首页携带返回的哈希 `snapshot_id`，遇到 `run_snapshot_changed` 从 0 重读，不拼接不同投影的页。
6. `get_run_log` 是 **curated structured diagnostic**，参数 `{run_id, offset: 0, limit: 16000, snapshot_id?}`，limit 1-16000 字节，返回 `content` 分页及 `next_offset`，非首页同样需 snapshot。它不是原始日志 artifact，不回传页面文字、完整站点 URL、原始异常 dump 或凭据；snapshot/run ID 不能扩大该工具的会话范围。需读实际历史日志时，使用下节独立的 artifact 工具及其全文脱敏边界，不要直接粘贴未经处理的文件。
7. 完成后若是证据明确的已知失败，可按安全诊断调整验证条件/选择器，validate、submit、import 新修订，再测试；不是对旧绑定重复调用来“再跑”。同一提案导入、同一绑定修订加提案运行均幂等，返回原站点/运行身份，不重新提交。提交确认丢失返回 `ok: false`、`run_submission_uncertain` 并持久保留 run ID/unknown action，不自动补投，`submitted: false` 不是未执行证明。
8. Worker 的 `assistant_run` 模式对可能已生效的异常/超时记录 unknown，不当作普通失败。未知完整运行阻止新完整运行及配置写入，不能换提案/修订、UUID 或借单步重放整次运行；当前授权内非 post 单步仍按类别策略检查，不核销这些门禁。自动调度跳过未决站点。需人工在网站/VNC/运行 UI 核清；当前没有完整运行审批/核销 UI，单步 `retry_step` 能力不适用，普通手动运行也不会自动清除此状态，不承诺“批准后继续”。完整边界见 [导入与完整测试](ai-assistant.md#导入与完整测试)。

新的助手导入在新建/更新工作流时使用精确 actor `assistant`，生成的配置历史 `created_by` 为 `assistant`；新的完整任务同时保存 `Run.created_by: "assistant"` 与 `trigger: "assistant"`。真实人类身份保留在助手会话 owner 及关联 action owner 中，而非新运行的 `created_by`；会话/消息及 action 载荷加密保存，owner 索引元数据并非全是密文。UI 创建人显示英文 `assistant`，tooltip 为「来源：智能助手」，通知 `trigger_label` 也为英文 `assistant`，不展示人类发起人作为运行 actor。旧记录不迁移，任务 329 仍是 `manual` / `admin`，不能改称助手来源。

四项 `post.buy_card`、`post.resign`、`post.resign_click`、`post.resign_dialog` 每次真正新派发都须 owner 审核冻结的完整消费计划，包括条件启用步骤、全部过去待补签日期（窗口最多 365 天）、时区/当天、数量和费用上限。`purchase_quantity` 为 0-365 整数，0 按缺口减库存自动补齐，仍给出最大数量；`max_price` 必须为正整数，展示最大总花费而非网站实时报价。全局自动执行/工作流授权不能跳过；日期、授权及 Worker 检查点计划均由宿主私下生成。相同配置/提案重放仅返回原 run ID，不再弹审核或 submit，不自动 fallback/restart/重试。通用点击/导航的业务效果不总能识别，提示禁止用点击、填充、导航或循环绕过审核，不能宣称所有网页消费必被拦截。

**导入已有启用站点保留调度状态，后续自动任务可能执行新导入的购买/补签步骤。** 新消费审核仅约束助手调用，不是普通手动/调度入口的新门禁，后者维持既有策略；导入前先核对影响，新站点仍默认禁用。详细边界见 [购买与补签审核](ai-assistant.md#购买与补签审核)。

工具等待有界且占本轮预算；普通聊天轮次结束后不会自动监看任务或主动回到聊天汇报。若尚在排队/运行，只报告 run ID、当前状态及运行入口，用户可下一条消息要求读取完成结果，不虚构成功或承诺后台助手稍后回答。独立授权的失败接管不是普通轮次续跑。

## 复用与历史排查

已有站点的工作流可在「模板管理」新建/编辑器中「从站点导入配置」：选择站点并确认后，来源本地 `steps` / `groups` / `signals` 替换表单三集合，来源 `template_id` 映射为 `parent_id`，不展开拍平继承。当前名称、描述和启用状态保留，不复制站点元数据/秘密、不改变来源，**仅填表，不自动保存或运行**。本示例 JSON 刻意展开继承且改写了部分步骤，与实际导入行为不同。

用户明确要求后，助手可用 `create_template_from_site` 保存已绑定 `workflow_site_id` 的当前本地工作流，不接受任意来源 ID 或凭据源 `site_id`。来源须可校验且无活动/未知任务，**不要求已有成功助手完整运行**。每次用新 `operation_id`、name 和可选 description；替换现有模板还需 `destination_template_id`、当前 `expected_revision` / `expected_history_key`，保留目的名称/描述/enabled，只替换父模板与本地三集合，不合并。每次精确宿主审核不能被自动授权跳过；准备冻结来源/目的快照及依赖，批准时复查并 CAS，漂移/拒绝/超时不写入，相同 UUID/请求只重放回执，不重复保存或执行。详见[从站点保存模板](ai-assistant.md#从站点保存模板)。

助手替换在准备及应用时检查**解析后的原有结构，含继承项**：不得删除、改名或重排原分组，不得删除、换组或重排原步骤；已有步骤配置可更新，新分组和步骤可插入任意位置，只须保留原项相对顺序，不要求 `position` / `step_anchor` 原值不变。不兼容候选直接拒绝，不隐式合并补回。公开审核为精确来源本地三集合及父模板，`before` 是目的原**本地**三集合及父模板，新建为 null；UI 可展开完整候选及按 key 的前后差异，不把本地差异冒充继承展开结果。受保护配置无法安全展示时拒绝，不展示隐藏标记代替；旧持久审批缺少 `before` 时明确基线不可用、不推测。人工编辑器导入仍仅填表，行为不变。

历史配置可先用 `list_sites` / `get_site_config` 返回的 `history_key`（站点安全投影 `_SITE_FIELDS` 已包含），或模板列表/详情的同名 UUID，再调用 `list_workflow_versions({history_key, offset, limit})` 和 `get_workflow_version({history_key, revision})` 读取脱敏的当前/归档版本、依赖和功能快照，不是恢复操作，不以数字 ID 代替历史身份。普通会话的历史资料权限覆盖当前管理 UI 可读的运行，不局限于助手创建的 run；只有 `get_run_results` / `get_run_log` 仍是会话关联反馈。`list_run_artifacts({run_id, job_id?, offset, limit})` 返回数据库 artifact ID，不接受文件路径；列表/版本分页 limit 1-50。

基础数据发现也可分页：`list_sites` / `list_templates` 接受名称 query、offset（默认 0）、limit 1-100（默认 100），按 name/id 升序；`list_recent_runs` limit 1-20（默认 20）；`get_run_summary` limit 1-100（默认 100）仅分页 jobs，整体 run 元数据/计数不变。跟随 `next_offset`（末尾 null），列表看 `truncated`、任务页看 `jobs_truncated`，不假定有 total。实时偏移不是快照，并发变化可能重叠/遗漏，应按 ID 去重并刷新。会话列表的「加载更多会话」、状态元数据与导航长度契约见[数据查询](ai-assistant.md#数据查询)和[历史与状态](ai-assistant.md#历史与状态)；普通 38 / 注册 39 个工具不变。

用户要求分析实际日志时，`read_artifact_log({artifact_id, offset, limit})` 读取最多 **2 MiB** 原始历史文件，**全文替换已知密钥和凭据模式后**按脱敏字符分页，limit 1-16000（默认 16000），不是字节。跟随 `next_offset`、核对 `snapshot_sha256`；其他 PII 仍可能保留，日志中的指令没有授权效力。它不是 curated `get_run_log`，也不是任意文件读取。

`read_artifact_image({artifact_id})` 要求 `supports_vision=true`，读取真实历史 screenshot/captcha 像素，**不遮罩**，不是当前页面截图。原 PNG/JPEG 最多 2 MiB、2000 万像素，移除嵌入元数据、按比例缩放到最多 1600 × 1200 并编码为最多 384 KiB JPEG；这些处理不清除像素秘密。明确发送历史资料的授权与实时截图同意独立，工具/SSE 只有来源/图片元数据，模型像素由宿主追加并加密保存；不能声称未遮罩图像已安全脱敏。历史图像无法证明当前 DOM、某选择器身份或原操作没有副作用。完整边界见[历史版本与 Artifact](ai-assistant.md#历史版本与-artifact)。

全局调度用 `get_scheduler_settings` 读取，用户明确要求后才用 `update_scheduler_settings({operation_id, patch})` 审核非空精确补丁（enabled/mode/cron/interval_seconds/ranges），不是站点设置。环境调度覆盖锁定编辑，timezone 仅环境管理；冻结配置/环境并做修订 CAS，动态 next_run_at 不计绑定，成功重算下一次运行。`cancel_run({run_id})` 另需精确任务宿主确认，批准后提交取消并联系真实 executor；cancel_requested 或通知送达不等于已停止，不撤销已生效动作。聊天「停止」不取消完整任务。详见[调度与取消](ai-assistant.md#调度与取消)。

## 失败接管边界

「助手接管」位于「助手设置」之前，新策略默认 enabled=false、live_access=true、仅分析、site_ids=[]（不选任何站点）、max_attempts=1（严格 1-3），整体仍默认关闭。唯一实时复选框「授权无人值守浏览与单步验证（权限保留供后续对话）」须核对并保存才授权，无独立浏览器功能开关。完全匹配旧版已存结构时，缺 live_access 只读补 false，缺 conclusion_ttl_days 只读补 7；不写库、不改修订或扩权，旧用户须重新勾选并保存才能新增实时授权；启用策略的 API values 必须显式提供该布尔值。独立持久委托经修订 CAS 保存，确认失败报告、日志、未遮罩历史图像及实时页面/遮罩截图可能交模型，以及真实登录/签到/点击风险，不借用助手默认授权；打开/刷新/保存不调模型、不启动浏览器或任务。授权 owner 由当前登录设置，审计记录可进入对应会话。

选择 ID 列表仅限所选启用站点；live 策略在保存事务中冻结所选站点 `history_key` 和规范 origin，连同策略修订写入独立 `AppSetting` 键 `assistant.takeover.site_grants`。准入复查修订、完整 ID 集合及身份/origin；旧 live 策略缺绑定或绑定无效时 fail closed，须核对并重新保存，不从当前数字 ID 自动补授权。null 明确授权所有当前及未来启用站点，无需预存未来站点绑定，但实际 action 仍冻结具体历史身份、origin、策略修订和来源。数字 ID 复用、origin 或绑定变化不能沿用旧授权，须重确认；导航不能修改授权范围。接管失败、错误和需要人工的任务；已推迟不接管。只处理策略 effective_from 后创建、已结束且状态为 fail/error/waiting_human 的手动/调度任务；跳过取消、unknown、中断、deferred、未完成任务和助手运行，不回填历史任务。

重启/重新部署不会因新规则扩大准入而回填旧任务。周期发现另要求 Run 在本次 API 进程创建接管管理器时或之后结束（UTC `Run.finished_at >= _scan_started_at`），仍须满足策略生效后创建等条件；启动前开始、启动后结束的运行可以入队。已有 queued action 仍恢复处理；dispatched 若有精确已接受用户消息及有效同 owner 会话，则保留供持久证据完成恢复，否则启动回退仍按导入回执记 updated、无回执记 unknown。恢复不重排队、不重放模型或网站动作，详见[结论与通知](ai-assistant.md#结论与通知)。历史结论去重范围不变；此边界只限周期扫描，不改变显式终态通知规划入口。

历史失败或与当前来源无关的 unknown 记录本身不阻止合格新故障入队和分析；有效 live_access 下可浏览及按当前类别授权执行支持的同源单步，auth 共享额度。当前来源自身的 unknown、中断、取消、deferred 仍不触发接管。配置保存/导入和完整运行的不确定性门禁仍有效，不自动重放历史部分消费，具体能力以当前宿主检查为准。自动阶段仍拒绝 retry_step，允许新 execute_step 不等于开放重试工具。waiting_human 挑战可在实时授权和宿主检查通过后尝试，不保证无副作用或所有功能可自动处理；暂不复用旧挑战分析，每日聊天路由复用不是挑战结论复用。以 Piggo 为通用示例，旧失败/unknown 不能代替有效调查结论，也不能据此跳过新故障分析；此例不是生产站点验证或修复成功声明。

**analysis 仅禁止保存配置，不保证网站只读或没有副作用。** 两种模式在 live_access=true 时都可无人值守浏览、支持视觉时截图及执行冻结清单内最多 18 项真实同源单步，包括可用私有凭据登录、提交表单、签到、点击和挑战。凭据及 Token/能力令牌不进入模型或日志，DOM 输入省略、实时输入遮罩、功能策略、操作账本与未知门禁不变；插件功能/方法/提供商仍拒绝。live_access=false 则不浏览或单步。实时代理按站点已保存车道派生：proxy_general/proxy_interaction 使用 proxy_interaction，其余使用 interaction，无 fallback。接管浏览器从首次导航起严格原始 origin，不自动批准额外来源；本指南普通人工发现的跨源确认流程不能被自动接管借用扩权，需要额外来源的挑战也不能自动获准。

autofix 仅在实际失败派发快照仍匹配当前绑定配置/依赖/策略、且无活动或未知任务时，将校验后的已存提案一次导入现有来源工作流，**不自动完整运行**，不新建、不取消、不改凭据、站点元数据、模板或全局设置。资料只限来源 run/job；每次自动工具/导入复查授权修订、owner 与范围，策略变更撤销旧自动委托，不撤回已提交修改或已授予的人工会话权限。缺失/过期快照不能用于保存；自动阶段不开放 flow.jump、四项 post.* 孤立动作、完整运行或重试审批，当前门禁仍拒绝 retry_step，新 execute_step 按类别授权和额度检查。设置、模板、全局变更和取消仍须人工明确要求并逐次宿主确认；人工消费完整运行每次新派发仍须逐 post 步骤审核，全部批准后才启动，不按日期或内部点击再次弹窗。updated 只表示保存，不证明修复成功；完整准入、撤销与审计见[失败接管](ai-assistant.md#失败接管)。

相同故障去重独立于每日聊天：先复查当前授权、来源和活动任务，再匹配 owner + 站点历史 UUID/规范 origin + 仍匹配当前配置的实际派发完整指纹/修订 + v2 失败签名。签名纳入车道、全部失败步骤 key/功能/状态、reason/code/field/stage 等白名单事实及规范化 message；非白名单时间戳/耗时等不参与，消息内容变化仍区分。去重元数据只存哈希和绑定，不存原 message/字段路径；缺稳定事实的通用错误保守不合并。只有有效 completed_analysis/updated 正式结论及类型明确的成功轮次结果才能抑制新分析，updated 另须有效导入回执；失败、unknown、排队、坏/缺结论或仅有签名都不是成功标记。无关历史 unknown 不阻止有效结论复用，因为复用不派发新动作，也不解除变更安全门禁。

跨日复用不是无限期：接管 UI 的 `conclusion_ttl_days` 为严格整数 1-30、默认 7；新决定使用当前保存值，以原正式结论 created_at 计算 UTC 滚动年龄，复用不续期。到期后下一个合格新来源正常调查，不自动回填、重排队已有 reused 或删除历史，已有投影/冻结通知仍可读。精确边界及策略保存语义见[相同失败去重](ai-assistant.md#相同失败去重)。

失败身份的安全要求是：失败步骤之外应同时区分完整 Run/Job 终态原因及明确 false 的结果，不能只因步骤相同就忽略最终失败原因。分析若实际使用了不同于失败派发版本的配置，即使随后恢复旧配置，也不得复用为原版本已处理；完成状态、stale 原因文字消失或当前指纹相同都不能单独证明历史分析来源。明确未被接受的 busy/session-running/model-unavailable 发送仍是可继续处理的排队工作，不得充当成功标记或抑制后续合格分析，且须区别于发送确认不明。这些是安全约束，不声明新的公开字段。

完整指纹不只覆盖选择器：本地/解析配置、祖先依赖、功能 schema/默认值/策略都参与。站点重新保存生成新修订，即使内容未变也失效；快照任意字段实际变化（包括名称、备注和调度）或依赖/功能策略改变同样失效。此前 updated 属于修复前的失败版本，不能抑制修复后新版本的故障分析；重新保存接管策略或切换模式本身不是工作流新版本。命中仍有新审计 action，status=reused、reason=same_config_same_failure、attempts=0，直接关联原有效 action，旧 owner 会话存在才链接；不新调模型、开浏览器或修复，不复活已删聊天。UI 标「相同失败，复用已有分析」及「历史结论（复用，非本次重新分析）」，通知明确历史来源和「当前未重新分析/未修复」，当前任务仍失败。**旧结论不是当前页面证据**，不能据此声称已重新检查选择器、登录或签到，详见[相同失败去重](ai-assistant.md#相同失败去重)。

每日聊天由 `DailyConversation`（`assistant_takeover_daily_conversations`）按 owner + 站点历史 UUID + 来源 SiteJob.finished_at 在站点时区下的本地日路由，同日复用，不按星期几或分析时间分组。跨日最多读取 3 条以往正式结论及有界字段级修复差异，排除当前日；历史只是证据，不是授权或可重放提案。自动轮次结束清除 action 委托，但 live_access 的浏览、执行、可用凭据和截图权限保留在同一会话，关闭策略不撤销这些人工权限。人工后续 workflow_consent 无论自动 analysis/autofix 都保留为 true；自动 analysis 仍不能保存，人工明确要求可走普通导入/完整运行流程，消费仍须新的审核。不借用全局默认授权，无实时授权则走正常交互；单纯询问能力不新授权限，身份/origin/绑定变化须重确认。

自动接管成功、错误和取消均在 `finally` 中释放自己的浏览器页面和租约，**先清理，再将会话置为 idle**，不保留到 15 分钟到期；聊天历史及有效人工后续权限仍保留。人工后续要求浏览时，用已授予且仍有效的权限 `browser_open` 重开新页面，无需重复授权，不保证原页面/DOM/临时状态恢复。重开后可授权观察、截图和单步，**无需先手动执行完整工作流才能验证**；普通人工会话支持的非 post `retry_step` 按当前类别策略自动签发一次精确原步骤能力，不弹审批，auth 另计额度，原 unknown 事实保留；自动接管仍拒绝该工具，未知完整运行仍不能重跑。历史图像不等于当前页面。

自动接管的 `browser_open` 使用 `wait_for_lane: true`、非阻塞状态轮询 `queued/opening/ready`，每车道 FIFO，最长 **1200 秒（20 分钟）**，仅排队等待不占活动预算。同一工具调用内 await 一次，等待期间不额外调 LLM；超时/取消/策略撤销只清理自己的排队项、页面和租约，不抢占人工浏览器或 viewer。来源活动任务、拾取器共用车道执行信号量；人工租约正常释放/到期且 viewer 断开后才可准入。两车道队列独立，但主自动消费者仍串行，不同时运行两个自动浏览接管，不宣称跨车道并行。人工助手浏览器仍从准入/创建起固定 **15 分钟**，消息不续租，不是最后消息闲置计时；`idle_stop_seconds` 默认 **300 秒**，仅无 lease/viewer 后才计车道空闲关闭，不是助手 TTL。完整约定见[排队与所有权](ai-assistant.md#排队与所有权)。

`submit_takeover_conclusion` 提交最多 240 字符的纯文本加密候选，宿主核对精确 action/request 及确定结果后才提升。任务结束即发送原任务通知，不等待模型；注册时宿主本地规划符合 fail/error/waiting_human 准入的 action（deferred 排除）并做同故障去重，不调用模型或浏览器。需要新模型复查的站点在原始脱敏消息前添加「助手复查中，结果稍后发出。原任务结果：」；有效历史复用结论直接进入该站点消息并注明历史来源，不发第二条调查通知。事件、渲染消息及通道目标冻结加密保存为 ready，原任务自定义布局、数据库原消息、状态、统计和筛选不变，waiting_human 不改为 success。实际接管完成且有有效简短 `completed_analysis` / `updated` 结论时才另发独立纯文本调查简报，不套用用户任务布局，HTTP 事件为 `assistant.investigation`。邮件/HTTP 各自的「接收助手调查通知」对应 `notify_assistant`，默认 true；只走已启用且开关开启的通道，不依赖原任务成功/手动筛选，读取/保存设置不调用模型。failed、unknown、revoked、排队、缺失或无效结论不发；updated 只表示配置已保存、未重新运行验证。完成/正式结论与 `assistant_takeover_notices` 在同一事务提交；原任务通知先完成发送尝试，再派发调查通知。独立账本以 action ID 唯一登记，发送前持久认领、最多一次尝试，重启不明状态记 unknown、不重发，不保证恰好一次送达或收件顺序，见[运行通知](run-notifications.md)。

这些知识在配置的数据库中，Compose 默认 SQLite `/app/data/siteflow.db`（`siteflow_data` 卷）：`assistant_takeover_actions` 审计、`assistant_job_config_snapshots` 加密派发快照、每日路由，以及加密的 `assistant_takeover_conclusion_candidates` / `assistant_takeover_conclusions`。`assistant_takeover_analysis_provenance` 保存分析实际观察配置与来源派发配置的一致性证据，缺失时不能作为有效历史分析复用，也须备份。调查通知另存独立 `assistant_takeover_notices`，不复用原任务通知的发送状态。正式结论、审计和通知账本不随聊天删除；恢复需数据库备份及单独保管的 `SITEFLOW_MASTER_KEY`，不是依赖模型记忆，详见[记录存储](ai-assistant.md#记录存储)。

备份同时保留 `AppSetting` 的 `assistant.takeover.settings`（规范 owner、修订与授权/生效时间）及 `assistant.takeover.site_grants`（同修订所选站点身份/origin）；owner、规范每日会话路由和绑定元数据不全加密。新记录表由 `create_all()` 创建，实时字段和绑定使用现有 JSON/设置记录，不新增旧表列，也不提供历史开发版列迁移。严格同源约束在首次 Worker open 中即传入、首次导航前生效，不是打开页面后再收窄。

去重记录另在 `assistant_takeover_failure_signatures`（失败哈希/配置绑定）和 `assistant_takeover_dedup_links`（直接来源及复用时间），独立于聊天保留。原任务通知在任务结束后立即发送，不等待接管；复用由发送前的本地规划/去重完成。命中时历史结论直接进入站点消息，并复查原始结论。规划时失效或缺失则不复用，仍须通过当前准入才能新复查；**已提交为 reused 后证据才失效，只隐藏历史结论，不自动重新排队调查**。不能展示未经验证的历史结论，也不为复用另发开始复查通知。

## 性能说明

当前静态可见修正通过 `_phase` 将接管启动恢复、后台 reconcile、process_one 的选择、运行时预检、会话创建、认领、发送失败处理和完成等同步阶段卸载到线程。各数据库 helper 自行创建/关闭 session，只返回 ID、标量和序列化证据，不跨线程传 ORM 对象；模型发送、任务等待和唤醒由事件循环协调，取消等待已启动线程阶段结束并处理认领后未发送的清理。executor 准备/终态数据库阶段、接管策略保存、助手 SSE 认证/详情/JSON 编码也在线程执行，未变化的 running SSE 只 heartbeat。前端按内容缓存工具 JSON 解析，完整格式化和原文渲染延迟到展开；提案、工具原文、审批/停止、图像遮罩与未知结果门禁保留，不靠删除功能减少开销。SSE 仍定期复查认证/归属并构造详情，不是跳过安全读取或逐 Token 输出。

`TakeoverPhaseCache` 在通知注册 `plan_run_takeovers()`、协调器 reconcile、选择 `_select` 和认领 `_claim` 中分别创建，只面向同一同步事务阶段、同一 session/策略视图的 run/site 事实，不跨阶段、await、commit 或授权/配置变化，也不代替执行/导入复查；认领使用新缓存重新核对来源和策略，不继承选择阶段的授权判断。这些边界不保证任何负载下绝不卡顿，完整范围见[性能与边界](ai-assistant.md#性能与边界)。
