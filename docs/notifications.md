# 通知设置

左侧菜单「通知设置」位于「系统设置」上方，用于在任务运行结束后发送邮件或 HTTP 通知。

## 通知布局

页面最上方的「通知布局」同时作用于邮件和 HTTP 通道的原任务通知。助手调查通知使用独立纯文本简报，不套用或修改此自定义布局。

- 通知标题：1–200 字符，用于邮件主题和 HTTP 通知所选的标题字段。
- 正文布局：1–20000 字符，支持 `{{变量}}` 占位符。
- 正文格式：纯文本或 HTML。邮件按所选格式发送；HTTP 请求体始终附带 `format` 字段。
- HTML 格式下，站点名、状态和消息等变量会被 HTML 转义；布局模板本身不转义。
- 未知占位符会原样保留，不会导致发送失败。

可用变量：

```text
run_id, status, status_label, trigger, trigger_label, mode, created_by,
started_at, finished_at, created_at, site_count,
success_count, fail_count, waiting_count, unknown_count, deferred_count,
canceled_count, unsuccessful_count, sites, deferred_sites, unsuccessful_sites,
siteflow_domain, summary
```

- `deferred_count` / `deferred_sites`：推迟站点数量 / 列表，仅包含 `deferred` 状态。
- `unsuccessful_count` / `unsuccessful_sites`：未成功站点数量 / 列表，与分析页一致，严格包含 `fail`、`error`、`unknown`、`waiting_human`、`deferred`、`canceled`、`interrupted`，排除 `success`、`skipped`、`queued`、`running`。
- `sites`、`deferred_sites`、`unsuccessful_sites` 格式相同，每行形如 `示例站点 D：已推迟 · 稍后重试`；空列表渲染为空字符串。
- `siteflow_domain`：SiteFlow 完整公开访问地址，来自 `Settings.siteflow_domain` / 环境变量 `SITEFLOW_DOMAIN`，未配置时渲染为空字符串。

默认布局：

```text
标题：[SiteFlow] 运行任务通知 #{{run_id}}
正文：
状态：{{status_label}}
站点：成功 {{success_count}}，失败 {{fail_count}}，推迟 {{deferred_count}}，等待人工 {{waiting_count}}
触发：{{trigger_label}}
结束时间：{{finished_at}}


{{sites}}
-------------------------------------
详细信息查看：{{siteflow_domain}}
```

时间按 `SITEFLOW_SCHEDULER_TIMEZONE` 格式化，默认 `Asia/Shanghai`。

页面预览和后端测试通知的示例为 4 个站点：A、B 成功，C 失败，D 为「已推迟 · 稍后重试」，即成功 2、失败 1、推迟 1；`summary` 也包含推迟数量。

## 触发时机

每次运行进入最终状态后立即发送一次原任务通知，包括成功、失败、错误、等待人工、取消和中断，不等待助手或额外调用模型。注册时仅本地规划接管并去重；需新模型复查时在站点原始脱敏消息前加「助手复查中，结果稍后发出。原任务结果：」，复用则直接带历史结论，不发第二条调查通知。事件、渲染正文和通道目标冻结加密保存为 ready。通知在 API 进程内异步发送，不阻塞任务收尾；通道发送失败不影响运行结果。

只有在「通知设置」中启用、且运行结果通过该通道筛选条件的通道才会在真实运行结束后发送。页面上的「发送测试」使用当前通道表单，无需先保存，也不会保存修改。SMTP 密码和 HTTP 请求头留空时由后端沿用已有配置。测试不受启用状态和筛选条件影响。

每个渠道独立保存三个开关：

- 「接收成功通知」：开启时成功和失败都发送；关闭后只接收失败或异常结果，运行状态为 `success` 的不发送。
- 「接受手动运行结果通知」：开启时手动执行和自动调度都发送；关闭后只接受 `trigger = schedule` 的自动调度运行结果。
- 「接收助手调查通知」（`notify_assistant`，默认 true）：仅实际接管完成且有有效简短 `completed_analysis` / `updated` 结论时另发一条调查结果；只影响当前邮件或 HTTP 通道，通道本身仍须启用，不依赖上面原任务的成功/手动筛选。failed、unknown、revoked 或无有效结论不发，未接管不声称已调查。

开关保存在数据库，始终可修改，不受环境变量配置锁定影响。读取、刷新和保存通知设置不调用模型，不授予或扩大接管权限。

助手调查通知为独立纯文本简报，标题为 `[SiteFlow] 助手调查结果 #<run_id>`，正文含站点、来源运行/站点任务、原任务状态、调查状态和结论。updated 表示「已更新配置但未重新验证」，不表示业务成功；任务原状态、消息、统计和原通知布局均不改写。完成/正式结论与独立 `assistant_takeover_notices` 账本原子提交；先完成原任务通知的发送尝试，再派发调查通知，不保证收件顺序。两类通知各自网络前认领、最多一次尝试，重启不明状态记 unknown、不重发；通道独立，失败不阻塞其他通道。持久化、升级和送达边界见[运行通知](run-notifications.md)。

## 邮件通知

字段：

- SMTP 主机
- SMTP 端口（默认 587）
- 加密方式：SSL、STARTTLS、不加密
- 用户名 / 密码（用户名可选；填写用户名时必须填写密码）
- 发件人
- 收件人（逗号、分号或换行分隔）

原任务邮件正文按布局的格式发送为 `text/plain` 或 `text/html`，标题作为 Subject。助手调查邮件始终使用独立的 `text/plain` 简报。

常见端口：465 使用 SSL，587/25 使用 STARTTLS。部分云服务器或 ISP 会封锁 465/587，此时连接会超时；请确认网络出口允许 SMTP，或使用服务器提供的其他提交端口。

## HTTP 通知

HTTP 助手调查通知支持在同一个请求中发送标题、接管结论和失败截图。

字段：

- 请求地址（http/https）
- 请求方法：POST / PUT / PATCH
- 标题字段：`title`（默认）、`subject`、`heading`。
- 正文字段：`content`（默认）、`text`、`message`、`body`、`description`。只发送所选字段；依赖 `body` 的旧 Webhook 需选择 `body`。
- 图片字段：无（默认）、`image`、`file` 或 `attachment`。
- 请求头 JSON（可选）
- 超时时间（1–120 秒，默认 10）

图片字段为「无」时使用 JSON。以下是使用默认标题和正文字段的原任务通知示例：

```json
{
  "event": "run.finished",
  "title": "[SiteFlow] 运行任务通知 #128",
  "content": "布局渲染后的正文",
  "format": "text",
  "run": {
    "run_id": 128,
    "status": "success",
    "status_label": "成功",
    "trigger": "schedule",
    "trigger_label": "定时调度",
    "site_count": 3,
    "success_count": 2,
    "fail_count": 1,
    "started_at": "...",
    "finished_at": "...",
    "sites": [
      {
        "site_id": 1,
        "site_name": "示例站点",
        "status": "success",
        "status_label": "成功",
        "message": "签到成功",
        "lane": "general",
        "attempt": 1
      }
    ]
  }
}
```

请求头值不会通过 API 回显，只显示当前请求头名称。保存时留空保持现有请求头；输入 `{}` 清除全部请求头；输入完整 JSON 会整体替换。

### 发送图片

1. 在「通知设置 → HTTP 通知」中，按接收端要求选择标题和正文字段，再将图片字段设为 `image`、`file` 或 `attachment`。例如，接收端要求 `title`、`content`、`image`，就选择这三项。
2. 点击「发送测试」验证接收端能否同时收到正文和内置的 `notification-test.png`。测试使用当前表单，不需要先保存。
3. 测试通过后保存 HTTP 通知配置。

选择图片字段后，系统自动用 `multipart/form-data` 发送，包含 boundary 的 `Content-Type` 由系统生成，无需手动设置。标题、正文和元数据作为表单字段发送，其中 `run` 是 JSON 字符串；图片以 `image/png` 文件上传，接收端需支持表单上传。

定时调度和手动运行的原任务通知均不附带图片。若接收端只接受 JSON，请将图片字段设为「无」。

助手调查通知使用 `event: "assistant.investigation"`、`format: "text"`，有独立的标题和正文。启用 HTTP 通道和「接收助手调查通知」并选择图片字段后，接管完成且有有效调查结论时，最多附带被接管站点任务最新的一张可用失败截图（PNG，最大 10 MiB）。没有可用截图时仍发送结论。来源字段 `run_id`、`run_status`、`job_id`、`job_status`、`site_id`、`site_name`、`conclusion`、`action_status` 位于顶层，不使用原任务通知的嵌套 `run` 对象或自定义布局。

## 环境变量与页面配置

每个字段优先使用环境变量，其次是数据库（页面保存）。来源为环境变量的字段在页面只读：

- `env`：环境变量，字段不可修改
- `db`：页面保存，可修改
- `unset`：未配置

启用开关始终可以在页面修改，即使通道配置来自环境变量。数据库中的启用值优先；没有数据库记录时使用环境变量的 `*_ENABLED` 默认值。环境配置若未设置 `*_ENABLED`，默认不启用。

数据库中的邮件密码、HTTP 请求头等配置使用 `SITEFLOW_MASTER_KEY` 加密保存；邮件密码、HTTP 请求头值不会回显。

### 公开访问地址

在后端环境中配置完整公开访问地址（包含 `https://` 或 `http://`）：

```dotenv
SITEFLOW_DOMAIN=https://siteflow.example.com
```

该变量对应 `Settings.siteflow_domain`，用于渲染模板中的 `{{siteflow_domain}}`；未配置时为空字符串，不会自动推断访问地址。修改环境变量后需重启后端。

已保存的自定义通知模板不会被新的默认模板覆盖。如需使用新的变量或默认布局，请在「通知布局」中手动修改并保存。

### 邮件环境变量

```dotenv
SITEFLOW_NOTIFICATION_EMAIL_ENABLED=false
SITEFLOW_NOTIFICATION_EMAIL_SMTP_HOST=smtp.example.com
SITEFLOW_NOTIFICATION_EMAIL_SMTP_PORT=587
SITEFLOW_NOTIFICATION_EMAIL_SECURITY=starttls
SITEFLOW_NOTIFICATION_EMAIL_USERNAME=
SITEFLOW_NOTIFICATION_EMAIL_PASSWORD=
SITEFLOW_NOTIFICATION_EMAIL_FROM=siteflow@example.com
SITEFLOW_NOTIFICATION_EMAIL_TO=admin@example.com,ops@example.com
```

`SITEFLOW_NOTIFICATION_EMAIL_SECURITY` 支持 `ssl`、`starttls`、`none`。

### HTTP 环境变量

```dotenv
SITEFLOW_NOTIFICATION_HTTP_ENABLED=false
SITEFLOW_NOTIFICATION_HTTP_URL=https://hooks.example.com/siteflow
SITEFLOW_NOTIFICATION_HTTP_METHOD=POST
SITEFLOW_NOTIFICATION_HTTP_TITLE_FIELD=title
SITEFLOW_NOTIFICATION_HTTP_BODY_FIELD=content
SITEFLOW_NOTIFICATION_HTTP_IMAGE_FIELD=none
SITEFLOW_NOTIFICATION_HTTP_HEADERS='{"Authorization":"Bearer CHANGE_ME"}'
SITEFLOW_NOTIFICATION_HTTP_TIMEOUT=10
```

HTTP 方法支持 `POST`、`PUT`、`PATCH`；请求头必须是 JSON 对象。

## API

```text
GET    /api/v1/notifications
PUT    /api/v1/notifications/layout
PUT    /api/v1/notifications/email
DELETE /api/v1/notifications/email
PUT    /api/v1/notifications/http
DELETE /api/v1/notifications/http
POST   /api/v1/notifications/email/test
POST   /api/v1/notifications/http/test
```

`DELETE` 只清除数据库配置，环境变量配置保持不变。

HTTP 配置的 `title_field`、`body_field` 和 `image_field` 支持 PUT 更新；省略保留原值，传 `null` 恢复默认。已保存的 `text_field` 和旧环境变量 `SITEFLOW_NOTIFICATION_HTTP_TEXT_FIELD` 仍可用作正文字段配置。

邮件/HTTP 的 GET 视图及各自 PUT 请求支持布尔字段 `notify_assistant`，未保存记录时默认 true，PUT 省略保留原值。修改请求沿用登录及 CSRF 检查，通知开关不是模型可调用的发送接口。
