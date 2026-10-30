# 任务执行

## 1. 范围

执行器第一阶段提供可运行的站点工作流队列：

- 创建运行记录：全部启用站点或指定站点。
- 每个站点生成一个 `site_job`，每个启用的工作流步骤生成 `job_steps`。
- 不同车道可并发执行站点任务；普通车道默认并发 5，代理普通默认 3，交互车道固定 1。
- 运行队列限制并发运行数和全局任务数。
- 系统调度器支持 cron、固定间隔和随机时间范围。
- 支持手动取消、取消当前步骤、超时和失败状态。
- 每个站点任务生成执行日志；步骤只要执行后不是成功状态（包括 `fail`、`error`、`unknown`、`waiting_human`、`canceled`）就保存截图；跳过的步骤不截图。
- 图形验证码读取时保存裁剪图片为 `captcha` artifact，识别结果写入详细日志。
- 运行详情可在「详细日志 / 状态日志」之间切换；详细日志来自 `job.log` artifact。
- 存在失败截图或验证码截图时，站点标题行显示「查看截图」按钮，可弹窗预览和下载；多张截图支持前后切换。

当前不包含通知和自动重试。

## 2. 数据流

```text
Web POST /api/v1/runs
  → runs / site_jobs (queued)
  → ExecutionManager 运行队列 / 任务队列
  → 按车道并发执行站点任务
  → 解析站点工作流 + 解密本地登录凭据（Bitwarden 延迟到登录步骤）
  → POST worker:/execute
  → worker.lease_lane()
  → 独立标签页逐步骤执行 / 条件与信号判断
  → 返回步骤结果
  → API 写入 job_steps / 汇总 runs
  → 关闭任务标签页 + worker.release_lane()
```

系统调度器每 30 秒检查一次调度设置：

- 定时调度：cron 表达式触发一次运行。
- 间隔调度：上次触发后经过配置间隔再次触发。
- 随机调度：每个时间范围每天随机选择一个稳定时间触发一次。
- 每次触发会收集所有已启用、已过 `checkin_after` 且当前没有运行中任务的站点。
- 同一时间只允许一个调度运行在执行；站点配置只保留 `checkin_after` 单次时间。
- cron 和随机范围按 `SITEFLOW_SCHEDULER_TIMEZONE` 计算，默认 `Asia/Shanghai`；不依赖容器系统时区。

Turnstile/滑块/图形验证码优先在站点配置的车道用 Playwright 截图和鼠标点击执行。Turnstile 连续两次点击仍未生效时，任务自动改到交互车道重试，并使用浏览器后端的桌面能力（当前为 mss + xdotool）fallback。

API 与 worker 不共享数据库。API 负责本地凭据解密、Bitwarden 查询参数和状态持久化；启用 Bitwarden 的站点会把查询参数随任务下发，worker 在任务启动后后台预取，真正到 `auth.form_login` 时使用，任务上下文结束后丢弃。凭据不写日志。

## 3. 状态

运行和站点任务：

```text
queued / running / success / fail / error / deferred /
unknown / skipped / waiting_human / canceled / interrupted
```

步骤：

```text
success / skipped / fail / error / deferred / unknown /
waiting_human / canceled / interrupted
```

- 条件无法判断：步骤 `waiting_human`，停止该站点任务。
- 不支持的验证功能块：步骤 `waiting_human`，等待人工处理。
- 本地登录凭据缺失：站点任务直接失败，不启动浏览器登录步骤。
- Bitwarden 凭据：任务启动后后台查询，进入 `auth.form_login` 时使用；登录步骤未执行时不会等待查询结果。
- API 调用 worker `POST /execute/stream`，worker 以 NDJSON 逐步返回进度；API 在每个步骤开始/结束时写入 `job_steps` 并更新任务心跳。
- 运行详情通过 SSE (`GET /api/v1/runs/{id}/events`) 推送完整快照，步骤或站点状态变化时立即更新；运行列表仍按 5 秒轮询。
- 前端打开详情时按站点显示子标签，多站点运行可直接切换；SSE 断开时退回 5 秒轮询。
- 所有步骤成功或跳过：运行成功。
- 任一站点等待人工：运行等待人工。
- API 重启时，未完成运行标记为 `interrupted`。

## 4. 已支持功能块

已接入：

```text
flow.open
auth.form_login
challenge.2fa
challenge.turnstile
challenge.altcha
challenge.slider
action.navigate / action.wait / action.click / action.fill
flow.result
sign.button / sign.iframe.image_captcha / sign.image_quiz / sign.unit3d_claim
post.buy_card
post.resign
```

- `flow.open`、`action.navigate`：导航完成后检测页面标题和正文中的错误页特征，例如 Bad Gateway、Service Unavailable、502/503/522、Internal Server Error、404、网站维护/正在维护/关闭维护、The website has been stopped 等，即使 HTTP 返回 200 也会检测。`flow.open` 的网络异常和错误页共用 `retry_count` 次额外重试（总尝试次数为 `1 + retry_count`）；错误页重试临时禁用当前任务标签页的缓存，强制重新访问原目标地址，不因 URL 相同而跳过，步骤结束后恢复缓存。不会清除 Cookie 或整个 profile 的缓存。`retry_count=0` 和 `action.navigate` 不额外重试。次数耗尽仍是错误页时返回 `fail` 和 `error_page` 数据，随后按该步骤的错误策略 `stop`/`continue`/`defer`/`escalate_interaction` 处理。
- `challenge.2fa`：通过 2FAuth 按 `service_hint` 匹配账号并获取 OTP；多个候选或没有候选时返回 `waiting_human`。
- `challenge.turnstile`：先检查 `.cf-turnstile`、`cf-turnstile-response`、`cf-chl-widget`、CF iframe、挑战文案；存在挑战时截图调用 CFbox，按复选框中心点击。CFbox 返回 `absent` 但 DOM 仍有挑战时继续等待，不直接判成功；完全没有 CF 标记约 2 秒后才视为无挑战。`wait_seconds` 是从步骤开始计算的总等待上限，包含 DOM 检测、CFbox 调用和点击后的等待，不是固定延时；未配置 CFbox 返回 `waiting_human`。
- `challenge.altcha`：定位 ALTCHA 组件并点击复选框，等待组件进入 `verified` 状态或写入 Token；`wait_seconds` 控制最长等待时间。
- `challenge.slider`：拖动手柄到容器右侧；配置了 `success_selector` 时等待成功元素。
- `sign.image_quiz`：读取签到页面上的答题图片、可选题目文字和选项，调用视觉模型选择答案，填写留言并点击对应选项提交；模型无法确定答案时按 `refresh_selector` 刷新换题，超过 `max_refresh` 或未配置视觉模型时返回 `waiting_human`。答题图片会保存为截图 artifact。提示词在「功能管理」的执行行为中全局配置，不进入步骤配置。
- `sign.unit3d_claim`：点击 Claim 按钮后等待成功条件，支持成功文本、出现元素或消失元素三种判断，任一满足即成功；都未配置时点击后直接成功。
- `sign.iframe.image_captcha`：在签到 iframe 内填写静态字段，识别图像验证码并填入，然后点击 iframe 提交按钮；验证码校验、刷新和任务重启逻辑与 `challenge.image_captcha` 相同。
- `challenge.image_captcha`：截取验证码图片，按步骤的 `captcha_method` 选择识别方式后填写；默认 `ddddocr`，也可选择 `vision`（需配置视觉大模型并开启支持视觉）或 `nexusphp_gd5`（支持 NexusPHP GD 5 号字体、6 位字符，标准尺寸 150×40，其他显示尺寸会自动缩放）。配置了 `submit_selector` 时自动提交，否则只填表，交由后续登录/签到步骤提交。`charset` 校验字符集（纯数字、纯小写/大写/大小写字母、数字+字母或不限制），`length` 校验字符数（0 不校验）。`min_confidence` 仅用于 ddddocr。任一校验不通过时先按 `refresh_selector` 刷新验证码（未配置则点击验证码图片），图片未变化或刷新后仍不通过时重新开始任务，最多重新开始 2 次；视觉模型和 NexusPHP GD5 不会静默回退到其他方式。
- `post.buy_card`：按待补签日期减去当前补签卡数量，自动购买缺额；动态识别商店中的补签卡商品、option、价格和魔力值，按配置间隔购买。商品标题支持配置多个候选，按顺序匹配。
- `post.resign`：读取待补签日期，检查 NexusPHP FullCalendar 日历，翻月定位漏签日期，通过 `ajax.php` 的 `attendanceRetroactive` 执行补签；不负责买卡。
- 待补签日期由 API 按站点「支持补签最大天数」生成并注入 worker：从该站点最早有运行记录的日期开始，窗口内没有成功运行或成功补签记录的日期都算缺失；今天等当日结束后再判定。补签结果写入 `site_resign_records`，同时合并到状态矩阵和趋势。
- 站点 Cookie 由系统主动维护：任务开始前注入，站点任务成功后从浏览器导出当前站点 Cookie 并加密更新；失败时保留原配置。
- `post.buy_card` 和 `post.resign` 不再配置历史天数、购买数或补签数，统一使用站点配置的最大天数。
- 魔力值不足时 `post.buy_card` 只购买可负担部分并继续，`post.resign` 按现有卡数补签，剩余日期保留到下次。
- 模板和站点解析信号始终包含系统信号「断签」(`sys_resign_pending`)；worker 用待补签日期数量计算真假，站点不支持补签时恒为假。条件日志会输出待补签天数。
- `post.buy_card` 未配置步骤超时时会根据购买数量和间隔自动放宽，避免默认 30 秒超时。

`action.*` 和签到步骤支持候选选择器按顺序尝试。`sign.*.success_signals` 按页面文本匹配；`sign.unit3d_claim` 还支持 `success_selector`（成功后出现）和 `success_when_missing`（成功后消失）。

`flow.jump` 检查页面文字（不区分大小写）或选择器；命中后可按配置跳转到任意步骤 key，或重新开始任务。未命中时继续执行后续步骤；`max_jumps` 防止条件持续命中造成死循环，超过上限任务失败。

`flow.result` 不操作页面，按步骤条件输出布尔值 `output_value` 和信息 `message`。条件满足并执行后，会把信息写入站点任务消息、把输出值写入任务输出，并立即结束当前站点任务，不再执行后续步骤；`output_value=false` 时站点任务状态记为 `fail`。条件不满足时跳过并继续。功能块支持多例，可配置多个带不同条件的结果出口，适合“已签到/未签到”分流。

所有实际执行的 `sign.*`、`post.buy_card` 和 `post.resign` 步骤也会输出：

```text
value: true / false       # 步骤是否成功
message: 步骤结果信息
```

输出同时写入步骤数据和站点任务的 `result_json.outputs`；失败步骤输出 `false`。

每个站点任务在车道内创建独立标签页，任务结束后关闭该标签页。车道启动时保留一个欢迎页作为常驻标签页，因此关闭最后一个任务标签页不会关闭浏览器。

## 5. 条件与信号

- `always`：直接执行。
- `signal:<key>:true|false`：执行前在页面求值。
- 信号包含文本/选择器、排除文本/选择器，支持 `any`/`all`。
- 信号在顶层页面和所有 frame 中查找，最多等待 3 秒让动态元素出现；仍不满足才判定为否。
- 如果挑战/元素注入可能超过 3 秒，建议在信号步骤前加 `action.wait`，或把 `flow.open` 的加载条件设为 `networkidle`。
- 信号无法求值（页面异常、信号不存在）时停止并等待人工，不猜测。
- 步骤的 `on_error` 支持 `stop`、`continue`、`defer`、`escalate_interaction`；`continue` 的步骤失败仍显示为失败，但不会单独导致整个任务失败。

## 6. API

```http
GET  /api/v1/runs?limit=50
POST /api/v1/runs                 {site_ids?: number[], force?: boolean}
GET  /api/v1/runs/{id}
GET  /api/v1/runs/{id}/events        SSE 运行进度
POST /api/v1/runs/{id}/cancel
GET  /api/v1/artifacts/{id}          日志/截图；download=1 强制下载
POST /api/v1/lanes/{key}/focus       定位运行中任务标签页 {job_id}
GET  /api/v1/analytics/matrix        状态矩阵
GET  /api/v1/analytics/trends        执行趋势
GET  /api/v1/analytics/failures      失败分析
```

`site_ids` 省略时执行所有启用且有效的站点；无效或无步骤的站点会自动跳过。显式指定的站点必须存在、启用且工作流有效。写操作要求会话和 CSRF。

## 7. 配置

```dotenv
SITEFLOW_EXECUTION_TIMEOUT=600       # 单站点任务最长等待秒数
SITEFLOW_EXECUTION_MAX_RUNS=2       # 同时执行的运行数
SITEFLOW_EXECUTION_MAX_JOBS=10      # 同时执行的站点任务数
SITEFLOW_LANE_GENERAL_CONCURRENCY=5
SITEFLOW_LANE_PROXY_GENERAL_CONCURRENCY=3
SITEFLOW_ARTIFACT_ROOT=/app/artifacts
SITEFLOW_ARTIFACT_RETENTION_DAYS=30
SITEFLOW_ARTIFACT_CLEANUP_INTERVAL_SECONDS=21600
SITEFLOW_ARTIFACT_CLEANUP_ENABLED=true
SITEFLOW_SCHEDULER_ENABLED=true
SITEFLOW_SCHEDULER_POLL_SECONDS=30
SITEFLOW_SCHEDULER_MODE=cron
SITEFLOW_SCHEDULER_CRON="0 10 * * *"
SITEFLOW_SCHEDULER_INTERVAL_SECONDS=3600
SITEFLOW_SCHEDULER_RANGES=08:00-10:00,18:00-20:00
```

调度设置在「系统设置」页面配置：

调度模式四选一：关闭调度、定时调度、间隔调度、随机调度。切换到关闭调度即关闭自动触发，切换到其他三种模式即开启自动触发；手动运行始终不受影响。

- `cron`：如 `0 10 * * *`
- `interval_seconds`：间隔秒数
- `ranges`：如 `10:00-10:10`、`18:00-18:10`

开关和调度参数保存在数据库；`SITEFLOW_SCHEDULER_ENABLED` 等环境变量存在时对应设置只读。任务调度页顶部会显示下次自动运行时间；关闭时提示“未开启自动调度，请在 系统设置->调度设置 中启用”。

随机调度的每个范围每天最多触发一次，具体时间在范围内随机且当天稳定。环境变量存在时页面只读并显示「环境变量」来源。

worker 不可达时，站点任务标记为 `error`；不会自动重试。

Turnstile 点击先用 Playwright 截图和鼠标点击。只有页面上的可见挑战消失后步骤才结束，不能仅凭 `cf-turnstile-response` token 或 URL 变化提前开始下一步。检测只认可见的 Turnstile 组件、挑战文案或挑战 URL；隐藏的 response input 和隐藏的 Cloudflare iframe 不算挑战。

如果页面跳转后仍然存在新的 CF 挑战（例如入站 CF 后登录页又出现 Turnstile），步骤会重置点击计数并继续处理新挑战，而不是误判失败。token 更新且仅保留组件本身不视为新挑战。CFbox 连续返回 `verified`、页面存在非空 Turnstile 响应 token，且页面已就绪、读取正常、无挑战文案和挑战 URL 时，稳定两秒后通过，允许已验证的组件继续显示。缺少 token 的可见组件不能仅凭 `verified` 放行。

信号条件读取页面标题、frame 文本和选择器时每个操作最多等待 2 秒，单次信号判断最多 10 秒；超时按无法判断处理，避免任务卡死。

连续两次点击后仍为 `unchecked` 时，会再等待约 4 秒确认状态：

- 当前是普通/代理普通车道：任务改到交互/代理交互车道重试。
- 当前已经是交互车道：切换桌面截图与系统坐标点击。
- X fallback 使用后端为该车道分配的 display，Worker 只保留 mss/xdotool 客户端。

## 8. Artifact

- worker 为每个站点任务写 `job.log`，记录任务开始/结束、步骤开始/结束、状态和消息。
- 图形验证码识别后，详细日志记录识别方式、验证码文本、置信度（ddddocr）或模板匹配余量（nexusphp_gd5）。
- 步骤执行后不是成功状态时通过 Playwright 截取当前页面，写入 `failure-<step_key>-<n>.png`；失败/错误标记为失败截图，其他状态标记为未成功截图。
- 若任务最终不是成功状态且没有任何步骤截图，会补一张任务级截图。
- 图形验证码读取时保存原始裁剪图片为 `captcha-<step_key>-<n>.png`；刷新验证码时同时保存刷新后的图片。
- 文件通过 `siteflow_artifacts` 共享卷挂载给 API 和 worker，默认根目录 `/app/artifacts`。
- 数据库 `job_artifacts` 只保存元数据：类型、步骤、相对路径、MIME、大小、sha256、创建时间。
- API 运行详情返回 artifact 元数据；`GET /api/v1/artifacts/{id}` 需要登录，按文件 MIME 内联返回。
- 前端不显示 artifact 文件列表；失败截图和验证码截图通过站点标题行的预览按钮查看。
- 后台清理任务默认每 6 小时执行一次，删除超过 30 天的文件和数据库记录，并清理过期孤儿文件。
- 系统设置显示日志/截图数量与空间占用，支持手动「立即清理」。
- 清理策略通过 `SITEFLOW_ARTIFACT_RETENTION_DAYS`、`SITEFLOW_ARTIFACT_CLEANUP_INTERVAL_SECONDS`、`SITEFLOW_ARTIFACT_CLEANUP_ENABLED` 配置。

## 9. 后续

- 定时调度的重试策略和通知。
- 其他视觉识别/识图能力（图形验证码已支持 ddddocr、视觉模型和 NexusPHP GD 5）。
- 截图/日志 artifact、站点每日状态、趋势报表。
# Cloudflare 双重确认

浏览器读取与交互统一分离：Page/Frame/Locator/ElementHandle 的正文、属性、HTML、输入值使用原生 Playwright；等待、可见性、数量检查与截图亦为原生路径。点击、填写、键盘、鼠标和滚动仍使用已有 SDK 交互包装（SDK 未包装的输入方法保持原有行为）。此策略同时覆盖工作流信号、验证码读取和插件 broker，不更改选择器、iframe、等待及权限语义。缺失元素仍按调用方超时处理，失败不能静默变为“不存在”。

页面文本检测通过有界 DOM 读取执行，不走拟人输入/读取包装；隐藏子 iframe 不参与挑战文本判断，可见子 iframe 或主页面读取失败仍阻止放行。点击坐标按实际提交给 CFbox 的截图尺寸换算，不依赖远程 CDP 可能缺失的 viewport 信息。等待原因变化时记录组件、挑战文字/URL、读取状态等标志；总超时时保留当前阶段及最后一次检测摘要，不记录 token 值、Cookie 或完整页面正文。

`challenge.turnstile` 每轮同时检查可见页面标记、标题/正文挑战文字、挑战 URL，以及 CFbox `/predict` 截图识别。页面未命中不再跳过 CFbox；任一侧检测到挑战都会继续处理。隐藏组件、残留响应字段和隐藏 iframe 不单独视为挑战。

页面已就绪、组件与文本读取成功、页面检查无挑战且 CFbox 返回 `absent`、`verified` 或明确的 `unknown`，连续稳定两秒后成功。另允许上述 `verified` 加非空 token 的已解决组件保留可见。`unknown` 表示视觉判断不确定，此时根据页面有无可见 CF 组件、挑战文案及挑战 URL 判断，不单独阻塞正常页面；初始不存在、点击后消失和验证通过均遵循稳定窗口。CFbox 检测到未解决挑战、页面仍有未解决挑战或读取失败时不放行。未配置 CFbox 返回 `waiting_human`；截图/请求/响应失败或缺失、非法状态返回 `fail`，总等待超时返回 `unknown`，不能作为“未检测到”成功。截图、页面检查、网络请求和轮询均受步骤 `wait_seconds` 总时限约束。正常稳定清除约需三次识别，成功后不再请求。
# Cancellation and Owned Pages

Each admitted job owns its new root page and popup descendants through `JobPages`.
Context siblings and the lane home page are never adopted. A loaded same-origin
popup becomes the active page for subsequent built-in and plugin steps, failure
artifacts, and task focus. Cookie export remains restricted to the configured site
origin in the shared context, not arbitrary popup destinations. Cross-origin or
unloadable popups are closed rather than handed to the workflow. Browser actions
outside the broker's origin are denied. This is not a browser network firewall:
a click/navigation may already have sent a request before its new origin is known.

All owned pages close on job exit, including success, failure, timeout and cancel.
Popup listeners stay installed while roots close to capture queued late popups,
then are removed; lifecycle tasks are awaited. Closing a job does not close sibling
jobs or refocus the home page while another task is still mapped.

Cancellation interrupts the admitted coroutine instead of waiting for the next
step. A plugin step's exit invalidates its broker token before RPC thread settlement;
in-flight broker actions are canceled and awaited. ExecuteStep and RecognizeCaptcha
use cancelable gRPC futures; the caller signals cancellation and waits for the
polling thread to settle. Late results are not applied. Runtime step handlers may
cooperate with the request's `is_active` callable. Python cannot forcibly terminate
an arbitrary handler thread: plugin code must cooperate, and external work already
submitted is not rolled back by gRPC cancellation.

Revocation is checked before actions, during execution, and before accepting remote
results. Worker authorization polls every 0.5s with a 2s HTTP request timeout and
fails closed. Secret-provider futures also poll job/approval state while waiting;
late secrets are rejected after grant and epoch revalidation. Built-in blocking
provider calls cannot necessarily be interrupted, nor can remote provider side
effects be undone. Cancellation means local capability withdrawal and result
rejection, not a guarantee of no remote side effects. See
`plugins/examples/demo/FAULTS.md` for measured latency and reproducible coverage.
