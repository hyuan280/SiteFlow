# 任务执行

## 1. 范围

执行器第一阶段提供可运行的站点工作流队列：

- 创建运行记录：全部启用站点或指定站点。
- 每个站点生成一个 `site_job`，每个启用的工作流步骤生成 `job_steps`。
- Worker 按 `general`、`interaction`、`proxy_general`、`proxy_interaction` 优先级非抢占式分配 dispatch 预留槽；容量允许时同一 Run 可跨车道重叠执行。同车道共享任务并发上限：普通默认 5，代理普通默认 3（均可配置 1..10），交互固定 1。
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
  → 一批注册所有初始非空车道的 dispatch ticket
  → Worker 按优先级授予预留；获准车道才创建任务并竞争全局任务槽
  → 解析站点工作流 + 解密本地登录凭据（Bitwarden 延迟到登录步骤）
  → POST worker:/execute
  → Worker 校验 dispatch 预留并获取独立任务租约
  → 独立标签页逐步骤执行 / 条件与信号判断
  → 保留已完成业务结果；成功时尝试有界只读 Cookie 导出
  → 尝试清理自有页；确认后释放租约，否则后台重试
  → 返回步骤结果
  → API 写入 job_steps / 汇总 runs
```

清理与结果发布的具体顺序及未确认关闭时的租约保留见 [Cancellation and Owned Pages](#cancellation-and-owned-pages)；业务完成不等于浏览器资源已经释放。

系统调度器每 30 秒检查一次调度设置：

- 定时调度：cron 表达式触发一次运行。
- 间隔调度：上次触发后经过配置间隔再次触发。
- 随机调度：每个时间范围每天随机选择一个稳定时间触发一次。
- 每次触发会收集所有已启用、已过 `checkin_after` 且当前没有运行中任务的站点。
- 同一时间只允许一个调度运行在执行；站点配置只保留 `checkin_after` 单次时间。
- cron 和随机范围按 `SITEFLOW_SCHEDULER_TIMEZONE` 计算，默认 `Asia/Shanghai`；不依赖容器系统时区。

### Scheduled Browser Admission

Automatic scheduling performs two-phase admission before creating any `Run`,
`SiteJob` or job steps. The first synchronous scan resolves eligible sites with
`WorkflowService.resolve_site()` and collects their saved execution lanes (default
`general`). The production loop awaits `LaneService.scheduler_preflight()` and
then repeats the scan with a one-use, at-most-10-second preflight proof. The second
scan rechecks schedule settings, active runs, eligible sites and resolved lanes;
new required lanes or a changed first required lane defer the entire batch.
Database work and `ExecutionManager.submit()` stay on the event-loop thread;
worker I/O is asynchronous.
Direct synchronous `scan_once()` has no proof and fails closed by default.

The production gate is authenticated `POST worker:/browser/preflight`, using the
browser Bearer token, no Origin, and only `{"lanes":[...required lane keys...]}`.
Required lanes are ordered `general`, `interaction`, `proxy_general`,
`proxy_interaction`. Preflight starts or reuses **only the first required profile**,
verifies its CDP connection and local `about:blank` home page, and checks the
remaining required lanes for queue eligibility. It does not launch every lane,
visit a business/site URL, execute workflow steps, or create a Run. Multiple
required lanes do not fail admission merely because their count exceeds the
profile cap: runtime dispatch queues them within the available capacity. No `Run`,
`SiteJob` or job steps are created until the first-profile check succeeds.

Worker preflight has a 120-second outer deadline; the API request allows 130
seconds and the scheduler wrapper 135 seconds. Capacity protection is fail-fast
for this gate, not a 120-second seat queue wait. No redirects or environment proxy
trust are allowed; the uncompressed response is limited to 64 KiB. Admission
requires backend `cloak-manager`, literal `ready=true`, the expected `first_lane`,
and known, unique lane keys with literal boolean flags covering every required
lane. Provider reasons, extra fields and seat-count claims are not evidence and
are not logged. Remote HTTP 402/capacity rejection, failed startup/CDP/home-page
checks, missing/false flags, unsupported backends, unknown/pending license state,
missing authentication, malformed responses or request failures all defer with
no Run and no business actions. Preflight does not write license settings.

Existing authenticated `GET worker:/browser/readiness` remains **read-only metadata
only**, based on backend status and all four lane flags. It does not create or
launch profiles, acquire leases, or call `/api/settings`. A healthy response or
eligible stopped lane is not launch proof; this GET is not the production
scheduler admission gate. Recognized license tiers are `keyless`, `free` and `pro`;
`none`/unknown tiers and pending or uncertain settings fail closed.

Cron and interval deferrals preserve the original overdue `next_run_at`; recovery
creates one current run, not a backlog. Random scheduling retries within the
current due window with `next_run_at` set to the scan time, then recalculates the
next window when that window expires. Each scheduler instance logs one warning
when browser admission becomes deferred, using the static reason
`browser_or_required_lane_unavailable` and only allowlisted required lane keys.
Repeated deferrals and random-window expiry do not repeat the warning. One recovery
message follows a successful first-profile preflight; it is not proof that all
future lanes can launch. Provider and exception prose are not included in these logs.
This admission gate applies only to automatic scheduled execution;
manual runs and assistant execution use runtime lane admission. Preflight warms
the first browser **without a lease or reservation**. Another run, assistant or
viewer can contend before execution acquires it; success is point-in-time evidence,
not a blanket guarantee of future capacity, connectivity or business success.

### Lane Dispatch and Profile Queue

The backend registers all initially nonempty lanes of a Run in one batch before
creating execution tasks. Worker grants dispatch reservations from the shared
finite profile pool with non-preemptive lane priority
`general` -> `interaction` -> `proxy_general` -> `proxy_interaction`: priority
orders waiting work, not eviction of active work. Only a granted ticket permits
creation of that lane batch's execution tasks, which then compete for global job
slots. Pending reservations do not occupy global job slots. With an effective cap
of 2, two different lanes of the same Run can overlap; free tier's cap of 1 keeps
different lanes sequential. Other Runs, assistants and viewers share this pool.

Each lane's task concurrency is shared across Runs and assistants: general defaults
to 5 and proxy general to 3, both configurable from 1 through 10; interaction lanes
are fixed at 1. Tabs in one lane share a profile and the short foreground-input
gate, not a whole-job lock. Profile capacity, lane concurrency and global job slots
are separate limits.

A granted reservation holds a pin lease until its whole lane batch joins, including
jobs waiting for a global slot. Cross-lane fallback queues the job for the target
lane and releases its source global job slot; it does not wait there for target
capacity. After the source batch joins, a queued fallback joins a later target
batch, reopening the target lane with a fresh ticket if its previous batch already
finished. Existing explicitly allowed workflow retry/fallback policies remain in
force; dispatch does not expand business retry budgets or authorize automatic
replay of executed steps.

Pending and granted tickets have a **60-second TTL**, renewed by the backend on a
**1-second** cadence. Tickets are bound to an owner, Run and lane, and execution
also checks the Worker generation and pinned session. Generation changes or lost
reservations fail closed without automatic replay. Cancellation/finalization
cleans up only that owner's reservations; delayed renewals cannot revive released
tickets. Released identities remain fenced for the registry generation even after
their detailed tombstones are compacted; bounded identity/claim storage fails
closed on exhaustion. Each dispatched Run/job/attempt identity can be claimed
only once, before artifact initialization or business work. Duplicate active
requests cannot replace the original execution's cancellation handle or log.
Expiry releases only dispatch pins, never ordinary job/cleanup leases,
quarantined ownership or viewer protection. Public `in_use` includes dispatch pin
leases as well as job/cleanup leases; it is not an exact running-task count.
Dispatch shutdown uses a bounded drain and retains unfinished cleanup ownership
so cancellation-resistant operations do not block all subsequent teardown.

The worker-wide `SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES` accepts only integers 1..4
and defaults to 1, never unlimited. Free licenses always cap it at 1 even if set
higher. Pro/unknown capacity has no reliable Manager seat metadata, so the default
is conservatively 1; keyless also defaults to 1. A finite operator override is a
local resource budget, not proof of licensed entitlement or a bypass for unknown
license tiers. Both Compose files explicitly pass this setting to Worker.

An ordinary runtime lane-lease acquisition waits for capacity for at most
**120 seconds**. A pending dispatch reservation can retry capacity acquisition
while its ticket is renewed; 120 seconds is not a total dispatch queue deadline.
Under pressure, the worker can immediately stop an idle SiteFlow-owned profile, even with
`SITEFLOW_LANE_IDLE_STOP_SECONDS=0`, then reuse the freed slot for the next lane.
It stops, never deletes: profile IDs and cookies persist for later reuse. It never
evicts active leases, viewers, task pages or quarantined ownership; unconfirmed
stop/launch cleanup continues to occupy capacity. Disabling timed idle shutdown
does not pin a profile under pressure, but an open viewer does. If no safe slot
becomes available, an ordinary bounded acquisition fails rather than evicting
active work; a healthy pending dispatch ticket remains queued.

UI manual lane start is fail-fast under capacity protection, not a queued task
lease. Assistant full-run dispatch first closes only its own discovery browser
session and releases its lease; it must not close other conversations' pages,
viewers or jobs to make capacity. Later runtime contention remains possible.

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
challenge.grid_captcha
action.navigate / action.wait / action.click / action.fill
flow.result
sign.button / sign.iframe.image_captcha / sign.image_quiz / sign.unit3d_claim
post.buy_card
post.resign
post.resign_loop
post.resign_dialog
```

- `flow.open`、`action.navigate`：导航完成后检测页面标题和正文中的错误页特征，例如 Bad Gateway、Service Unavailable、502/503/522、Internal Server Error、404、网站维护/正在维护/关闭维护、The website has been stopped 等，即使 HTTP 返回 200 也会检测。`flow.open` 的网络异常和错误页共用 `retry_count` 次额外重试（总尝试次数为 `1 + retry_count`）；错误页重试临时禁用当前任务标签页的缓存，强制重新访问原目标地址，不因 URL 相同而跳过，步骤结束后恢复缓存。不会清除 Cookie 或整个 profile 的缓存。`retry_count=0` 不额外重试。次数耗尽仍是错误页时返回 `fail` 和 `error_page` 数据，随后按该步骤的错误策略 `stop`/`continue`/`defer`/`escalate_interaction` 处理。
- `action.navigate.retry_count` 默认 0，范围 0 到 20；仅对确认可重复访问的页面显式开启，例如设置为 2 表示最多尝试 3 次，间隔 1 秒。只重试 Chromium 的 `ERR_CONNECTION_CLOSED`、`ERR_CONNECTION_RESET`、`ERR_CONNECTION_ABORTED`、`ERR_TIMED_OUT` 和 `ERR_CONNECTION_TIMED_OUT`；不重试错误页、证书/代理错误、`ERR_ABORTED`、页面关闭、普通加载超时或浏览器断连。不重放登录、点击、购买或补签操作；URL 本身带副作用时应保持 0。默认步骤超时按重试预算扩展，显式配置的步骤超时仍优先。
- `challenge.2fa`：通过 2FAuth 按 `service_hint` 匹配账号并获取 OTP；多个候选或没有候选时返回 `waiting_human`。
- `challenge.turnstile`：先检查 `.cf-turnstile`、`cf-turnstile-response`、`cf-chl-widget`、CF iframe、挑战文案；存在挑战时截图调用 CFbox，按复选框中心点击。CFbox 返回 `absent` 但 DOM 仍有挑战时继续等待，不直接判成功；完全没有 CF 标记约 2 秒后才视为无挑战。`wait_seconds` 是从步骤开始计算的总等待上限，包含 DOM 检测、CFbox 调用和点击后的等待，不是固定延时；未配置 CFbox 返回 `waiting_human`。
- CFbox 的视觉状态 `error` 是合法识别结果，不代表 HTTP 服务异常；仍逐轮重新截图识别，相同状态日志去重，每约 10 秒输出等待计数和阻塞原因。视觉 `error` 与页面检查冲突时，可以在本步骤观察到页面挑战、点击时读取到空 token、随后读取到新 token，且无可见组件、挑战文字或挑战 URL、页面可读且 ready 稳定至少 2 秒后通过替代确认，结果保留 `cfbox_state=error` 和 `completion_reason=fresh_token_and_clear_page`。旧 token、不可读 token 或重建挑战不得复用此前证据。
- 连续低置信度复核：`state=error/unknown` 且有效数值 `confidence < 0.95` 时，同一状态、同一 URL 连续至少 3 次重新截图识别，并在这段时间内持续无可见 CF 组件、挑战文字及挑战 URL，页面可读且 ready 至少 2 秒，则按误检测处理，无需 token 或点击，返回 `completion_reason=low_confidence_and_clear_page`，保留实际 `cfbox_state`、`confidence` 和 `low_confidence_count`。状态、URL、页面检查或低置信度条件中断后重新累计。等于 0.95、缺失、非数值、非有限值或越界值不计入此路径。原有新 token 确认及普通 `unknown` 双重确认保持独立，不因低置信度复核重置其稳定时间。
- 没有足够证据仍返回 `unknown`；HTTP、认证、连接或响应格式失败不会被降级为通过。上述判断只确认挑战状态，不证明后续业务提交成功。
- `challenge.altcha`：定位 ALTCHA 组件并点击复选框，等待组件进入 `verified` 状态或写入 Token；`wait_seconds` 控制最长等待时间。
- `challenge.slider`：拖动手柄到容器右侧；配置了 `success_selector` 时等待成功元素。
- `challenge.grid_captcha`：独立的九宫格多选验证码功能块（`challenge` 类别，允许多例），始终要求视觉模型，不属于字符验证码的 `captcha_method`。截图必须同时包含目标参考图与完整 3×3 网格，单元从左到右、从上到下编号 1–9，选择所有匹配项；不清楚时刷新，不猜测。未配置视觉模型或未开启「支持视觉」时需要人工处理。全局提示词在「功能管理」配置，不进入步骤字段。

九宫格统一使用 `max_refresh` 控制额外重试次数，默认 3（首次加重试最多 4 轮），范围 0–10；0 表示不额外重试。识别不清刷新、答错后自动换题、识别期间题目变化，共用同一预算，不按原因或每轮重置。例如识别不清刷新一次、答错换题一次，合计已消耗 2 次重试。每次重新截图和识别。开始识别前等待题目稳定；模型返回后若确认题目已更新且新题无预选，也可丢弃旧答案并重试。提交后只有确认出现不同的新题才会重新作答，提交超时、异常或仅有状态文字变化不会盲目重复提交。次数耗尽转人工，日志记录验证轮次、重试次数和累计模型调用次数。
- `sign.image_quiz`：读取签到页面上的答题图片、可选题目文字和选项，调用视觉模型选择答案，填写留言并点击对应选项提交；模型无法确定答案时按 `refresh_selector` 刷新换题，超过 `max_refresh` 或未配置视觉模型时返回 `waiting_human`。答题图片会保存为截图 artifact。提示词在「功能管理」的执行行为中全局配置，不进入步骤配置。
- `sign.unit3d_claim`：点击 Claim 按钮后等待成功条件，支持成功文本、出现元素或消失元素三种判断，任一满足即成功；都未配置时点击后直接成功。
- `sign.iframe.image_captcha`：在签到 iframe 内填写静态字段，识别图像验证码并填入，然后点击 iframe 提交按钮；验证码校验、刷新和任务重启逻辑与 `challenge.image_captcha` 相同。
- `challenge.image_captcha`：截取验证码图片，按步骤的 `captcha_method` 选择识别方式后填写；默认 `ddddocr`，也可选择 `vision`（需配置视觉大模型并开启支持视觉）或 `nexusphp_gd5`（支持 NexusPHP GD 5 号字体、6 位字符，标准尺寸 150×40，其他显示尺寸会自动缩放）。配置了 `submit_selector` 时自动提交，否则只填表，交由后续登录/签到步骤提交。`charset` 校验字符集（纯数字、纯小写/大写/大小写字母、数字+字母或不限制），`length` 校验字符数（0 不校验）。`min_confidence` 仅用于 ddddocr。任一校验不通过时先按 `refresh_selector` 刷新验证码（未配置则点击验证码图片），图片未变化或刷新后仍不通过时重新开始任务，最多重新开始 2 次；视觉模型和 NexusPHP GD5 不会静默回退到其他方式。
- `post.buy_card`：通过商品范围内的页面元素购买，不再构造 POST。`purchase_quantity=0` 按待补签日期减去已有卡数补足，正数固定购买 N 张（范围 1 到 365），不要求待补签日期非空。支持 table/grid/custom 布局、商品/标题/价格候选选择器及魔力值读取；`purchase_clicks` 按列表顺序执行，每项使用 product/page 范围内首个可用候选，点击发出后不切换候选重试。`quantity_selectors` 为空时逐张购买；配置后在唯一商品内填写批量数量，取目标剩余、余额可负担数与输入框 max 的最小值，并按 min/step 向下取合法正整数。使用真实 fill/Tab 处理常见失焦行为，实际点击前重读数量、单张报价与余额，变化或失效时停止。每轮必须核验卡数恰好增加本轮 N 张；独立库存页在提交结束后有界重新 GET 核验。原生弹窗拒绝、数量异常或结果不明返回 `waiting_human`，即使 `on_error=continue` 也停止。已发起购买后禁止任务自动重启、转车道重跑及回跳重购。旧 option/action/submit 请求字段保留但忽略。
- `post.resign`：读取待补签日期，检查 NexusPHP FullCalendar 日历，翻月定位漏签日期，通过 `ajax.php` 的 `attendanceRetroactive` 执行补签；不负责买卡。
- `post.resign_loop`（页面循环补签）：仅处理 API 下发的过去待补签日期，不修改旧 `post.resign`。确认月份并唯一定位当月目标日期，选中后确认 selected，再点击文字明确包含“补签/補簽”的独立按钮，随后立即交给下游步骤；同一 job 内通过 `flow.jump` 回到本步骤，先复核上次日期 done，再推进下一日期。日期定位、选中确认及 done 标记必须绑定 `{date}` 或 `{day}`，支持 `{year}/{month}`；`{day}` 仅在目标月已确认且排除相邻月时使用。不读取或核验卡数，只以目标日期 done 确认成功，全部日期 done 才将 `sys_resign_pending` 置为 false。`wait_seconds` 默认 10 秒（1-60），用于页面、日历状态及按钮就绪，不是点击后的成功等待；`max_retries` 默认 3（0-10），每日期首次提交后最多额外重试 3 次。须先确认站点重复补签同日不会重复扣卡；只支持同 job 循环，不支持跨进程自动续跑。页面循环补签不安装跨源网络拦截；旧页面补签步骤需手动重配，无旧键别名、模式开关或自动迁移。完整 11 字段及回跳示例见[使用指南](usage.md#页面循环补签)。
- `post.resign_dialog`（弹窗按钮补签）：在当前页面点击入口并等待唯一弹窗，按配置的正则提取按钮日期，只点击 pending 对应的唯一过去日期。日期点击前完成持久化预留；支持成功提示短暂出现后刷新，必须同时核验目标 done 或本次新提示及卡数恰好减少 1。可配置按钮缺失时记为已签到，但须有日期列表就绪证据，推断结果不能覆盖已有 unknown；历史 unknown 仍只接受日期绑定的 done。每次提交最多点击一次，刷新后可重新打开弹窗处理下一日期，不自动重放消费。字段及配置示例见[使用指南](usage.md#弹窗按钮补签)。
- 页面补签每次提交前调用内部持久化检查点接口，以任务级 token 原子记录 `(site_id, date)`，事务提交并收到 ACK 后才点击，页面确认后再次持久化确认。页面循环补签仅在同 job 回跳中按上述预算重试，不能接管其他活动任务；弹窗按钮补签已有未决或终态日期不能重复预留，排队旧任务也不能绕过。原生弹窗默认拒绝，跨源操作/新窗口/日期歧义/结果不明时转人工，不被 `on_error=continue` 放行；这与页面循环不拦截跨源网络资源不同。取消或断连保留已确认部分结果，不自动续跑或重放；弹窗按钮补签后续任务对未决日期先只读检查 done，没有明确完成标记则停止。ACK 丢失时可能保留“未点击但未决”的保守记录，需要人工核验；这不是远端事务或 exactly-once 保证。
- 待补签日期由 API 按站点「支持补签最大天数」生成并注入 worker：从该站点最早有运行记录的日期开始，窗口内没有成功运行或成功补签记录的日期都算缺失；今天等当日结束后再判定。补签结果写入 `site_resign_records`，同时合并到状态矩阵和趋势。
- 站点 Cookie 由系统主动维护：任务开始前注入，站点任务成功后尝试从浏览器导出当前站点 Cookie，取得有效结果后加密更新；任务失败或导出超时/失败时保留原配置，导出失败不改变已完成业务结果。
- `post.resign` 的历史日期范围继续使用站点最大补签天数；购卡自动数量由该范围内的待补签日期决定，固定数量由 `purchase_quantity` 决定。
- 魔力值不足时 `post.buy_card` 只购买可负担部分并继续，`post.resign` 按现有卡数补签，剩余日期保留到下次。
- 模板和站点解析信号始终包含系统信号「断签」(`sys_resign_pending`)；worker 用待补签日期数量计算真假，站点不支持补签时恒为假。条件日志会输出待补签天数。
- `post.buy_card` 未配置步骤超时时会根据购买数量、点击项、库存验证和间隔计算预算；显式步骤超时仍优先。新配置沿用 `workflow_steps.config_json`，不创建新表、不搬迁旧数据。

#### 购卡与补签的执行边界

- 购卡优先从单独配置的同源库存页读取卡数，未配置则使用商店页。库存正则对可见文本取第一捕获组；仍有加载指示时不采信初始读数。验证期间可重新核对同源库存页，旧值、暂时缺失或数量不一致不触发再次付款。日志记录轮次、期望数量、检查次数、耗时和最后读数；URL 保留真实查询参数用于导航，库存地址日志不记录查询参数。
- 页面循环补签的日期确认和回跳仅作用于同一任务，不支持跨进程续跑。旧步骤需手动重新配置，无旧键别名或自动迁移。选择器与条件示例见[页面循环补签](usage.md#页面循环补签)。
- 弹窗补签的日期正则可以匹配命名组 `date`（完整 ISO 日期），或 `month` 和 `day`，并可带 `year`。只有文本没有明确年份时才能从唯一待补签月日推导年份；歧义或与页面的明确日期矛盾时停止。只有 `done_selector` 支持日期模板，其选择器必须绑定 `{date}` 或 `{day}`。成功提示基线在实际点击前采集，旧提示不能证明本次成功；即使新提示短暂出现，仍须核对库存恰好减少一张。按钮缺失判已签需观察到可解析日期按钮或明确的弹窗就绪证据，不能用来核销未知结果。

`action.*` 和签到步骤支持候选选择器按顺序尝试。`sign.*.success_signals` 按页面文本匹配；`sign.unit3d_claim` 还支持 `success_selector`（成功后出现）和 `success_when_missing`（成功后消失）。

### 九宫格工作流组合

将打开页面、触发签到、处理验证码与输出结果拆为独立步骤，不在验证码功能中写入站点地址或凭据。例如以下站点配置片段使用占位站点和示例签到按钮选择器，实际使用时替换为页面对应选择器：

```json
{
  "name": "Grid check-in example",
  "base_url": "https://site.example",
  "steps": [
    {"step_key": "open", "feature_type": "flow.open"},
    {
      "step_key": "signup",
      "feature_type": "action.click",
      "config": {"selector": ["#signup"]}
    },
    {
      "step_key": "grid",
      "feature_type": "challenge.grid_captcha",
      "on_error": "stop",
      "config": {
        "image_selector": [".geetest_panel_box", ".geetest_widget"],
        "cell_selector": [".geetest_item"],
        "success_signals": ["验证成功", "驗證成功", "今天已经签到过啦", "今天已經簽到過啦"]
      }
    },
    {
      "step_key": "result",
      "feature_type": "flow.result",
      "config": {"output_value": true, "message": "Check-in confirmed"}
    }
  ]
}
```

`image_selector` 是有序候选区域，首个命中区域必须包含目标和全部九格，不能只截取网格。`cell_selector` 同样按候选顺序定位单元集合。`selected_selector` 默认 `.geetest_selected, .selected, [aria-checked="true"], [aria-selected="true"]`，含义是对每个单元自身做 CSS `matches`，不是在其内部查找子元素。刷新和提交按钮候选默认分别为 `[".geetest_refresh"]` 和 `[".geetest_commit"]`。

提交后需要明确的成功条件：`success_signals`（任一文本命中）、`success_selector`（任一元素出现）或显式配置的 `success_when_missing`（列表中全部元素消失），三类条件任一满足才成功。默认使用示例中的四个成功文本，两个选择器列表均为空；验证码面板消失本身不代表已解决。不要仅把从未出现的备用选择器放入 `success_when_missing`，以免其缺失被当成成功。按站点实际成功状态调整条件。`success_wait_seconds` 默认 10 秒，范围 1–120；所有成功条件都清空仍允许保存，但运行时应返回 `unknown`，不能无条件成功。后续无条件 `flow.result` 应保留在验证码成功之后，不要用 `on_error=continue` 绕过确认。

`flow.jump` 检查页面文字（不区分大小写）或选择器；命中后可按配置跳转到任意步骤 key，或重新开始任务。未命中时继续执行后续步骤；`max_jumps` 防止条件持续命中造成死循环，超过上限任务失败。

`flow.result` 不操作页面，按步骤条件输出布尔值 `output_value` 和信息 `message`。条件满足并执行后，会把信息写入站点任务消息、把输出值写入任务输出，并立即结束当前站点任务，不再执行后续步骤；`output_value=false` 时站点任务状态记为 `fail`。条件不满足时跳过并继续。功能块支持多例，可配置多个带不同条件的结果出口，适合“已签到/未签到”分流。

可选模型判断要求「功能设置」中的总开关与步骤卡片的「开启模型判断」同时开启，并遵循全局判断范围。步骤开关默认关闭，旧步骤缺少该字段时保持关闭。站点/模板可通过 `llm_enabled` 开启、`llm_prompt` 提供业务判断标准，留空使用全局提示词；支持模板继承与站点覆盖。全局关闭时步骤模型设置保留但不生效。视觉模式仅发送页面截图，文本模式仅发送页面文本；模型明确结论替代原输出，请求失败或结论无效则步骤失败，不静默回退。

结果判断仅通过 system 提示约束模型直接返回正文 JSON，不声明工具或设置 tool_choice，也不强制 response_format。例如 `{"result":true,"reason":"简短可见依据"}`。`result` 必须为 boolean，`reason` 必须为 1 到 500 字符的非空简短依据；拒绝额外字段、重复 JSON 键和非法 JSON。返回非空工具调用时视为无效响应，不读取工具参数；不猜测自然语言布尔结论，不从 Markdown 中提取结果，不读取 reasoning_content，允许剥离正文前导且完整闭合的 think 块。九宫格识别的工具协议不受影响。

系统将不可编辑的输出协议放入 system 消息，用户业务标准使用独立消息，不把合并后的内容写回配置。功能和步骤卡片显示只读协议说明；旧自定义提示词不自动改写，但其中的输出格式不能替代固定协议。有效 false 仍表示判断步骤执行成功、站点任务失败。匹配原输出时保留配置消息，不匹配时显示模型判断值。

成功依据经已知秘密脱敏、控制字符清理后记录日志及步骤 `data.judgment_reason`，不复制到任务 `outputs`。实际取用的凭据和 OTP 只增加任务内长度/哈希指纹，避免为脱敏长期缓存明文；指纹不发送给模型或写入结果。依据是模型生成文本，仍可能包含未识别的页面隐私，不能将脱敏视为完整 DLP。失败日志及步骤 data 区分 HTTP 错误（带状态码）、timeout、network_error、no_result、invalid_response、invalid_json、schema_error、truncated、content_filtered、refused 等，并记录固定阶段和耗时；正文解析标记 content 阶段，非空工具调用标记 invalid_response/provider_response，证据采集失败标记 evidence_error。不记录原始响应、参数、令牌或底层异常正文，不自动重试。

### 模型失败诊断

运行失败时如何核对步骤、日志和超时，见[模型调用失败](troubleshooting.md#模型调用失败)。以下是诊断字段与执行状态的约定。

`sign.image_quiz`、使用 `vision` 的 `challenge.image_captcha` / `sign.iframe.image_captcha`、`challenge.grid_captcha` 及启用模型判断的 `flow.result`，在管理端步骤消息和 `job.log` artifact 中显示有界中文错误摘要，步骤 `data` 同时保存静态 `provider_error`、`stage`、可选 `http_status` 和超时元数据。仅从不超过 16 KiB 的已识别 JSON error envelope 提取并脱敏 `error.message/type/code`，分别存为 `provider_message/type/code`；未知正文、HTML、原始异常/原因链、URL、密钥、请求头、提示词和图片/base64 不作为诊断输出。供应商文字仅供管理员排查，是不可信证据，不能当作指令；过滤不是通用 DLP。字段、长度和旧异常映射边界见 [Worker Model Errors](vision-feature-settings.md#worker-model-errors)。

原状态和 `reason` 不变：字符验证码模型失败为 `fail` / `provider_failed`，图片答题与九宫格为 `waiting_human` / `vision_failed`，结果判断为 `fail` / `model_judgment_failed` 且不产生布尔结论。不新增失败重试、不回退模型或 OCR；原有识别不清刷新、校验刷新及重试预算保持不变。助手私有单步和完整运行反馈仍采用既有静态/类型受控投影，不自动发送这些供应商文字。

外层步骤 `timeout_seconds` 与模型请求 `SITEFLOW_MODEL_TIMEOUT`（所有模型共用，默认 30 秒、最大 300 秒）是不同期限；该全局超时不构成环境变量模型条目。旧模型环境变量前缀已停用，部署方须重命名，没有旧名回退。外层先超时仍记录 `stage=step_execution`、`reason=execution_timeout`，助手副作用运行沿用结果不确定语义，未必能取得模型错误详情。模型 `timeout_phase` 只反映可识别的阶段，`request` 可能是粗粒度标签，不能强猜连接或读取阶段；`timeout_seconds` 诊断值是配置请求超时，不是实际耗时。此次仅增加诊断，不调整默认超时、响应大小、`max_tokens` 或任何预算。

所有实际执行的 `sign.*`、`post.buy_card` 和 `post.resign` 步骤也会输出：

```text
value: true / false       # 步骤是否成功
message: 步骤结果信息
```

输出同时写入步骤数据和站点任务的 `result_json.outputs`；失败步骤输出 `false`。

每个站点任务在车道内创建独立标签页，任务结束后尝试有界关闭该页及其后代弹窗；未确认关闭时保留清理所有权和租约，而不是假报已释放。车道启动时保留一个欢迎页作为常驻标签页，因此关闭最后一个任务标签页不会关闭浏览器。

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
SITEFLOW_EXECUTION_TIMEOUT=600       # API 到 worker 的 HTTP 超时，流式读取时限制无数据等待
SITEFLOW_EXECUTION_MAX_RUNS=2       # 同时执行的运行数
SITEFLOW_EXECUTION_MAX_JOBS=10      # 同时执行的站点任务数
SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES=1  # Worker profile 上限，整数 1..4；free 始终为 1
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

已经提交执行的任务在 worker 不可达时标记为 `error`；不会自动重试。自动调度在创建运行前通过上述首个 profile 启动/CDP/本地空白主页预检，不可用时先延后，不创建失败记录。预检不预留容量，不保证后续车道或业务步骤成功；业务步骤执行后不会因容量问题自动重放。

`SITEFLOW_EXECUTION_TIMEOUT` 的现有默认值仍为 600 秒，本次不改变其读取超时行为；它不是任务总时长，也不是每个步骤的总时限。API 使用 HTTPX 流式读取 worker，持续收到数据可使运行超过 600 秒；长时间没有数据仍可能触发读取超时。步骤有自己的 `timeout_seconds`，浏览器设置与焦点转换另有下述 10 秒预算，不能靠增大 600 秒来安全解除生命周期阻塞。

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

页面已就绪、组件与文本读取成功、页面检查无挑战且 CFbox 返回明确的 `absent` 或 `verified` 时，连续稳定两秒后成功。另允许上述 `verified` 加非空 token 的已解决组件保留可见，但可见 frame 的 marker/token 探测必须完整可读。普通 `unknown` 仅在本步骤从未出现未解决挑战证据时保留页面确认兼容；出现 DOM 挑战、视觉 error/unchecked/processing 或多候选未解决证据后，后续 `unknown/null` 不能自行清除阻塞。符合条件的新 token 或连续低置信度复核仍可独立确认。

CFbox 的非空 `challenges` 数组始终参与聚合，包括顶层 confidence 为 null 的情况。子状态按 unchecked、processing、error、unknown 的未解决优先级处理；只有一个明确 unchecked 时，其他无坐标 unknown/error/processing 不构成第二点击目标，可以使用该唯一坐标，但不能据此判完成。任何其他不同坐标或多个 unchecked 都拒绝选点，不选第一个。多项未解决子结果、歧义 unknown 阻止普通完成和新 token 豁免；若所有未解决子项均为无坐标 error/unknown 且各自置信度有效、严格小于 0.95，则可独立累计三轮及至少两秒的低置信度页面复核。该路径仍要求页面无可见组件、无挑战文字/URL且可读、就绪，不能只凭复选框坐标为空通过。畸形子结果失败关闭。结果保留 `cfbox_summary_state`、`challenge_states`、有效状态及置信度，诊断额外提供子置信度和复核资格。

页面标记扫描支持 open Shadow DOM、非 div 的 CF widget 容器、与可信 Turnstile 脚本关联的 `.ts-box`，并检查可见子 iframe 的内容与实际 URL。隐藏祖先和 slot 隐藏残留不单独作为挑战；扫描超限或可见 frame 检查失败不当作不存在。closed Shadow DOM 内部无法由普通页面脚本扫描，实际可访问的 frame 仍参与检测，不能把未知读取结果当作无挑战。

CFbox 检测到未解决挑战、页面仍有未解决挑战或读取失败时不放行。未配置 CFbox 返回 `waiting_human`；截图/请求/响应失败或缺失、非法状态返回 `fail`，总等待超时返回 `unknown`，不能作为“未检测到”成功。截图、页面检查、网络请求和轮询均受步骤 `wait_seconds` 总时限约束。正常稳定清除约需三次识别，成功后不再请求。
# Cancellation and Owned Pages

## Concurrency and Foreground Input

`general` and `proxy_general` admit multiple jobs, subject to API/global limits,
the lane execution limiter and available browser/CPU/network resources. They do
not serialize an entire job behind the previous job. Each job runs its own steps
in order in a separate tab, but tabs share a browser context and foreground input.

Humanized public input actions hold a reentrant per-context gate for the complete
action and foreground their target. New-page creation and explicit focus changes
also use that gate so they do not background another tab mid-action. These are
intended to be short serialized transitions, not a whole-job lock; an input action
can nevertheless take time. A later job can wait for current input or the lane
limiter even when several tabs exist. Navigation, DOM observations, waits and
screenshots do not acquire this input gate. Parallel admission does not promise
perfectly simultaneous foreground interaction or protection from manual VNC input.

`Page.close()` no longer acquires the shared input gate. A suspended tab close
therefore does not itself hold up another job's creation, focus or input. This
does not eliminate other resource contention or the fail-closed quarantine below.

## Setup and Pending Native Calls

After lane acquisition, `JobPages.setup()` allows **10 seconds per setup stage**,
not 10 seconds for the whole job or browser startup. The stages are `cookies`
(when seed cookies exist), `new_page` and `focus`. Before each operation the worker
writes `browser_setup_<stage>` to `job.log` and emits running progress with `stage`
and artifact metadata, allowing diagnostics before any workflow step has started.
Timeout returns static `reason`/`message=browser_setup_timeout`; other setup errors
use `browser_setup_failed`, with the stage retained. Cookie setup failure is
`fail`; page creation/focus failure is `error`. These diagnostics do not expose
cookie values, browser URLs or native exception text.

Separately, the input guard has a **10-second combined budget** for acquiring the
gate and running a native creation/focus transition, including focus before an
input action. This is not a 10-second limit on the complete click/fill action.
Timing out before dispatch does not cancel the current input owner. If a dispatched
native lifecycle call is still pending after timeout or caller cancellation, it
may still change focus remotely: the lock is freed, but the context is quarantined.
New conflicting input, creation and focus fail closed with the static
`Browser lifecycle pending` error until that native call settles. They do not
silently unlock and continue, nor wait for the legacy 600-second HTTP timeout.
Reads and close cleanup remain outside the gate; quarantine may still prevent
future jobs in that context from performing input.

Dispatched `new_page()` work is not canceled on a job setup timeout/cancellation.
Its tracked creator is retained until the returned page is attached to its
`JobPages` owner, even if the job has already exited; cleanup then closes that late
page. A 10-second caller budget does not prove that remote creation was aborted.
Non-creation setup wrappers can be canceled, but a pending native focus call still
keeps the input quarantine until settlement.

Native guard child tasks are registered with the exact `JobPages` owner that
dispatched them, including children outliving a timed-out setup wrapper. Shutdown
tracks those children as well as their callers, not a context-wide sweep that
could cancel another job's lifecycle work.

## Owned Cleanup

Each admitted job owns its new root page and popup descendants through `JobPages`.
Context siblings and the lane home page are never adopted. A loaded same-origin
popup becomes the active page for subsequent built-in and plugin steps, failure
artifacts, and task focus. Cookie export remains restricted to the configured site
origin in the shared context, not arbitrary popup destinations. Cross-origin or
unloadable popups are closed rather than handed to the workflow. Browser actions
outside the broker's origin are denied. This is not a browser network firewall:
a click/navigation may already have sent a request before its new origin is known.

On success, failure, timeout or cancellation, cleanup attempts only owned pages.
For each page it allows **3 seconds for native `page.close()`**, then, if closure
is unconfirmed, **3 seconds for a CDP fallback**. The fallback attaches to that exact
owned page, reads `Target.getTargetInfo`, requires a nonempty page target identity,
and sends `Target.closeTarget` only for that target. Confirmation is a successful
native close, `is_closed() is True`, or an explicit CDP close success, not a truthy
placeholder. Cleanup never enumerates and closes other context pages, refocuses
the home page, or calls `browser.close()` to resolve a single job's cleanup.

If any page/creator remains unresolved, its owner, popup listeners, any existing task mapping
and lane lease remain tracked. A background reaper logs
`browser_cleanup_quarantined` and retries after **5 seconds** between rounds;
confirmed cleanup releases the lease and logs `browser_cleanup_released`.
Lease release has its own **5-second wait budget**, reusing an outstanding release
task rather than duplicating it. Listeners/mappings are removed only after owned
closure and creation settle. The execution slot is freed when the admitted job
returns, so an unresolved cleanup lease does not consume all future job capacity.
`in_use` counts leases, including dispatch pins and quarantined cleanup, not just
executing jobs: it must not be forced to zero while ownership is unresolved. Normal stop and idle
stop remain blocked by those leases. Input quarantine is a separate constraint
and can still block new input despite available execution slots.

There is no universal automatic all-clear. In particular, remote creation can
have produced a target without returning its page/target identity. If it never
returns, or shutdown cancellation leaves that creation unconfirmed, the worker
cannot safely identify a target to close or prove it absent. It retains the lease
and ownership uncertainty; the reaper cannot discover that identity by closing
arbitrary tabs. An operator must investigate/reconcile the owned backend/profile
in a controlled maintenance window without disrupting other jobs. A timeout,
disconnected client, stopped local task or elapsed retry interval is not proof
that remote resources have disappeared.

## Business Results and Shutdown

The executor hands off a snapshot of the completed business result, including
steps, outputs and artifacts, **before optional Cookie export**, not after it.
For a successful job, export is a read-only context-cookie query through
`JobPages.setup()` with a **10-second caller budget**; it neither creates a page
nor acquires foreground input. Timeout logs the static `cookie_export_timeout`,
and other export failures log `cookie_export_failed`, without changing the
completed business status/message or exposing native exception text. A successful
export adds cookies to the result and refreshes the retained snapshot; interruption
before that point preserves the business result without requiring fresh cookies.

Export or cleanup timeout, failure, cancellation or backend loss after the initial
handoff must not turn a completed successful Claim into a failed/canceled worker
result or replay it. This applies to a fully completed job, not merely an earlier
successful step in an unfinished workflow. Terminal progress is published after
optional export and the initial cleanup attempt or interruption, without exposing
the internal handoff marker. Business success and
`browser_cleanup_quarantined` can therefore coexist. A lost response still cannot
guarantee API persistence, and an explicitly canceled parent Run can retain its
own cancellation status. This behavior does not repair historical database rows
or automatically rerun any job; verify remote effects before a fresh manual run.

`LaneManager.stop()` retains one shielded shutdown task, reused by repeated stop
calls. Caller cancellation, including during the idle-monitor/cleanup prelude,
does not cancel that task or bypass the teardown path: nested `finally` blocks
still attempt mapped-profile stop, final owner reconciliation and backend close.
The canceled caller allows a bounded settlement wait before re-raising cancellation;
if shutdown outlives that wait, the same retained task continues rather than
starting a second teardown.

The shutdown task invokes `shutdown_job_cleanup()` before backend teardown to stop
reapers and cancel tracked owner work, including native guard children and shielded
creation, close and release tasks, with a **1-second cancellation settlement wait**.
After stopping the mapped lane profiles it runs final reconciliation with a **5-second wait**,
then another bounded cancellation pass. Only confirmed closed pages with no
unresolved creator may release their leases. Unconfirmed owners/leases are retained,
not cleared just because teardown ran. If a dispatched creator returns a page
during or after shutdown, that page is first attached to its original owner; final
reconciliation or its late-return callback schedules one bounded owned-page close
and releases only after confirmation. It does not restart a job or an indefinite
reaper after shutdown. A missing identity or unconfirmed close still retains the
lease. These are cleanup-hook budgets, not a 5-second guarantee for the entire
shutdown: provider stop has its own deadline,
and uncooperative native/remote work may outlive local cancellation.

The production investigation motivating this change is evidence of the reported
multi-tab wait and completion/cleanup ambiguity, not a production retest of the
fix. Mocked regression coverage in `backend/tests/test_browser_input.py` and
`backend/tests/test_job_lifecycle.py` exercises stalled close, late creation,
quarantine, result preservation and shutdown. It does not establish real-site
success, native cancellation guarantees or automatic recovery for every remote
target. This documents workspace behavior, not a deployed fix; no production
rerun or automatic historical database result repair is implied.

## Cancellation Boundaries

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
