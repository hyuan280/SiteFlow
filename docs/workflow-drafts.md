# 配置草稿校验

草稿接口接受与站点、模板保存接口相同的配置 JSON，在不保存配置的情况下完成校验并返回生效预览。它为后续 AI 生成草稿、编辑器校验提供基础，目前不包含模型调用或新的前端入口。

## API

| 方法与路径 | 场景 |
| --- | --- |
| `POST /api/v1/sites/validate` | 新建站点草稿 |
| `POST /api/v1/sites/{site_id}/validate` | 编辑已有站点草稿 |
| `POST /api/v1/templates/validate` | 新建模板草稿 |
| `POST /api/v1/templates/{template_id}/validate` | 编辑已有模板草稿 |

所有接口均要求管理员会话及 `X-CSRF-Token`。成功和预期错误响应均带有 `Cache-Control: no-store`。校验不会更新会话活跃时间或删除过期会话。

请求体分别使用 `SiteWrite`、`TemplateWrite`，字段定义可在 `/docs` 查看。例如：

```http
POST /api/v1/sites/validate
Content-Type: application/json
X-CSRF-Token: <当前会话 CSRF token>
```

```json
{
  "name": "示例站点",
  "base_url": "https://example.com/",
  "steps": [
    {"step_key": "open", "feature_type": "flow.open", "config": {}},
    {"step_key": "sign", "feature_type": "sign.button", "config": {}}
  ]
}
```

成功返回 `200`，结构如下；`local`、`resolved` 在实际响应中是完整对象：

```text
{
  "valid": true,
  "owner_type": "site",
  "owner_id": null,
  "history_key": null,
  "current_revision": null,
  "local": { ...规范化后的本地配置... },
  "resolved": { ...继承合并并应用当前功能默认值后的完整工作流... }
}
```

- 新建草稿不分配数据库 ID、稳定身份或版本；上述三个元数据字段及 `resolved.owner_id` 为 `null`。
- 编辑草稿返回当前对象的 ID、`history_key` 和当前版本，版本不会递增。
- `local` 保留本地覆盖语义，不把继承后的结果铺平回写；不含 `expected_revision`、`expected_history_key`、凭据或 Cookie。
- `resolved` 包含步骤、分组、信号、功能依赖 `requires` 和提示 `warnings`。它只是预览，不是可直接保存的请求体。
- 校验沿用现有首个错误返回方式，不返回 `valid: false`。请求结构、功能参数、条件、信号、分组和排序等错误返回 `422`；名称、版本、稳定身份及已使用模板的结构冲突返回 `409`；对象或直接引用的模板不存在返回 `404`。错误体沿用保存接口的 `detail`。

## 编辑语义

编辑使用与 PUT 相同的完整替换规则，而非 PATCH。省略步骤、分组、信号等集合会按默认空集合校验；站点更新省略 `trusted_urls` 时保留当前值，本地分组已有 `step_anchor` 在提交 `null` 时也沿用普通保存的保留规则。

建议编辑请求始终提供 `expected_revision` 和 `expected_history_key`。草稿接口只读取并核对，不抢占或递增版本。新建时这两个字段与普通创建接口一样不作为前置条件。

模板编辑不仅校验自身，也按候选配置校验所有受影响的子模板和站点，包括未启用的对象。不能绕过已使用模板的结构限制、信号删除保护或继承循环检查。

校验通过不是保存授权，也不锁定依赖。用户确认后仍需调用普通 POST/PUT；应使用原先的版本及身份前置条件，保存时重新校验并创建历史。配置、功能设置或插件在两次请求之间发生变化时，保存仍可能失败，不应自动覆盖或重试。

## 副作用与边界

校验只使用内存候选对象和数据库读取，不执行配置 SQL 写入，不创建历史、任务或执行记录，不加解密配置历史，不读取账户凭据或 Cookie，不启动浏览器或执行工作流。不会通过“保存后回滚”实现预览。已有配置及调用方传入的草稿不会被修改。

仍会读取当前模板链和功能设置，并使用与保存相同的功能 Schema 校验。需要 CFbox 的步骤只检查当前集成是否已配置，不进行连通性测试；读取加密存储的集成配置时可能需要解密该配置字段。插件功能也沿用现有 Schema 校验路径。

校验不验证选择器能否命中真实页面、登录是否成功、凭据是否可用或业务操作是否有效，也不调用大模型。加密历史、数据库约束竞争和提交失败仍属于真正保存阶段的运行结果。

普通配置字段、URL 和步骤参数可能含手工填写的秘密。该接口是管理员完整配置预览，不是脱敏接口；后续发送到模型前仍必须单独筛选上下文。
