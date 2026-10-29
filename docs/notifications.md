# 通知设置

左侧菜单「通知设置」位于「系统设置」上方，用于在任务运行结束后发送邮件或 HTTP 通知。

## 通知布局

页面最上方的「通知布局」同时作用于邮件和 HTTP 通道：

- 通知标题：1–200 字符，用于邮件主题和 HTTP 的 `title`。
- 正文布局：1–20000 字符，支持 `{{变量}}` 占位符。
- 正文格式：纯文本或 HTML。邮件按所选格式发送；HTTP 请求体始终附带 `format` 字段。
- HTML 格式下，站点名、状态和消息等变量会被 HTML 转义；布局模板本身不转义。
- 未知占位符会原样保留，不会导致发送失败。

可用变量：

```text
run_id, status, status_label, trigger, trigger_label, mode, created_by,
started_at, finished_at, created_at, site_count,
success_count, fail_count, waiting_count, unknown_count, deferred_count,
canceled_count, sites, summary
```

默认布局：

```text
标题：[SiteFlow] 运行 #{{run_id}} {{status_label}}
正文：
SiteFlow 运行 #{{run_id}} 已结束
状态：{{status_label}}
站点：{{site_count}} 个（成功 {{success_count}}，失败 {{fail_count}}，等待人工 {{waiting_count}}）
触发：{{trigger_label}}
结束时间：{{finished_at}}

{{sites}}
```

时间按 `SITEFLOW_SCHEDULER_TIMEZONE` 格式化，默认 `Asia/Shanghai`。

## 触发时机

每次运行进入最终状态后发送一次通知，包括成功、失败、错误、等待人工、取消和中断。通知在 API 进程内异步发送，不阻塞任务收尾；单个通道发送失败只写日志，不影响运行结果。

只有在「通知设置」中启用、且运行结果通过该通道筛选条件的通道才会在真实运行结束后发送。页面上的「发送测试」不受启用状态和筛选条件影响，但要求配置可用。

每个渠道独立保存两个筛选开关：

- 「接收成功通知」：开启时成功和失败都发送；关闭后只接收失败或异常结果，运行状态为 `success` 的不发送。
- 「接受手动运行结果通知」：开启时手动执行和自动调度都发送；关闭后只接受 `trigger = schedule` 的自动调度运行结果。

筛选配置保存在数据库，始终可修改，不受环境变量配置锁定影响。

## 邮件通知

字段：

- SMTP 主机
- SMTP 端口（默认 587）
- 加密方式：SSL、STARTTLS、不加密
- 用户名 / 密码（用户名可选；填写用户名时必须填写密码）
- 发件人
- 收件人（逗号、分号或换行分隔）

邮件正文按布局的格式发送为 `text/plain` 或 `text/html`，标题作为 Subject。

常见端口：465 使用 SSL，587/25 使用 STARTTLS。部分云服务器或 ISP 会封锁 465/587，此时连接会超时；请确认网络出口允许 SMTP，或使用服务器提供的其他提交端口。

## HTTP 通知

字段：

- 请求地址（http/https）
- 请求方法：POST / PUT / PATCH
- 请求头 JSON（可选）
- 超时时间（1–120 秒，默认 10）

请求体：

```json
{
  "event": "run.finished",
  "title": "[SiteFlow] 运行 #128 成功",
  "body": "布局渲染后的正文",
  "content": "与 body 相同，兼容使用 {content} 的 webhook 模板",
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

## 环境变量与页面配置

每个字段优先使用环境变量，其次是数据库（页面保存）。来源为环境变量的字段在页面只读：

- `env`：环境变量，字段不可修改
- `db`：页面保存，可修改
- `unset`：未配置

启用开关始终可以在页面修改，即使通道配置来自环境变量。数据库中的启用值优先；没有数据库记录时使用环境变量的 `*_ENABLED` 默认值。环境配置若未设置 `*_ENABLED`，默认不启用。

数据库中的邮件密码、HTTP 请求头等配置使用 `SITEFLOW_MASTER_KEY` 加密保存；邮件密码、HTTP 请求头值不会回显。

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
