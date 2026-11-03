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
challenge.grid_captcha
action.navigate / action.wait / action.click / action.fill
flow.result
sign.button / sign.iframe.image_captcha / sign.image_quiz / sign.unit3d_claim
post.buy_card
post.resign
post.resign_click
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
- `post.resign_click`（页面点击补签）：独立的页面交互功能，不修改旧 `post.resign` 的配置或提交方式。仅处理 API 下发的过去待补签日期；确认月份，唯一定位当月目标日期，点击后确认目标 selected，再点击文字明确包含“补签/補簽”的按钮。日期定位、选中确认及 done 标记必须绑定 `{date}` 或 `{day}`，支持 `{year}/{month}`；`{day}` 仅在目标月已确认且排除相邻月时使用。只在目标日期 done 出现且补签卡恰好减少 1 时确认成功。
- 页面补签每次提交前调用内部持久化检查点接口，以任务级 token 原子预留 `(site_id, date)`，事务提交并收到 ACK 后才点击。已有未决或终态日期不能重复预留，排队旧任务也不能绕过；页面确认后再次持久化确认。原生弹窗默认拒绝，跨源/新窗口/日期歧义/结果不明时转人工，不被 `on_error=continue` 放行。取消或断连保留已确认部分结果；后续任务对未决日期先只读检查 done，没有明确完成标记则停止，不再消费卡片。预留 ACK 丢失时可能保留“未点击但未决”的保守记录，需要人工核验；这不是远端事务或 exactly-once 保证。
- 待补签日期由 API 按站点「支持补签最大天数」生成并注入 worker：从该站点最早有运行记录的日期开始，窗口内没有成功运行或成功补签记录的日期都算缺失；今天等当日结束后再判定。补签结果写入 `site_resign_records`，同时合并到状态矩阵和趋势。
- 站点 Cookie 由系统主动维护：任务开始前注入，站点任务成功后从浏览器导出当前站点 Cookie 并加密更新；失败时保留原配置。
- `post.resign` 的历史日期范围继续使用站点最大补签天数；购卡自动数量由该范围内的待补签日期决定，固定数量由 `purchase_quantity` 决定。
- 魔力值不足时 `post.buy_card` 只购买可负担部分并继续，`post.resign` 按现有卡数补签，剩余日期保留到下次。
- 模板和站点解析信号始终包含系统信号「断签」(`sys_resign_pending`)；worker 用待补签日期数量计算真假，站点不支持补签时恒为假。条件日志会输出待补签天数。
- `post.buy_card` 未配置步骤超时时会根据购买数量、点击项、库存验证和间隔计算预算；显式步骤超时仍优先。新配置沿用 `workflow_steps.config_json`，不创建新表、不搬迁旧数据。

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

结果判断与九宫格识别采用相同兼容策略：声明可选工具 `submit_workflow_result`，`tool_choice=auto`，不强制指定工具、不使用 required 或强制 response_format。模型可以调用一次该工具，也可以在无工具调用时直接返回正文 JSON，例如 `{"result":true,"reason":"简短可见依据"}`。`result` 必须为 boolean，`reason` 必须为 1 到 500 字符的非空简短依据；两条路径均拒绝额外字段、重复 JSON 键和非法 JSON。错误或多个工具调用不会再用正文兜底；不猜测自然语言布尔结论，不从 Markdown 中提取结果，不读取 reasoning_content，允许剥离正文前导且完整闭合的 think 块。

系统将不可编辑的输出协议放入 system 消息，用户业务标准使用独立消息，不把合并后的内容写回配置。功能和步骤卡片显示只读协议说明；旧自定义提示词不自动改写，但其中的输出格式不能替代固定协议。有效 false 仍表示判断步骤执行成功、站点任务失败。匹配原输出时保留配置消息，不匹配时显示模型判断值。

成功依据经已知秘密脱敏、控制字符清理后记录日志及步骤 `data.judgment_reason`，不复制到任务 `outputs`。实际取用的凭据和 OTP 只增加任务内长度/哈希指纹，避免为脱敏长期缓存明文；指纹不发送给模型或写入结果。依据是模型生成文本，仍可能包含未识别的页面隐私，不能将脱敏视为完整 DLP。失败日志及步骤 data 区分 HTTP 错误（带状态码）、timeout、network_error、no_result、invalid_tool_call、invalid_json、schema_error、truncated、content_filtered、refused 等，并记录固定阶段和耗时；无工具时的正文解析标记 content 阶段，证据采集失败标记 evidence_error。不记录原始响应、参数、令牌或底层异常正文，不自动重试。

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

页面已就绪、组件与文本读取成功、页面检查无挑战且 CFbox 返回明确的 `absent` 或 `verified` 时，连续稳定两秒后成功。另允许上述 `verified` 加非空 token 的已解决组件保留可见，但可见 frame 的 marker/token 探测必须完整可读。普通 `unknown` 仅在本步骤从未出现未解决挑战证据时保留页面确认兼容；出现 DOM 挑战、视觉 error/unchecked/processing 或多候选未解决证据后，后续 `unknown/null` 不能自行清除阻塞。符合条件的新 token 或连续低置信度复核仍可独立确认。

CFbox 的非空 `challenges` 数组始终参与聚合，包括顶层 confidence 为 null 的情况。子状态按 unchecked、processing、error、unknown 的未解决优先级处理；只有一个明确 unchecked 时，其他无坐标 unknown/error/processing 不构成第二点击目标，可以使用该唯一坐标，但不能据此判完成。任何其他不同坐标或多个 unchecked 都拒绝选点，不选第一个。多项未解决子结果、歧义 unknown 阻止普通完成和新 token 豁免；若所有未解决子项均为无坐标 error/unknown 且各自置信度有效、严格小于 0.95，则可独立累计三轮及至少两秒的低置信度页面复核。该路径仍要求页面无可见组件、无挑战文字/URL且可读、就绪，不能只凭复选框坐标为空通过。畸形子结果失败关闭。结果保留 `cfbox_summary_state`、`challenge_states`、有效状态及置信度，诊断额外提供子置信度和复核资格。

页面标记扫描支持 open Shadow DOM、非 div 的 CF widget 容器、与可信 Turnstile 脚本关联的 `.ts-box`，并检查可见子 iframe 的内容与实际 URL。隐藏祖先和 slot 隐藏残留不单独作为挑战；扫描超限或可见 frame 检查失败不当作不存在。closed Shadow DOM 内部无法由普通页面脚本扫描，实际可访问的 frame 仍参与检测，不能把未知读取结果当作无挑战。

CFbox 检测到未解决挑战、页面仍有未解决挑战或读取失败时不放行。未配置 CFbox 返回 `waiting_human`；截图/请求/响应失败或缺失、非法状态返回 `fail`，总等待超时返回 `unknown`，不能作为“未检测到”成功。截图、页面检查、网络请求和轮询均受步骤 `wait_seconds` 总时限约束。正常稳定清除约需三次识别，成功后不再请求。
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
