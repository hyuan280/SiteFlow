# 运行分析

## 1. 页面

左侧菜单「运行分析」提供：

- 状态矩阵：站点 × 日期，展示每天汇总状态和尝试次数。
- 执行趋势：每日状态堆叠柱 + 成功率折线。
- 失败分析：每日每个站点最后一次已结束运行中状态未成功的记录，支持站点、状态、功能类型和消息搜索。

时间范围支持 7/14/30/90 天，可筛选站点；矩阵可选择是否包含停用站点。

## 2. 状态矩阵

- 日期按调度时区 `SITEFLOW_SCHEDULER_TIMEZONE` 分桶，默认 `Asia/Shanghai`。
- 汇总优先级：`success > waiting_human > unknown > deferred > fail > error > canceled > interrupted`。
- 单元格显示当天汇总状态；悬停显示尝试次数、各状态计数和最后消息。
- 点击有运行记录的单元格进入对应运行详情。

## 3. 失败分析

先按“站点 + 本地日期”选出当天最后一次已结束运行，再只保留这次运行状态不是 `success` 的记录。也就是说同一天先失败、之后成功，不会出现在失败分析中；同一天先成功后失败，会显示最后一次失败。

默认显示全部未成功状态，可切换失败/错误、等待人工、未知、推迟或取消/中断。

- 支持站点、功能类型、消息关键字筛选。
- 每个站点每天最多一条记录。
- 显示失败步骤；同时提供运行详情和失败截图入口。
- 截图已被 artifact 清理时入口消失，但失败记录仍保留。

## 4. 趋势

- 堆叠柱：成功、等待人工、未知、推迟、失败、错误、取消、中断。
- 折线：当日成功率。
- 顶部摘要：总执行、成功率、失败/错误、等待人工。

## 5. API

```text
GET /api/v1/analytics/matrix?days=14&site_id=&include_disabled=false
GET /api/v1/analytics/trends?days=30&site_id=
GET /api/v1/analytics/failures?days=7&site_id=&status=fail,error,unknown,waiting_human,deferred,canceled,interrupted&lane=&feature_type=&q=&date=&page=1&page_size=20
```

当前按原始 `site_jobs` / `job_steps` 实时聚合，避免额外汇总表与任务写入路径耦合；数据量增长后可再增加每日汇总表。
