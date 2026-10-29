# 功能块与模板模型

> 核心模型已实现：功能目录、模板继承、站点覆盖、配置 Schema 校验、解析预览和基础编辑 UI。任务执行器仍属于下一阶段。

## 1. 概念

| 概念 | 说明 |
|------|------|
| Feature Type | 代码插件，固定 `key`，例如 `auth.form_login`、`challenge.turnstile` |
| Feature Step | 模板或站点中的一个配置实例，引用 Feature Type 并保存 `config_json` |
| Template | 有序功能步骤集合，可继承父模板 |
| Site Workflow | 站点引用模板，并覆盖/追加/禁用步骤 |
| Cardinality | `single` 或 `multiple`；登录、签到、页面识别为单例，验证/动作块可多例 |

## 2. 功能分类

```text
flow      打开站点 / 页面识别
auth      表单登录
challenge 两步验证 / Turnstile / ALTCHA / 滑块 / 图形验证码 / 问答 / 识图 / UNIT3D Claim
sign      直接导航 / 按钮 / 链接 / iframe 图像验证码签到
action    导航 / 点击 / 填表 / 等待（通用低代码块）
post      补签 / 购买补签卡
```

## 3. 插件协议

```python
class Feature:
    key: str
    name: str
    category: Literal["flow", "auth", "challenge", "sign", "action", "post"]
    cardinality: Literal["single", "multiple"]
    requires: set[Literal["vision", "totp", "cfbox"]]
    config_model: type[BaseModel]

    def run(self, ctx: WorkflowContext, cfg: BaseModel) -> StepResult: ...
```

`StepResult`：`ok / skip / fail / deferred / unknown / needs_vnc / needs_human`。

## 4. 配置 Schema 与前端

- Feature Type 用 Pydantic 定义 `config_model`，自动导出 JSON Schema。
- `GET /api/v1/features` 返回功能目录、能力依赖和 JSON Schema。
- 前端根据 Schema 自动渲染字段：字符串、枚举、数字、布尔开关、数组和键值对象。
- 必填字段由 Schema 的 `required` 生成，保存前和保存时都会校验。
- 字段默认值来自 Schema；站点/模板覆盖时，未被覆盖的字段继续使用默认值或父级值。
- 配置模型使用 `extra="forbid"`，未知字段会拒绝保存。
- 密钥字段不放入普通 `config_json`，由站点凭据/加密配置层管理。
- 站点用户名/密码已由 `site_credentials` 本地加密保存；`auth.form_login` 执行时从任务上下文读取，不进入解析结果。

## 5. 模板与覆盖

解析顺序：

1. 从根模板到子模板按 `step_key` 合并。
2. 站点步骤按 `step_key` 覆盖模板步骤；`enabled=false` 可禁用继承步骤。
3. 新步骤插入指定位置。
4. 配置对象深合并，数组替换，`null` 清除继承值。
5. 替换 `{base_url}` 占位符。
6. 校验单例数量、必填步骤和能力依赖。

模板示例：

```text
nexusphp-base
├── flow.open
├── auth.form_login
└── sign.button

nexusphp-turnstile (继承 nexusphp-base)
└── challenge.turnstile
```

## 6. 信号、条件与错误策略

执行条件不再写死登录/签到枚举，而是引用站点或模板定义的页面信号：

```text
always
signal:<signal_key>:true
signal:<signal_key>:false
```

信号定义包含：

```text
signal_key / label / description
include_texts / include_selectors
exclude_texts / exclude_selectors
match_mode: any | all
enabled
```

判断规则：

```text
包含规则命中，且排除规则未命中 → true
否则 → false
页面不可用或规则无法执行 → unknown
```

- 没有定义信号时，步骤条件只能选择 `always`。
- 条件引用不存在或已禁用的信号时，保存失败。
- 信号结果 `unknown` 时升级为人工接管，不按普通失败处理。
- 被条件引用的信号禁止删除，但允许修改检测规则。

错误策略：

```text
stop / continue / defer / escalate_interaction
```

- `stop`：步骤失败终止任务，任务状态为失败。
- `continue`：步骤本身仍记录为失败，但任务继续执行；该失败不会把整个任务状态改为失败。
- `defer`：任务状态记为推迟。
- `escalate_interaction`：任务等待人工处理，不直接切换车道；Turnstile 的自动 fallback 使用独立的转车道逻辑。

## 7. 功能依赖与站点车道

`requires` 只表示功能运行所需的外部能力，例如 `cfbox`、`totp`、`vision`，不再标记车道需求。

站点车道由两个开关直接决定：

```text
优先交互  使用代理  车道
否        否        general
是        否        interaction
否        是        proxy_general
是        是        proxy_interaction
```

- 功能块可以在任意站点使用，基础车道由站点开关决定。
- `challenge.turnstile`、`challenge.slider`、`challenge.image_captcha` 可以在普通车道执行；只有站点设置为交互车道，或 Turnstile Playwright 点击失败触发 fallback 时，才进入交互车道。
- 代理未配置时，站点的“使用代理”开关不可正常保存/运行；代理站点不得回退直连。

## 8. 图形化编辑目标

已具备：

- 单页应用布局：顶部项目名和账户菜单，左侧功能菜单，页面内完成模板/站点配置。
- 站点/模板列表、创建、编辑、删除；不使用弹窗配置。
- 步骤按列表从上到下执行；导入的模板步骤顺序固定，新增步骤可拖拽插入其中。
- 步骤 key 由前端程序生成并只读显示：模板使用 `temp_xxxxxx`，站点使用 `site_xxxxxx`；修改步骤保留原 key，用于覆盖合并。
- 信号 key 由前端生成 `sig_xxxxxx`；信号可被模板继承、被子模板/站点覆盖。
- 信号无法判断时固定升级为人工接管。
- 选择模板/父模板后，导入步骤直接展开，标注来源模板；模板链中的修改按顺序显示为子卡片。
- 当前站点/模板的修改作为最后一张子卡片显示，可移除后恢复纯模板配置。
- 编辑步骤的启用/禁用/继承、条件、错误策略、超时。
- Schema 动态字段表单：字符串、枚举、数字、布尔开关、数组增删、键值对象增删。
- 字段级继承/覆盖：父模板值显示“继承”，编辑后标记“覆盖”，可恢复继承。
- 继承/覆盖来源标记，服务端解析预览。
- 功能目录、必填字段、默认值和 Schema 查看。
- 步骤分组：模板已有分组锁定，但可新增分组；新增步骤可添加到任意分组或未分组并重新分配，已有分组步骤保持原分组。
- 系统信号「断签」在模板和站点中始终可用：存在待补签日期时为真；站点不支持补签时恒为假。
- 「支持补签最大天数」同时作为待补签历史窗口、单次购卡上限和单次补签上限，默认 30 天。
- 步骤代码配置：粘贴 JSON 后校验语法、字段和必填配置，自动填回卡片。
- 页面元素拾取器：`*_selector` 字段和信号选择器可从交互车道页面拾取；普通点击操作页面，Alt/Option+点击生成 CSS 选择器。
- 配置导入/导出：模板和站点列表支持全选/多选导入导出 JSON；模板包含分组、步骤、信号和父模板名称，站点包含站点字段、分组、步骤和信号，不包含登录凭据。导出不写入 id；导入跳过同名项，先校验名称重复、父模板/模板引用，模板按父级顺序创建且版本从 1 开始。

## 9. 当前实现

### 9.1 功能目录

内置功能块：

```text
flow.open
auth.form_login
challenge.2fa / challenge.turnstile / challenge.altcha / challenge.slider / challenge.image_captcha
sign.button / sign.iframe.image_captcha / sign.image_quiz / sign.unit3d_claim
action.navigate / action.click / action.fill / action.wait
flow.jump
flow.result
post.buy_card
post.resign
```

每个功能定义固定 `key`、分类、单例/多例、`requires` 和 Pydantic `config_model`。
常用选择器提供默认值；`challenge.2fa.service_hint` 没有默认值，属于必填字段。

### 9.2 解析语义

- 模板链从根到叶按 `step_key` 合并。
- 站点步骤按 `step_key` 覆盖模板步骤。
- 步骤执行顺序就是最终列表顺序；有分组时分组按组内最早步骤的位置排序，组内按步骤位置排序，未分组步骤按自身位置穿插在分组之间。
- 覆盖步骤使用覆盖层位置：基础步骤 2000，子模板覆盖 2001，站点覆盖 2002；同一 step_key 按位置从小到大应用，后面的覆盖前面的。
- 模板导入步骤固定，新增步骤通过内部排序值插入中间。
- 用户不再编辑顺序号或步骤 key；新增模板步骤生成 `temp_xxxxxx`，新增站点步骤生成 `site_xxxxxx`；修改子步骤沿用被修改步骤的 key。
- 覆盖步骤的 `enabled=null` 表示继承，`false` 表示禁用，`true` 表示启用。
- `enabled` 语义：
  - 继承：沿用模板/父级状态；没有父级时默认启用；
  - 启用：显式启用，可覆盖父级禁用；
  - 禁用：保留配置、不参与执行，也不贡献功能依赖。
- 步骤分组：模板定义 `workflow_groups` 和步骤 `group_key`；站点继承模板分组，只能插入本地分组。
- 模板被站点或子模板引用后，已有分组的 key、顺序、名称锁定；仍可新增分组、在分组末尾添加步骤，新步骤和旧的未分组步骤可分配到其他分组。
- 站点本地分组可自定义名称，只能放在模板分组之外或两个模板分组之间，不能进入模板分组内部。
- 步骤元数据（label/condition/on_error/timeout）为空时继承父级。
- 配置字段在 Schema 中有默认值；新增步骤时无需重复填写默认值。
- 选择器字段为有序候选数组，执行时按顺序尝试，第一个命中的选择器生效。
- 必填选择器候选为空时保存失败；可选选择器留空表示不检查。
- 页面信号选择器仍使用包含/排除规则，不按动作候选处理。
- `challenge.turnstile` 需要 CFbox：截图交给外部识别服务，返回复选框中心百分比，再由 xdotool 点击；未配置 CFbox 时不能保存该功能块。
- `challenge.image_captcha` 的默认识别方式 `ddddocr` 和 `nexusphp_gd5` 由 worker 镜像内置提供，不需要额外配置服务；只有步骤选择 `vision` 时才需要配置视觉大模型。
- 必填字段没有默认值时必须由用户输入（例如 `challenge.2fa.service_hint`）。
- 配置对象深合并，数组替换，`null` 删除继承字段。
- 站点解析时替换 `{base_url}`，包括功能默认值中的占位符。
- 启用步骤的 `requires` 只合并能力依赖；站点通过“优先交互/使用代理”开关选择车道。
- 单例功能启用多次、未知功能、配置类型错误、模板循环都会拒绝保存。
- 条件引用不存在的信号、没有信号却使用非 always 条件、删除被引用信号都会拒绝保存。

### 9.3 API

```text
GET    /api/v1/features
GET    /api/v1/templates
POST   /api/v1/templates
GET    /api/v1/templates/{id}
PUT    /api/v1/templates/{id}
DELETE /api/v1/templates/{id}
GET    /api/v1/templates/{id}/resolved

GET    /api/v1/sites
POST   /api/v1/sites
GET    /api/v1/sites/{id}
PUT    /api/v1/sites/{id}
DELETE /api/v1/sites/{id}
GET    /api/v1/sites/{id}/resolved
```

`GET /api/v1/sites` 默认返回完整站点摘要。只需要站点选择信息时使用：

```text
GET /api/v1/sites?view=options
GET /api/v1/sites?view=options&include_valid=true
```

`options` 省略凭据、步骤、信号、时间等字段；默认不执行完整工作流解析，因此 `valid` 为 `null`。需要有效性和校验错误时加 `include_valid=true`。

所有接口要求管理员会话；写操作额外要求 CSRF。删除仍被站点引用的模板，或仍有子模板的模板，返回 409。
