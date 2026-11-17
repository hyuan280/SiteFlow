# 集成配置规范：代理、2FAuth、CFbox、Bitwarden 与大模型

范围：代理、2FAuth、CFbox、Bitwarden 与多模型注册表的解析优先级、加密存储、API 与前端行为。

核心原则：

1. **环境变量优先**：同名配置存在环境变量时，以环境变量为准，前端只读。
2. **未配置即禁用**：代理车道、2FAuth 功能在缺少配置时不可用，不静默降级。
3. **密钥不回显**：环境变量来源由部署方 Secret 管理、不写入数据库；数据库来源使用 AES-256-GCM 加密存储。API 只返回掩码和来源。
4. **配置来源可追踪**：每个字段返回 `source = env | db | unset` 与 `editable`。
5. **绝不从代理静默回退直连**：代理站点在代理未配置时直接失败/推迟，防止 IP 泄露。

---

## 1. 环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `SITEFLOW_MASTER_KEY` | 是（加密时） | base64 编码的 32 字节 AES-256-GCM 主密钥，走 Docker Secret |
| `SITEFLOW_PROXY_URL` | 否 | 代理 URL；配置后才启用 `proxy_general` / `proxy_interaction` |
| `SITEFLOW_TWOFAUTH_URL` | 否 | 2FAuth 服务地址，例如 `https://2fa.example.com` |
| `SITEFLOW_TWOFAUTH_TOKEN` | 否 | 2FAuth API Bearer Token |
| `SITEFLOW_MODEL_BASE_URL` / `SITEFLOW_MODEL_ID` / `SITEFLOW_MODEL_API_KEY` | 否 | 一条可选环境变量模型的 OpenAI 兼容 `/v1` 根地址、上游模型名和密钥；空字符串视为未设置，不覆盖其他模型 |
| `SITEFLOW_MODEL_SUPPORTS_VISION` | 否 | 环境变量条目的视觉能力；显式 false 也构成环境配置。省略时使用保留的 DB 标记，否则默认 true；不需要环境变量条目时保持注释 |
| `SITEFLOW_MODEL_MAX_TOKENS` | 否 | 可选输出 Token 上限，非负整数；如 4096 发送 `max_tokens=4096`，0 不发送参数、使用提供商默认值，不代表无限输出。省略保留 DB 上限；空值会归一为 0，请保持注释而非赋空值 |
| `SITEFLOW_MODEL_TIMEOUT` | 否 | 所有模型共用的后端请求超时（秒），默认 30、最大 300；单独设置不产生或锁定环境变量模型，浏览器直连测试固定 30 秒 |

环境变量使用 `SITEFLOW_*` 前缀。

模型变量从 `SITEFLOW_VISION_*` 改为 `SITEFLOW_MODEL_*`，**旧名被忽略，没有别名回退，必须在部署配置中重命名**（上游模型名为 `SITEFLOW_MODEL_ID`）。上述五个连接设置任一个有效存在，包括显式 false/0，就形成一条普通环境变量条目；整条连接只读且不能删除，即使其余字段来自 DB 或尚未配置。缺少地址、模型或密钥时显示未配置，必须由部署方修正环境变量，不能在页面补齐。全局超时不计入这五个设置。

模板见 `.env.example`；复制为 `.env` 后执行 `chmod 600 .env`。示例：

```dotenv
SITEFLOW_MASTER_KEY=base64:CHANGE_ME
SITEFLOW_PROXY_URL=socks5://user:pass@203.0.113.10:1080
SITEFLOW_TWOFAUTH_URL=https://2fa.example.com
SITEFLOW_TWOFAUTH_TOKEN=CHANGE_ME
```

---

## 2. 代理 URL

### 2.1 支持的协议

```text
http://host:port
https://host:port
socks5://host:port
socks5h://host:port

http://user:pass@host:port
https://user:pass@host:port
socks5://user:pass@host:port
socks5h://user:pass@host:port
```

要求：

- 必须有 scheme、host、port；
- 允许无认证；
- 用户名/密码允许 URL 编码字符；
- 不支持代理 URL 携带 path/query/fragment；
- IPv6 使用 `[::1]:1080` 形式；
- `socks5h` 在内部按 SOCKS5 处理，DNS 解析由代理侧完成；
- 代理 URL 整体视为敏感配置（可能含密码），加密存储。

### 2.2 代理启用规则

| 条件 | 行为 |
|------|------|
| `SITEFLOW_PROXY_URL` 已配置 | 启用 `proxy_general` / `proxy_interaction`；代理站点强制走该代理 |
| 未配置 | 代理车道禁用；代理站点任务返回 `fail`/`deferred`，错误信息“代理未配置” |
| 配置非法 | 代理车道禁用；保存/启动校验失败；不启动浏览器 |
| 运行中代理不可达 | 该站点任务失败，不重试到直连 |

**禁止**：代理站点在代理不可用时自动改走 `general` / `interaction`。

---

## 3. 2FAuth

### 3.1 启用条件

`SITEFLOW_TWOFAUTH_URL` 与 `SITEFLOW_TWOFAUTH_TOKEN` 都解析成功后才启用 2FAuth 功能。

| URL | Token | 状态 |
|-----|-------|------|
| env | env | 启用，两个字段均只读 |
| env | db | 启用，URL 只读，Token 可替换 |
| db | env | 启用，URL 可编辑，Token 只读 |
| db | db | 启用，两个字段可编辑 |
| 缺失任一 | — | 禁用；两步验证步骤不可用 |

### 3.2 绑定规则

- 站点可通过 `site_totp_bindings.account_id` 显式绑定 2FAuth 账号；
- 未绑定时，按 `service`、站点名、域名主体生成候选；
- 候选只用于前端提示，必须人工确认；
- 候选不唯一或为空时，任务返回 `needs_human`，不静默猜测；
- 2FAuth API 没有域名字段，域名匹配是 SiteFlow 侧的启发式逻辑，不是 2FAuth 能力。

### 3.3 调用接口

```text
GET /api/v1/twofaccounts
GET /api/v1/twofaccounts/{id}/otp
```

只读取 `service/account` 用于展示；OTP 只在任务执行时获取，不落库、不写日志。

---

### 3.4 CFbox

CFbox 是外部截图识别服务，用于定位 Cloudflare Turnstile 复选框：

```text
POST {CFBOX_URL}/predict
Header: X-API-Key: <token>   # 服务端启用鉴权时需要
multipart field: image       # PNG/JPEG 截图
```

响应关键字段：

```text
has_cf_challenge
state: absent | unchecked | verified | processing | error | unknown
confidence: 0 到 1 的数值或 null
checkbox_center_pct: {x, y}
widget_box_pct
ambiguous / reason
challenges: [{state, confidence, checkbox_center_pct, ...}]
```

`state=error` 表示截图中识别到视觉错误状态，和请求异常、HTTP 非 200 或非法响应不同。Worker 会继续重新识别；状态不变时不重复打印每轮日志，等待期间会输出低频进度。`error/unknown` 的有效 `confidence < 0.95` 时，连续 3 次同状态的新截图和至少 2 秒的清晰页面状态可用于排除误检测；缺失或非法置信度不按 0 处理。完整完成判据、新 token 及连续低置信度确认见 [任务执行](execution.md)。

顶层 `unknown`、`confidence=null` 可能是 `multiple_widgets` 汇总，而不是未检测到组件。Worker 会遍历非空 `challenges`，以未解决子状态优先判断；唯一明确的 unchecked 坐标可以点击，多个 unchecked 不擅自选点。含未解决子挑战的多候选结果不能通过普通 unknown 或新 token 路径直接放行。只有所有未解决子项均为 error/unknown、各自具有有效且小于 0.95 的置信度、全部子项均无复选框坐标时，才允许独立的连续低置信度页面复核。任一高置信度、未知置信度、unchecked/processing 或坐标都会阻止该复核，不以聚合最小值代表所有子项。日志和结果保留顶层、子状态、各自置信度及复核资格，不把父级 null 当作 0。

唯一 unchecked 有有效坐标时，其他无坐标的 unknown/error/processing 不构成第二个点击目标，不再阻止该次点击；任何其他不同坐标或多个 unchecked 仍拒绝选点。允许点击与允许完成独立：未解决子结果继续阻塞通过，必须在后续截图和页面检查中确认。

配置：

```dotenv
SITEFLOW_CFBOX_URL=http://cfbox:8000
SITEFLOW_CFBOX_API_KEY=          # 可选
```

- URL 必需，API Key 可选；环境变量优先于数据库。
- 页面「系统设置 → CFbox」可保存、测试、清除；API Key 只显示掩码。
- 测试通过 `POST /predict` 发送空图片：认证通过时返回 400 `empty_image` 视为连通。
- 未配置 CFbox 时，`challenge.turnstile` 功能块保存会返回 422。

### 3.5 站点登录凭据

站点用户名和密码不放入功能步骤的 `config_json`，由站点级凭据表保存：

```text
GET    /api/v1/sites/{id}/credential
PUT    /api/v1/sites/{id}/credential   {username?, password?}
DELETE /api/v1/sites/{id}/credential
```

- 首次配置必须同时提供用户名和密码；已配置后可只替换其中一个。
- 用户名、密码分别使用 AES-256-GCM 加密，各自独立 nonce。
- AAD 绑定 `site_credential:{site_id}:username` / `site_credential:{site_id}:password`。
- API 只返回 `configured`、`username_masked`、`updated_at`、`last_verified_at`、`last_error`，不返回密码或原始用户名。
- 站点列表和详情中的 `credential` 字段只包含上述元数据。
- 站点解析结果不包含凭据；本地凭据由 API 在任务启动前解密，Bitwarden 站点由 worker 在任务启动后后台预取，进入 `auth.form_login` 时使用，任务结束清除。
- 当前仅支持本地加密存储；后续 Bitwarden 通过独立提供器接入，不改变站点/步骤模型。

### 3.6 大模型

「系统设置 → 大模型」使用 `SettingsView` 中整行宽度的 `ModelSettingsCard`。列表按优先级排列，允许 **0-20 条数据库模型（含旧 DB 配置条目），另加最多 1 条环境变量模型，合计最多 21 条**，没有必需条目或主次模型之分。环境变量条目不占数据库名额；已有 20 条 DB 模型时仍须完整显示新增的环境变量条目，不截断、丢弃或因此禁用列表操作。「添加模型」按 DB 条目数判断容量，不按总行数。旧配置条目只是普通数据库条目，可以改名、排序、停用、修改用途或删除。

- 每行显示名称、上游模型、启用开关、用途、视觉/仅文本及配置状态；上下移动、启用/临时禁用及删除先改草稿，必须点「保存排序与启用状态」。禁用保留连接，并非清空配置。
- 环境变量条目的 UI 来源标签为「环境变量」，API 仍用 `source=env`。整条连接只读、不能打开配置弹窗或删除；UI 可保存排序和启用状态，名称/用途元数据仅可通过 API 更新，不能在 UI 编辑。其五个连接字段全部禁止修改，而不是只锁有环境值的字段。
- 「配置 / 添加模型」弹窗编辑显示名称、Base URL、上游模型名称、API Key、支持视觉、`max_tokens`、启用状态和用途。「保存模型与列表」同时提交弹窗与当前列表排序、开关和删除草稿；不是只保存当前一条。
- 用途为 `feature_cards`（功能卡片）、`assistant`（智能助手及单次配置生成）和 `mcp`（仅预留）。用途只控制模型路由，不授予工具、截图、凭据、执行、导入或消费权限；当前没有 MCP runtime/server 或外部授权实现。
- API Key 永不回显；已有模型在弹窗留空表示保留，客户端通过省略字段实现，不是向 API 发送空密钥。新增模型须提供完整地址、模型名和密钥。
- 每条的正整数 `max_tokens` 原样发送，留空或 0 保存后清除该条数据库上限，不发送参数，使用上游默认值，不保证无限输出。注册表 API 省略字段保留，null/0 清除，只接受严格整数 0-2147483647 或 null，不接受空白字符串。旧 `/vision` API 仍接受 null、空白或 0 清除旧配置条目的上限。
- 后端模型请求超时全局使用 `SITEFLOW_MODEL_TIMEOUT`（默认 30 秒、最大 300 秒）；浏览器直连测试固定为 30 秒，包含读取响应。环境变量在旧连接内部仍逐字段覆盖 DB，省略的上限/视觉标记可保留 DB 值；但任一环境连接设置存在时整条连接都锁定。移除全部五个环境连接设置后才恢复为可编辑的数据库条目；其他模型不继承这些值。需要清除 DB 上限时，解除环境条目后在 UI 留空/填 0 保存。

运行时按顺序选第一个**启用、配置完整、用途匹配、符合本次视觉要求**的条目。文本调用可选仅文本模型，视觉验证码/截图判断跳过仅文本条目；禁用或用途不匹配的条目不会因自身连接无效阻止其他合格条目。配置状态不代表连通健康。选中后的请求失败、超时或非法输出不自动换下一个模型、输入模式或 OCR 方法；支持思考模型，但推理不是最终答案，也不自动协商提供商专属预算。原有响应字节预算不变。

工作流使用私有 `feature_model_runtimes` 按功能键分别选择 `feature_cards`：同一流程的文本 `flow.result` 可用首个文本模型，后面的图片验证码可用后续视觉模型。`ddddocr` 不使用大模型；`flow.result` 的模型覆盖仍只是所选提供商上的上游模型名字符串，不是注册表 ID 或提供商选择器。助手每轮只选择一次 `assistant` 模型，主对话、标题、摘要及接管沿用本轮连接；不会为图片自动换模型。「助手设置」的 null 上限继承**本轮所选条目**，显式 0/正整数覆盖，不影响其他入口。详见 [模型路由与 API](vision-feature-settings.md#model-registry)。

注册表接口为 `GET/PUT /api/v1/integrations/models` 和 `POST /api/v1/integrations/models/{model_id}/test`。GET 返回 `{revision, items}`，每项新增行级 `source=env|db` 与 `config_editable`，独立于 `config` 内的字段来源；环境变量条目为 `source=env, config_editable=false`。PUT 提交 `{expected_revision, items}` 的**完整有序元数据列表**，每项可附稀疏 `config` 补丁；没有必需的主模型 ID，无环境变量条目时可为空。新条目 ID 为规范 UUID4；`primary` 仅保留为旧配置的内部兼容 ID，不是 UI 角色或排序优先权。省略已有 DB 条目表示删除；省略环境变量条目或提交其任意连接补丁返回 409 `model_config_locked`。保存按修订及密文 CAS；409 `model_registry_conflict` 保留草稿、不自动重试，核对后刷新会丢弃草稿，再重新编辑。响应丢失也须先刷新核对。

行内「测试」保持无请求体，只测试指定 ID 已保存的有效配置（含环境覆盖），不测试草稿。弹窗「测试当前配置」也适用于未配置/新增条目，独立校验连接参数，不受名称、启用状态或用途控制；没有已保存密钥时必须输入密钥。新增条目或输入非空白 API Key 时，浏览器以原生 `fetch` 向当前 Base URL 的 `/chat/completions` 发 POST；测试不把新密钥交给 SiteFlow 后端，只有显式「保存」才按正常流程提交。已有条目密钥留空时，测试 API 接受可选 `{expected_revision, config}`，`config` 仅含 `base_url`、`model`、`supports_vision`、`max_tokens` 非密钥草稿补丁，在内存中与**指定条目**的有效已保存连接/密钥合并；不按用途重选、不绕过环境锁。修订过期返回 409 `model_registry_conflict`，锁定字段补丁返回 409 `model_config_locked`，均不自动保存或重试。复用已保存密钥而修改目标地址时，须额外明确确认目标，意味着主动允许把该密钥发往编辑后的 URL；密钥从不返回浏览器。

保存提示保持简短：「保存前请测试连通性，测试不代表保存配置」。所有测试均只预览结果，不写配置、不改变修订、顺序、启用、用途或历史密钥归档。浏览器失败不自动回退到后端；后端测试即使只读也要求显式 CSRF 校验。浏览器测试只检查用户浏览器的基础连接，后端测试来自 API 而非 Worker；图片/文本成功不保证生产可达性、工具/视觉能力、实际鉴权权限或业务成功。字段校验、30 秒/64 KiB 预算、无凭据/无 referrer/无重定向策略、后端原始字节及编码响应限制和安全中文诊断见[测试 API 与安全边界](vision-feature-settings.md#saved-and-current-configuration-tests)。MCP 未实现等技术边界保留在文档，不要求在配置弹窗增加长篇警告。

`GET /integrations` 的 `vision` 及原 `/integrations/vision` PUT/DELETE/test 始终只兼容旧配置条目，不随列表首项或实际选择变化。旧 DELETE 只清五个 DB 连接字段（地址、模型、密钥、视觉、上限），保留注册表元数据；列表删除旧配置条目则同时删除该条元数据和活动 `vision.*` 连接，避免再次读取时复活。任一环境连接设置存在时拒绝连接修改/清除/条目删除。旧 PUT/DELETE 同样推进注册表修订，旧客户端 Cookie/会话行为不变。旧条目不完整或可识别的 URL/上限错误可展示字段错误；DB 来源可编辑修复，ENV 来源须修改部署配置。损坏/无法解密的注册表仍返回静态 503，不静默当成默认配置。

加密 `integration_configs` 键 `models.registry` 保存有序元数据、新增模型连接秘密及私有历史脱敏密钥；旧连接仍存原五个 `vision.*` 键，保留原 AAD 兼容，不代表 UI 角色。读取和纯元数据保存不改写已有密文。无注册表时只读合成 revision 0：有旧 DB 连接或任一环境连接设置才合成启用的普通条目，用途 `feature_cards` + `assistant`（MCP false）；否则为空。已有注册表缺少环境变量条目时将其追加到末尾，不强行排首位，不写库/探测网络/修改 Cookie。不新增 SQL 列，升级须配套 **API、Worker、Web** 并重命名部署环境变量；不是生产部署或在线站点验证声明。

替换密钥、删除数据库条目（含旧配置条目）或清除旧连接后，旧密钥仅为历史日志脱敏保留在加密 `redaction_keys` 中，去重且最多 **512** 个，也受整个文档 256 KiB 上限约束；达到限制拒绝整次事务，不静默丢弃旧密钥。归档从不用于运行时调用、不返回 API，也不会复活已删除条目，因此删除不承诺永久擦除密钥。artifact 日志脱敏使用所有已知模型密钥，含禁用、未使用、MCP 预留及已退役密钥；秘密和私有模型连接不进入助手明文历史或工作流快照。备份数据库并单独保护主密钥；完整契约见 [存储与安全](vision-feature-settings.md#selection-and-storage)。

## 4. 配置解析优先级

代理、2FAuth 等集成按字段独立解析。模型的旧连接内部也按字段解析，但任一 `SITEFLOW_MODEL_*` 连接设置存在时整条连接都只读；其他模型连接只从注册表数据库读取，名称/启用/用途/顺序没有环境覆盖：

```text
环境变量存在
  └─ 使用 env 值，source=env，editable=false
否则数据库存在加密配置
  └─ 解密使用，source=db，editable=true
否则
  └─ source=unset，功能禁用
```

说明：

- 环境变量不会写回数据库；Docker Secret / `.env` 的权限由部署方负责（`.env` 必须 600）。
- 数据库中已有配置但环境变量存在时，数据库值被忽略但保留；移除环境变量后自动恢复使用。
- 环境变量存在时，API 拒绝修改/清除对应字段（返回 `409 CONFIG_LOCKED_BY_ENV`）。
- 模型注册表使用 `409 model_config_locked`，环境变量条目的全部连接字段均被锁定，未提供环境值的字段也不能编辑；`SITEFLOW_MODEL_TIMEOUT` 不参与该判断。
- 修改数据库配置必须写审计日志。

---

## 5. 数据模型

```sql
integration_configs(
  key TEXT PRIMARY KEY,                  -- proxy.url / twofauth.url / twofauth.token
  value_ciphertext BLOB NOT NULL,
  nonce BLOB NOT NULL,
  key_version INTEGER NOT NULL,
  is_secret INTEGER NOT NULL DEFAULT 1,
  fingerprint TEXT NOT NULL,             -- 用于展示掩码，不参与解密
  updated_at TEXT NOT NULL,
  updated_by TEXT
);
```

- 所有集成配置统一加密，不区分 URL/Token。
- `fingerprint` 可为 HMAC-SHA256(明文) 的前 8 位，用于 UI 显示 `****a1b2c3d4`。
- 主密钥：`SITEFLOW_MASTER_KEY`，AES-256-GCM，AAD 绑定 `key + key_version`。
- 密钥轮换：新增 `key_version`，后台重加密；旧版本保留直到全部轮换完成。
- 备份：数据库备份与主密钥分开保存；主密钥丢失则密文不可恢复。

---

## 6. API

### 6.1 查询

```http
GET /api/v1/integrations
```

响应示例：

```json
{
  "proxy": {
    "enabled": true,
    "source": "env",
    "editable": false,
    "protocol": "socks5",
    "url_masked": "socks5://user:***@203.0.113.10:1080"
  },
  "twofauth": {
    "enabled": true,
    "url": {
      "value": "https://2fa.example.com",
      "source": "env",
      "editable": false
    },
    "token": {
      "configured": true,
      "source": "db",
      "editable": true,
      "masked": "****a1b2c3d4"
    }
  }
}
```

规则：

- 代理 URL 永不返回明文密码；只返回协议、用户名、主机、端口和掩码。
- 2FAuth URL 不是机密，可返回值；Token 永不返回原值。
- `enabled=false` 时同时返回 `reason`。

### 6.2 修改

```http
PUT /api/v1/integrations/proxy
PUT /api/v1/integrations/twofauth
DELETE /api/v1/integrations/proxy
DELETE /api/v1/integrations/twofauth
```

- 请求体中的密钥字段只写不读。
- 字段被环境变量锁定 → `409`。
- Token 更新允许“只提交 URL”或“只提交新 Token”；空字符串表示不修改。
- 清除配置需显式 `DELETE`，不能靠空字符串误清空。
- 每次修改写 `audit_log`，记录字段名，不记录值。

### 6.3 测试

```http
POST /api/v1/integrations/proxy/test
POST /api/v1/integrations/twofauth/test
```

- 代理测试：通过代理请求出口 IP 服务，返回出口 IP 与耗时，不返回代理密码。
- 2FAuth 测试：调用账号列表，返回账号数量与 `service` 列表，不返回 secret/OTP。
- 测试使用当前解析后的配置（env 优先）。

---

## 7. 前端行为

### 7.1 设置 → 集成

代理卡片：

- 未配置：输入框可编辑，保存按钮可用；代理车道显示“未启用”。
- 数据库配置：显示掩码 `socks5://user:***@host:port`，可替换、可清除。
- 环境变量配置：输入框只读，显示掩码，标注“来自环境变量”；不提供清除按钮。
- 协议选择器/输入校验：http、https、socks5、socks5h。

2FAuth 卡片：

- URL：
  - env：显示完整 URL，只读，标注“来自环境变量”；
  - db：显示完整 URL，可编辑；
  - unset：空输入框。
- Token：
  - env：显示“已由环境变量配置”，只读，不显示任何片段；
  - db：显示“已配置（****a1b2c3d4）”，提供“替换 Token”；
  - unset：密码输入框。
- 保存后只返回 `configured/masked/source`，不返回原值。
- 页面刷新后不保留明文 Token。

### 7.2 禁用状态

- 站点通过「优先交互」和「使用代理」开关选择车道。
- 代理未配置时，「使用代理」开关不应被启用；已配置为代理车道的站点在代理未配置时显示红色告警，禁止运行。
- 2FAuth 未配置时，`challenge.2fa`（验证分类）功能块显示“2FAuth 未配置”，不可添加到工作流或运行时直接失败。

---

## 8. 校验与安全

### 8.1 代理校验

- scheme ∈ `http/https/socks5/socks5h`；
- host 非空，port 1–65535；
- 禁止 path/query/fragment；
- 凭证可省略；若存在密码，必须整体加密；
- 保存时仅做格式校验，连通性由“测试”按钮验证。

### 8.2 2FAuth 校验

- URL 必须是 `http/https`，去掉末尾 `/`；
- Token 非空；
- 测试调用 `GET /api/v1/twofaccounts`，401/403 视为配置错误；
- 不在日志中输出 Token、OTP、secret。

### 8.3 日志脱敏

以下内容必须替换：

- 代理 URL 中的 `user:pass@` → `user:***@`；
- 2FAuth Token；
- OTP 原值（只记录“已获取，长度 6”）；
- Cookie/storage_state 内容。

### 8.4 运行时解析

- 每次任务开始时重新解析配置，避免长期缓存环境变量/数据库旧值。
- 代理/2FAuth 配置变更不需要重启 worker；前端保存后下一任务生效。
- 环境变量变更需要重启容器（Docker 环境变量不可热更新）。

---

## 9. 后端实现状态（P1 foundation）

已实现：

- `backend/app/core/config.py`：读取 `SITEFLOW_*`，环境变量优先；
- `backend/app/core/crypto.py`：AES-256-GCM、AAD 绑定、指纹；
- `backend/app/models/integration.py`：`integration_configs` 表；
- `backend/app/services/integrations.py`：env/db 解析、掩码、校验、CRUD、代理/2FAuth 连通性测试；
- `backend/app/api/v1/integrations.py`：GET/PUT/DELETE/test；
- `backend/app/models/workflow.py`：`site_credentials` 表；
- `backend/app/services/credentials.py`：凭据加密、掩码、更新、解密；
- `backend/app/api/v1/sites.py`：站点凭据 GET/PUT/DELETE；
- `backend/tests/`：认证、配置、凭据、车道等测试。

启动：

```bash
cd backend
../.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

测试：

```bash
cd backend
../.venv/bin/pytest
```

管理员认证与登录页已接入；详见 `docs/authentication.md`。配置 API 现在要求登录，写操作还要求 CSRF。

## 10. 验证

通用验证结论见 `docs/verification.md`。

不要提交真实服务配置、凭据、截图或运行数据。`.env` 不进入 Git 或镜像，权限保持 `600`。

---

## 4. Bitwarden

Bitwarden 集成由一个独立的 `bw` CLI HTTP 包装容器提供，SiteFlow 不在本地保存 Bitwarden 条目中的用户名和密码。

### 4.1 配置优先级

服务地址、Token 和启用状态分别按以下顺序解析：

1. 环境变量：`SITEFLOW_BITWARDEN_URL`、`SITEFLOW_BITWARDEN_TOKEN`、`SITEFLOW_BITWARDEN_ENABLED`
2. 数据库：系统设置页保存的 Bitwarden 配置
3. 自动检测：`SITEFLOW_BITWARDEN_AUTO_DETECT=true` 时探测 `http://bitwarden:8090`

配置来源在系统设置卡片中显示为 `env`、`db`、`auto` 或 `unset`。环境变量提供字段只读；数据库配置可以指向外部部署的包装容器。

### 4.2 包装容器

```dotenv
BW_SERVER=https://vault.example.com
BW_CLIENTID=
BW_CLIENTSECRET=
BW_PASSWORD=
BW_API_TOKEN=
BW_CACHE_SECONDS=60
```

- `BW_CLIENTID`、`BW_CLIENTSECRET`、`BW_PASSWORD` 必需，容器启动后自动登录并解锁保险库。
- `BW_API_TOKEN` 可选；设置后 SiteFlow 调用 `/lookup` 必须携带相同 Bearer Token。
- `BW_CACHE_SECONDS` 控制条目缓存时间；查询前会执行一次 `bw sync --force`。

接口：

```text
GET  /health             # 无需 Token，用于 compose 自动检测
POST /check              # 需要 Token；登录并解锁，供系统设置「测试」使用
POST /lookup             # Bearer Token 可选；body: {mode, host, custom}
POST /sync               # 强制同步
```

`mode` 取值：

- `domain`：完整域名匹配；
- `root_domain`：主域名匹配（内置常见多级后缀）；
- `custom`：匹配条目名称、登录 URL 或用户名片段。

### 4.3 站点配置

Bitwarden 集成处于启用状态时，站点凭据编辑页显示「Bitwarden」标签：

- 启用开关：从 Bitwarden 获取凭据；服务不可用时显示「不支持」；
- 匹配方式：完整域名匹配、主域名匹配、自定义；
- 自定义匹配值：仅「自定义」时显示。

站点启用 Bitwarden 后，本地用户名/密码不再用于登录；Cookie 仍独立生效。API 只下发 Bitwarden 查询参数，worker 在任务启动后后台查询，进入 `auth.form_login` 时等待并使用结果；如果登录步骤未执行，不会阻塞任务。查询失败会使登录步骤失败并在任务消息中说明原因，不会回退到本地凭据。
