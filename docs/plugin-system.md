# Plugin System Design

> 当前范围约束优先于下文历史设计：一个插件对应一个不可变容器。安装、升级、
> 回滚、运行时 pip、热加载和容器管理均不属于 SiteFlow 范围；下文相关设想已废弃，
> 不是产品计划。镜像、依赖与部署由外部运维负责，应用不访问 Docker Socket。

## 变更影响与移除接入

管理员可查询 `GET /api/v1/plugin-runtime/plugins/{id}/references` 与 `change-impacts`。
检查包含禁用步骤、全局 AppSetting 默认值、模板继承和站点覆盖，以及凭据、TOTP、
验证码方法引用。已有有效配置按新 JSON Schema 校验，仅输出对象/步骤标识和受控错误码，
不输出配置值。未显式设置凭据提供商仍采用原有 Bitwarden/manual 回退。
注册元数据变更保留上次元数据和当时影响快照并撤销审批；当前引用查询反映最新数据库。
Schema/default 变动不一定不兼容，检查不保证插件行为语义兼容或远端服务可用。

`POST .../remove` 保留持久化墓碑和所有工作流配置，撤销权限；旧授权/租约使用时
由 permission_epoch 和运行状态拒绝。引用不会阻止管理员明确移除。
容器重新注册被拒绝，心跳不能恢复接入。`POST .../restore` 需要管理员认证和 CSRF，
恢复后仍需重新审批。移除仅指系统接入，不会停止或删除容器。

本文描述 SiteFlow 插件系统的推荐架构。第一版目标是支持：

- 插件增加功能步骤，安装后可直接在模板和站点配置中选择。
- 插件增加图像验证码识别方式，自动出现在验证码步骤选项中。
- 插件提供 TOTP 或密码凭据来源，替代 Bitwarden/2FAuth 等内置实现。

核心设计约束：

> **一个插件对应一个容器。**
> 插件代码、依赖、数据和运行时全部限制在各自容器内；主系统通过协议调用插件。

插件容器内可以热加载插件版本并在运行时安装依赖，但只影响该插件自己的容器，不影响主系统和其他插件。

## 1. 设计原则

1. **一插件一容器**
   - 插件之间不共享文件系统、进程空间和 Python 环境。
   - 每个插件有独立资源限制和存储卷。
   - 单个插件升级、崩溃或安装依赖不会影响其他插件。

2. **主系统不加载插件代码**
   - API 和 worker 不 `import` 插件模块。
   - 主系统只持有插件 endpoint、协议版本和元数据缓存。

3. **协议优先**
   - 功能元数据、验证码方法、步骤执行、秘密获取全部通过协议完成。
   - 协议有明确版本，插件可独立升级。

4. **能力授权**
   - 插件必须在 manifest 中声明能力。
   - 普通功能插件不能读取秘密。
   - TOTP/密码提供插件属于高信任能力，必须单独审批和审计。

5. **秘密最小暴露**
   - 普通插件永远拿不到密码、Cookie、TOTP 种子。
   - 秘密提供插件只能通过一次性 lease 提供秘密。
   - 主系统日志、任务快照、运行详情不记录秘密值。

6. **浏览器操作代理**
   - 插件不能直接获取 Playwright `Page` 或 CDP。
   - 插件通过 worker 的受控动作代理操作当前站点。
   - 敏感填写由 worker 使用 secret ref 完成。

## 2. 总体架构

```text
                    ┌─────────────────────────┐
                    │ SiteFlow API            │
                    │ 注册/元数据/配置/调度     │
                    └───────▲─────────┬───────┘
                            │         │ 控制协议
                 注册/心跳   │         ▼
             ┌──────────────┴────────────────────┐
             │                                   │
    ┌────────┴───────┐ ┌────────────────┐ ┌─────┴──────────┐
    │ Plugin ACME    │ │ Plugin Foo     │ │ Plugin Vault   │
    │ 功能 + 验证码  │ │ 功能步骤       │ │ TOTP/密码      │
    └───────┬────────┘ └───────┬────────┘ └───────┬────────┘
            │                  │                  │
            └──────────────────┼──────────────────┘
                               │ 数据协议
                               ▼
                    ┌─────────────────────────┐
                    │ SiteFlow Worker         │
                    │ Browser + Action Broker │
                    └─────────────────────────┘
```

- 插件启动后主动向 API 注册，并持续发送心跳。
- API 校验注册信息后拉取元数据、刷新功能目录。
- API 和 worker 在插件可用后调用插件控制面/执行面。
- Worker 负责浏览器会话，并向插件提供受限动作代理。
- 每个插件容器同时提供控制面和执行面接口。
- 普通插件和秘密提供插件都是独立容器，互不共享文件系统。

## 2.1 插件发现与注册

### 2.1.1 发现方式

不通过 Docker Socket 扫描容器，也不依赖 mDNS/广播发现。采用：

> **插件容器启动后主动注册 + 心跳 + 主系统校验**

插件容器需要配置：

```text
SITEFLOW_CONTROL_URL=http://api:8000
SITEFLOW_PLUGIN_ID=acme
SITEFLOW_PLUGIN_TOKEN_FILE=/run/secrets/siteflow-plugin-token
SITEFLOW_PLUGIN_ENDPOINT=plugin-acme:7000
```

- `SITEFLOW_CONTROL_URL`：主系统 API 地址。
- `SITEFLOW_PLUGIN_ID`：插件唯一标识。
- `SITEFLOW_PLUGIN_TOKEN_FILE`：每插件独立注册令牌，推荐使用 Docker secret 或只读文件。
- `SITEFLOW_PLUGIN_ENDPOINT`：插件对主系统暴露的 gRPC 地址。
- `SITEFLOW_PLUGIN_ENDPOINT` 可以由插件上报，也可以由主系统根据插件 ID 推导；主系统必须校验它属于允许的内部网络。

### 2.1.2 注册流程

```text
Plugin 容器启动
    ↓
读取 CONTROL_URL / PLUGIN_ID / TOKEN
    ↓
POST /api/v1/plugin-runtime/register
    ↓
主系统校验 ID、token、协议版本、endpoint、能力
    ↓
主系统通过 gRPC 调用 Health / GetManifest
    ↓
拉取功能和验证码元数据并保存
    ↓
插件状态 = unapproved，等待管理员检查和权限审批
```

注册请求示例：

```json
{
  "plugin_id": "acme",
  "protocol_version": 1,
  "endpoint": "plugin-acme:7000",
  "manifest": {
    "id": "acme",
    "name": "Acme Plugin",
    "version": "1.0.0",
    "protocol_version": 1,
    "capabilities": ["features", "captcha_methods", "browser_actions"]
  }
}
```

主系统要求的校验：

- 插件 ID 与 token 绑定。
- 功能 key 必须使用 `plugin.<id>.` 前缀。
- 验证码方法 key 必须使用相同命名空间。
- 协议版本必须兼容。
- endpoint 必须是允许的 Docker 内部 DNS 名称或白名单网段，并带有端口。
- 解析后的地址不能是公网、loopback、link-local、组播或保留地址。
- 插件首次注册必须经过检查和权限审批；能力变更后需要重新审批。
- 同一插件 ID 的重复注册要替换旧实例并递增 generation。

### 2.1.3 主系统接口

建议新增内部接口：

```text
POST /api/v1/plugin-runtime/register
POST /api/v1/plugin-runtime/heartbeat
POST /api/v1/plugin-runtime/unregister
GET  /api/v1/plugin-runtime/plugins
GET  /api/v1/plugin-runtime/plugins/{id}
POST /api/v1/plugin-runtime/plugins/{id}/check
POST /api/v1/plugin-runtime/plugins/{id}/approve
POST /api/v1/plugin-runtime/plugins/{id}/disable
POST /api/v1/plugin-runtime/plugins/{id}/enable
```

注册接口只允许内部网络访问，并建议使用 mTLS 或每插件独立注册 token。不要使用全局共享 token。

### 2.1.4 心跳与状态

插件每隔 10–30 秒发送心跳。主系统建议使用 30–90 秒 TTL。

插件状态：

```text
registering
available
unhealthy
disabled
incompatible
unapproved
```

- 心跳超时：`unhealthy`
- 协议不兼容：`incompatible`
- 首次注册、能力变更或部分权限未审批：`unapproved`
- 管理员停止接入：`disabled`，插件连接和心跳继续
- 已批准全部声明能力且心跳正常：`available`

首次接入流程：

1. 插件注册时执行协议、端点、能力、健康检查和远端 manifest 校验。
2. 注册成功后状态为 `unapproved`，检查结果保存在注册表并显示在插件管理页。
3. 管理员在“插件管理”中勾选允许的能力并通过审批。
4. 全部声明能力获批后状态变为 `available`，插件才进入正常工作状态。
5. 完全相同的 manifest、endpoint 和功能/验证码元数据重新注册会保留审批；任一变化清空审批并重新进入 `unapproved`。
6. 管理员可以停止插件接入：插件仍保持 gRPC 连接和心跳，但状态为 `disabled`，不会进入系统功能；启用后恢复原审批状态。

`/features` 只对普通用户暴露 `available` 插件的功能。
已保存的插件步骤不能因为插件离线而被静默删除；应保留步骤并显示插件不可用。

### 2.1.5 主系统重启与插件重启

- 主系统重启后，插件注册客户端应自动重试。
- API 可以从数据库或 AppSetting 恢复最后一次插件元数据缓存，但必须重新健康检查。
- 插件重启后重新注册，主系统更新 generation。
- 主系统进程重启不需要重新配置插件列表。
- 插件升级通过 generation 切换，正在运行的任务固定使用开始时的版本。

### 2.1.6 可选的反向持久通道

第一版推荐“主动注册 + endpoint 回调”。如果希望插件完全不暴露监听端口，可以后续增加：

```text
Plugin → API: 持久 gRPC stream / WebSocket
API → Plugin: 元数据、执行、识别请求
```

这种方式适合远程插件、NAT 和严格防火墙环境，但协议代理更复杂。无论采用哪种方式，都不应通过 Docker Socket 自动扫描容器。

## 3. 插件容器

### 3.1 容器组成

每个插件容器包含：

```text
/plugin/
  runtime/          插件运行时和管理器
  current/          当前插件版本
  versions/         历史版本，用于回滚
  venv/             插件依赖
  state/            插件私有状态
```

容器内运行一个稳定的 **Plugin Supervisor**：

- 对外提供固定协议端口。
- 管理插件版本、venv、子进程和健康检查。
- 热加载时重启插件子进程，不重启容器。
- 一个容器只运行一个插件，因此不需要处理插件间依赖冲突。

插件代码可以在容器内运行在独立子进程中。这样做的目的不是跨插件隔离，而是：

- 热更新时干净地重启插件代码。
- 避免新代码影响正在执行的旧任务。
- 让 Supervisor 能在插件崩溃时恢复。

### 3.2 两种部署模式

**不可变镜像模式（生产推荐）**

- 插件代码和依赖打进镜像。
- 容器根文件系统只读。
- 更新插件 = 更新容器镜像。
- 安全性最好，版本可审计。

**运行时安装模式（开发/管理场景）**

- 基础插件镜像只包含 Supervisor 和 Python。
- 插件包和 venv 放在独立卷中。
- 通过管理接口上传插件包、运行时 `pip install`、热重载。
- 依赖安装只影响当前插件容器。
- 生产环境建议仍使用固定版本和镜像签名。

## 4. 插件 Manifest

每个插件至少包含一个 `plugin.json`：

```json
{
  "id": "acme",
  "name": "Acme Plugin",
  "version": "1.0.0",
  "protocol_version": 1,
  "entrypoint": "plugin:register",
  "capabilities": [
    "features",
    "captcha_methods",
    "browser_actions"
  ]
}
```

高信任插件示例：

```json
{
  "id": "acme-vault",
  "name": "Acme Vault",
  "version": "1.0.0",
  "protocol_version": 1,
  "entrypoint": "plugin:register",
  "capabilities": [
    "secret.credentials",
    "secret.totp"
  ]
}
```

要求：

- `id` 全局唯一。
- 功能 key 必须命名空间化：`plugin.<id>.<name>`。
- 验证码方法 key 必须命名空间化。
- `protocol_version` 不兼容时拒绝加载。
- 能力变更需要管理员确认。
- 插件不应自行指定任意 pip index URL。

## 5. 协议

内部统一使用 gRPC + protobuf。Proto 文件位于 `proto/siteflow_proto/plugin.proto`，生成代码随仓库提交，使用 `scripts/generate_plugin_proto.sh` 重新生成。

### 5.1 控制面

```proto
service PluginControl {
  rpc GetManifest(Empty) returns (Manifest);
  rpc ListFeatures(Empty) returns (FeatureList);
  rpc ListCaptchaMethods(Empty) returns (CaptchaMethodList);
  rpc ListSecretProviders(Empty) returns (SecretProviderList);
  rpc Health(Empty) returns (HealthStatus);
  rpc Reload(ReloadRequest) returns (ReloadResult);
}
```

控制面返回 JSON Schema 和公开元数据，不返回秘密。

### 5.2 执行面

```proto
service PluginExecution {
  rpc ExecuteStep(StepRequest) returns (StepResult);
  rpc RecognizeCaptcha(CaptchaRequest) returns (CaptchaResult);
}
```

`StepRequest` 包含插件功能 key、步骤配置、站点 origin、当前 URL、截止时间和 worker 浏览器代理地址。插件不接收密码、Cookie、TOTP 种子或主系统文件系统路径。

`StepResult` 返回 `status`、`message` 和 `data_json`，由 worker 转换为现有步骤结果。验证码识别返回文本、置信度、匹配余量和元数据。

### 5.3 浏览器动作代理

当前实现使用 worker 内部 HTTP 接口：

```text
POST http://worker:6300/plugin-broker
{"token":"step-token","op":"click","selectors":["#sign"]}
```

插件调用 worker 的动作代理时使用步骤级 token。当前实现绑定：

- 页面和步骤 key
- 允许的站点 origin
- 过期时间
- 允许的动作集合

worker 只接受受控动作：`goto`、`click`、`fill`、`read_text`、`wait_for`、`screenshot`、`query_selector`。`goto` 只能访问当前站点 origin。`click` 会自动跟随同源 `target="_blank"` 新标签页。插件不能执行任意 JavaScript、读取 Cookie 或直接使用 CDP。

### 5.4 gRPC 协议

控制面使用 `siteflow_proto.PluginControl` 服务，proto 定义：

```proto
service PluginControl {
  rpc Health(Empty) returns (HealthStatus);
  rpc GetManifest(Empty) returns (Manifest);
  rpc ListFeatures(Empty) returns (FeatureList);
  rpc ListCaptchaMethods(Empty) returns (CaptchaMethodList);
  rpc Reload(ReloadRequest) returns (ReloadResult);
}
```

约定：

- 插件端点是 `host:port`，例如 `plugin-acme:7000`；也接受 `grpc://host:port`，存储时统一为 `host:port`。
- API 调用插件时在 gRPC metadata 中携带 `x-plugin-token`，插件端必须校验同一个 token。
- 暂不使用 mTLS；生产环境通过内部网络、独立 token 和后续 mTLS 增强。
- Docker healthcheck 也通过 `Health` RPC 检查，不使用 HTTP 健康端点。
- 控制面已实现 Health、GetManifest、ListFeatures、ListCaptchaMethods；执行面已实现 ExecuteStep 和 RecognizeCaptcha；浏览器动作通过 worker 代理。

注册和心跳仍然使用 API 内部 HTTP/JSON 接口。注册不是插件数据面协议，保留 REST 便于 API、管理 UI 和插件生命周期统一处理：

```text
POST /api/v1/plugin-runtime/register
POST /api/v1/plugin-runtime/heartbeat
POST /api/v1/plugin-runtime/unregister
GET  /api/v1/plugin-runtime/plugins
GET  /api/v1/plugin-runtime/plugins/{plugin_id}
```

`/api/v1/plugin-runtime/register|heartbeat|unregister` 只允许内部网络访问；通过 Web 反向代理访问时返回 404。管理员查询接口仍可通过正常登录访问。

插件容器中的 `Plugin Supervisor` 负责：

- 加载 `/plugin/current/plugin.json`
- 启动插件 gRPC 服务
- 启动后向 `SITEFLOW_CONTROL_URL` 注册
- 按 `SITEFLOW_PLUGIN_HEARTBEAT_SECONDS` 发送心跳
- 收到 404/409 或网络恢复后重新注册
- 退出时注销当前 generation

当前已实现注册、健康检查、manifest、功能/验证码元数据读取与目录注入、远程步骤执行、验证码识别和浏览器动作代理。

## 6. 功能步骤扩展

插件在控制面返回：

```json
{
  "key": "plugin.acme.checkin",
  "name": "Acme 签到",
  "category": "sign",
  "cardinality": "multiple",
  "version": 1,
  "description": "通过 Acme 页面完成签到。",
  "config_schema": {
    "type": "object",
    "properties": {
      "url": {"type": "string", "default": "{base_url}"},
      "button_selector": {
        "type": "array",
        "items": {"type": "string"},
        "default": ["button[type='submit']"]
      }
    }
  },
  "required": [],
  "requires": []
}
```

主系统将其适配为内部 Feature 定义：

- 出现在 `GET /api/v1/features`。
- 出现在功能管理。
- 出现在模板和站点步骤选择中。
- 使用现有 JSON Schema 表单渲染。
- 支持现有的默认值、必填字段、错误策略、超时和工作流顺序。

插件步骤执行时，worker 发送：

```json
{
  "execution_id": "opaque-token",
  "plugin_id": "acme",
  "feature_key": "plugin.acme.checkin",
  "config": {
    "url": "https://pt.example.com/attendance.php",
    "button_selector": ["#sign"]
  },
  "site_origin": "https://pt.example.com",
  "current_url": "https://pt.example.com/index.php",
  "deadline_ms": 30000,
  "broker": {
    "url": "https://worker:6310/plugin-broker",
    "token": "step-scoped-token"
  }
}
```

不发送：

- 用户名、密码、Cookie、TOTP 种子
- 主系统数据库连接
- 主系统环境变量
- 文件系统路径
- Docker socket
- 原始 Playwright/CDP 控制权

## 7. 验证码方法扩展

插件注册：

```json
{
  "key": "plugin.acme.gd5",
  "label": "Acme GD5",
  "description": "稳定识别 Acme GD 5 验证码。",
  "supports_vision": false
}
```

主系统做两件事：

1. 将方法动态注入图像验证码步骤的 `captcha_method` enum。
2. worker 调用插件的 `RecognizeCaptcha`。

请求：

```json
{
  "method_key": "plugin.acme.gd5",
  "image_base64": "...",
  "charset": "alnum",
  "length": 6
}
```

响应：

```json
{
  "text": "AB12CD",
  "confidence": null,
  "margin": 12,
  "meta": {}
}
```

主系统继续统一处理：

- 字符集和长度校验
- 刷新验证码
- 重启任务
- 截图 artifact
- 日志和运行结果

验证码插件不需要浏览器，也不需要站点登录态，是风险最低的一类插件。

## 8. 浏览器动作代理

插件不能拿到原始 Page 对象，只能通过 worker 动作代理执行受限操作。

建议第一版支持：

- `goto`
- `click`
- `fill`
- `read_text`
- `wait_for`
- `screenshot`
- `query_selector`
- `fill_secret`

请求示例：

```json
{
  "token": "step-scoped-token",
  "op": "goto",
  "url": "https://pt.example.com/attendance.php"
}
```

敏感填写：

```json
{
  "token": "step-scoped-token",
  "op": "fill_secret",
  "selector": "#password",
  "secret_ref": "site.password"
}
```

worker 自行解析秘密并填写，只返回成功状态。插件不接触明文。

默认禁止：

- 任意 JavaScript `evaluate`
- 原始 CDP/WebSocket
- 浏览器 Cookie 读取
- 任意跨域导航
- 任意文件上传/下载
- 访问主系统文件系统

worker 应按插件声明的允许域名限制导航目标，并记录动作审计日志。

## 9. TOTP 和密码提供插件

TOTP/密码提供插件属于高信任插件，需要单独能力声明和审计。

### 9.1 TOTP Provider

接口：

```text
GenerateTotp(provider_id, site_origin, service_hint)
    → { code, expires_at }
```

设计要点：

- TOTP 种子保存在插件自己的加密存储中。
- 主系统只保存 provider id 和账号匹配提示。
- 主系统不接收 TOTP 种子。
- worker 只接收一次性 OTP。
- `challenge.2fa` 步骤保持 provider 无关。
- 现有 2FAuth 可作为内置 provider 实现。

### 9.2 密码提供 Provider

接口：

```text
ResolveCredential(provider_id, site_origin, match_mode, custom_match)
    → { lease_id, username, password, expires_at }
```

设计要点：

- 主系统数据库不保存密码。
- API 不把密码写进 job payload。
- worker 仅在 `auth.form_login` 步骤请求凭据。
- 返回一次性 lease，有短 TTL。
- 使用后立即失效。
- 不记录请求体、密码或 lease 内容。
- 插件容器拥有自己的加密存储和解锁状态。
- 现有 Bitwarden 可作为内置 provider 实现。

### 9.3 Provider 选择

站点通过 `credential_provider_id` 选择凭据提供商；2FA 步骤通过 `totp_provider_id` 选择 TOTP 提供商。

Bitwarden 使用以下匹配配置：

- `bitwarden_enabled`
- `bitwarden_match_mode`
- `bitwarden_custom_match`

这些字段仍属于当前站点 API 和导入/导出格式，不在启动时转换或清理。

## 10. 秘密边界

必须把插件分为两个信任等级：

### 普通插件

- 功能步骤
- 验证码方法
- 浏览器动作
- 不接收密码、Cookie、TOTP 种子

### 秘密提供插件

- 可以访问自己持有的密码或 TOTP 种子
- 通过一次性 lease 提供最小必要数据
- 只允许 worker 在授权步骤调用
- 不向普通插件开放接口
- 有独立容器、独立存储、独立网络策略

“一个插件一个容器”保证普通插件无法读取秘密插件容器的数据。
但秘密插件本身仍然是受信任代码，容器不能防止插件主动泄露自己拥有的秘密。

## 11. 主系统改造点

### 后端

- `backend/app/features/catalog.py`
  - 从静态 `FEATURE_LIST` 扩展为内置功能 + 远程插件功能。
- `backend/app/features/base.py`
  - 增加可适配远程 JSON Schema 的功能定义接口。
- `backend/app/services/feature_settings.py`
  - 当前依赖 Pydantic 模型字段；需要抽象为统一的 schema/defaults/validation 接口。
- `backend/app/features/captcha.py`
  - 改为运行时验证码方法注册表。
- `backend/app/api/v1/features.py`
  - 返回插件功能，并标记来源插件。
- 新增 `backend/app/services/plugins.py`
  - 插件注册、心跳、endpoint 校验、manifest、健康状态和元数据缓存。
- 新增 `backend/app/api/v1/plugin_runtime.py`
  - `register`、`heartbeat`、`unregister`、插件状态和重载接口。
- `backend/app/services/executor.py`
  - 不再把 Bitwarden/2FAuth token 放进普通 job payload。
  - 传递 provider id、站点安全上下文和 broker token。
- 新增数据库或 AppSetting 配置：
  - 最后一次注册的插件 endpoint 和 generation
  - provider 选择
  - 插件能力审批状态

### Worker

- `worker/executor.py`
  - 将硬编码步骤分发改为“内置处理器 + 远程插件处理器”。
  - 验证码识别改为方法注册表。
  - 增加 browser broker。
  - `auth.form_login` 支持 Credential Provider lease。
  - `challenge.2fa` 支持 TOTP Provider。
- 新增 `worker/plugin_broker.py`
  - 受限浏览器动作协议。
- 新增 `worker/plugin_protocol.py`
  - 与插件容器通信。
- 凭据、OTP 使用后立即清除，不写日志和 artifact。

### 前端

前端大部分可以保持不变：

- `/features` 自动带来插件功能。
- JSON Schema 自动渲染配置。
- 验证码方法 enum 自动出现。

需要新增：

- 插件状态和版本展示。
- Credential Provider / TOTP Provider 选择。
- 插件安装、重载、回滚管理界面（管理员）。
- 插件来源标签和可用性提示。

## 12. 热加载和运行时依赖安装

插件容器内部由 Supervisor 管理：

1. 上传或更新插件版本到 staging。
2. 校验 manifest 和协议版本。
3. 创建或更新插件 venv。
4. 安装依赖。
5. 启动新版本子进程。
6. 健康检查。
7. 新请求切换到新 generation。
8. 旧进程等待正在执行的任务完成。
9. 失败回滚旧版本。

要求：

- 安装和重载只影响当前插件容器。
- 正在运行的任务固定使用开始时的插件版本。
- 插件配置 Schema 不兼容时必须提升功能版本或提供迁移。
- 安装失败不能破坏当前可用版本。
- 生产环境建议固定版本、镜像签名和人工审批。

## 13. 资源与存储控制

每个插件容器独立限制：

```yaml
cpus: "0.50"
mem_limit: "512m"
pids_limit: 256
read_only: true
tmpfs:
  - /tmp:size=64m
```

存储：

- 每个插件独立 volume。
- 运行时安装的插件包、venv、状态都放在该 volume。
- 限制最大插件包大小、venv 大小和保留版本数。
- 自动清理旧版本和 pip 缓存。
- 生产环境建议使用宿主机文件系统配额或支持 quota 的存储驱动；Docker volume 本身通常不能直接限制目录大小。

每个插件还需要：

- 最大并发执行数
- 步骤超时
- 验证码请求超时
- 安装超时
- 协议消息大小限制
- 插件异常熔断和健康检查

## 14. 部署方式

推荐每个插件一个 Compose service：

```yaml
services:
  plugin-acme:
    image: ghcr.io/example/siteflow-plugin-acme@sha256:...
    read_only: true
    networks:
      - siteflow_internal
    volumes:
      - plugin_acme_data:/data
    environment:
      SITEFLOW_CONTROL_URL: http://api:8000
      SITEFLOW_PLUGIN_ID: acme
      SITEFLOW_PLUGIN_ENDPOINT: plugin-acme:7000
      SITEFLOW_PLUGIN_TOKEN_FILE: /run/secrets/plugin-acme-token
    secrets:
      - plugin-acme-token
    cpus: "0.50"
    mem_limit: "512m"

  plugin-vault:
    image: ghcr.io/example/siteflow-plugin-vault@sha256:...
    read_only: true
    networks:
      - siteflow_internal
    volumes:
      - plugin_vault_data:/data
    environment:
      SITEFLOW_CONTROL_URL: http://api:8000
      SITEFLOW_PLUGIN_ID: vault
      SITEFLOW_PLUGIN_ENDPOINT: plugin-vault:7000
      SITEFLOW_PLUGIN_TOKEN_FILE: /run/secrets/plugin-vault-token
    secrets:
      - plugin-vault-token
    cpus: "0.25"
    mem_limit: "256m"
```

新增插件容器后，插件会主动注册，主系统自动刷新功能目录，不需要手动维护 endpoint 列表，也不需要重启 API/worker 进程。主系统内部只缓存已注册插件的 endpoint 和 generation。

构建并运行示例插件：

```bash
./scripts/generate_plugin_proto.sh
docker compose -f ops/docker-compose.yml build plugin-demo demo-site
docker compose -f ops/docker-compose.yml up -d plugin-demo demo-site
```

示例插件会自动注册，首次状态为“待审批”。登录 SiteFlow 后进入“插件管理”，检查通过后勾选权限并点击“通过审批”，状态才会变为“正常”。

`SITEFLOW_PLUGIN_TOKENS` 示例：

```text
SITEFLOW_PLUGIN_TOKENS='{"demo":"replace-with-a-random-token"}'
SITEFLOW_PLUGIN_DEMO_TOKEN=replace-with-a-random-token
```

生产环境应覆盖 compose 的 demo 默认 token，并优先使用 Docker secret 或只读文件。插件容器不要写入镜像或提交真实 token。

## 14.1 插件注册安全

注册接口需要额外防护：

- 只监听内部网络，不暴露到公网。
- 每插件独立 token，token 与 plugin id 绑定。
- 注册请求限流，防止暴力注册和资源耗尽。
- endpoint 只允许内部 DNS 或白名单网段，并校验 DNS 解析结果，防止 SSRF。
- 功能 key 必须符合 `plugin.<id>.` 前缀。
- 所有插件首次注册必须经过管理员审批；高信任能力必须单独确认。
- 记录注册、心跳、注销、版本和 generation 变化。
- 不接受插件自行扩大能力范围；能力变更触发重新审批。

## 15. 安全审计

需要记录：

- 插件注册、心跳超时、注销和重新注册
- 插件 ID、endpoint、协议版本、generation
- 插件加载、卸载、重载、版本变化
- 能力声明和审批结果
- 插件健康状态
- 步骤执行开始/结束
- 浏览器动作类型、目标 origin、结果
- 秘密请求的 provider、站点、步骤和结果
- 安装依赖、安装来源和版本

不记录：

- 密码、Cookie、TOTP 种子
- 完整 OTP
- Credential lease 内容
- 主系统密钥
- 插件认证 token

## 16. 实施阶段

### 阶段 1：协议和基础运行时

- Plugin Supervisor 容器
- 插件主动注册、心跳、注销
- manifest、健康检查、版本管理
- 元数据控制协议
- 注册 token 和 endpoint 白名单校验
- 内置插件兼容层

当前仓库已落地：

- 后端注册表、心跳 TTL、generation、注销和查询接口。
- 每插件独立 token、endpoint 白名单、DNS 解析地址校验、SSRF 防护和协议版本校验。
- 所有插件首次注册后标记为 `unapproved`，保存检查结果并等待管理员审批。
- 支持重新检查、部分审批、完整审批和能力变更后重新审批。
- 支持停止/启用插件：停止后连接和心跳继续，但不接入系统；重新注册不会自动解除停止状态。
- 前端新增“插件管理”菜单，展示检查项、权限、审批人和插件状态。
- `plugins/runtime` 最小 gRPC Supervisor，包含 Token 校验、健康检查、manifest、功能/验证码元数据读取和注册心跳循环。
- `plugins/examples/demo` 示例插件镜像，compose service `plugin-demo` 默认启动。测试站点、导入文件及说明见该目录的 `README.md`。
- 远程功能目录、验证码方法注入、远程步骤执行和浏览器动作代理已接入。

### 阶段 2：功能步骤和验证码

- 远程 Feature 定义
- 动态 JSON Schema 注入
- Captcha method 注册表
- worker 远程步骤执行
- 浏览器动作代理

当前已落地：

- 注册时通过 gRPC 拉取 `ListFeatures` 和 `ListCaptchaMethods`，保存到插件注册表。
- 功能 key 和验证码 key 必须使用 `plugin.<id>.` 命名空间。
- 只有已审批且未停止的插件才会注入功能目录。
- 远程 JSON Schema 直接进入功能管理和步骤配置表单，支持默认值、必填字段和字段校验。
- 插件验证码方法自动加入图形验证码识别方式枚举。
- 插件停止、禁用或取消审批后会从功能目录和验证码枚举中移除。
- worker 通过 gRPC 执行 `plugin.<id>.*` 步骤；插件通过步骤级 token 调用 worker 浏览器动作代理。
- 插件验证码方法通过 `RecognizeCaptcha` 远程识别。
- 浏览器代理只允许受控动作：goto、click、fill、read_text、wait_for、screenshot、query_selector；goto 限制在当前站点 origin。
- 示例插件提供 `plugin.demo.demo_checkin` 和 `plugin.demo.gd5`，演示按钮点击、成功文本读取及本地验证码识别。

### 阶段 3：秘密提供者

- TOTP Provider
- Credential Provider
- 通用 provider 选择
- Bitwarden/2FAuth 适配为内置 provider
- secret lease 和审计

当前已落地第一个内置 provider 增量（不是完整阶段 3）：

- API `SecretProviders` 统一处理 `builtin.manual`、`builtin.bitwarden` 和 `builtin.twofauth`，使用加密凭据、Bitwarden 启用/匹配字段、2FAuth 集成配置和步骤 `service_hint`。
- 普通 worker job payload 不包含用户名、密码、Bitwarden API token 或 2FAuth API token；包含当前任务所需的 Cookie、CFbox/vision 运行配置。
- 每次 job attempt 生成独立 256-bit 随机 worker capability，绑定 job/site/attempt、已启用的认证步骤、purpose 和 provider；不复用浏览器 broker token，也不使用新增全局共享 provider token。
- Worker 仅在 `auth.form_login` / `challenge.2fa` 中通过内部 `POST /api/v1/internal/secrets/resolve` 申请 lease，再兑换凭据或 OTP；普通插件协议不包含 worker capability 或 lease。填写前检查页面仍处于站点 origin，不允许跨源 SSO 自动填写。
- Lease 为 256-bit 随机不透明值，30 秒有效，绑定 job/site/step/purpose/provider。在锁内原子消费，先消费再调用 provider，失败也不能复用。同一 scope 再申请会替换尚未使用的 lease。worker capability 在执行超时后失效。
- capability/lease 只在 API 单进程内存中保存 SHA-256 摘要和非秘密作用域，不持久化、不缓存明文。当前要求单 API 进程；重启 fail closed，不能横向多进程共享 lease。
- 任务结束、重试前、取消和 API shutdown 撤销 capability/lease；兑换前检查数据库取消/状态/attempt，provider 返回后再次检查。provider 调用已发出时取消不能撤回该外部请求，但不再向 worker 返回秘密。
- 接口不使用浏览器 session/CSRF 认证，只接受该 attempt 的 Bearer capability，响应 `Cache-Control: no-store`；Caddy 对秘密内部路径返回 404。内部 Docker 网络仍是受信任边界，尚未实现 mTLS。
- 审计记录 job/site/step/provider 和成功/失败，不记录 token、lease、用户名、密码、OTP 或 provider 原始错误响应。敏感填写异常不会将 Playwright 的填充值调用日志写入结果。

后续增量已实现远程秘密提供者、动态提供者选择、独立高信任能力审批和持久化审计查询，见文末。尚未实现分布式 lease 存储和插件 `fill_secret`。

### 阶段 4：管理和生产化

- 插件管理 UI
- 安装、热加载、回滚
- 插件状态和告警
- 资源配额和存储清理
- 签名、固定版本和供应链校验
- 可选持久反向控制通道

## 17. 决策摘要

- 一个插件一个容器。
- 插件容器启动后主动向主系统注册并发送心跳，主系统自动发现。
- 不通过 Docker Socket 扫描容器，不依赖 mDNS/广播发现。
- 注册使用每插件独立 token，endpoint 必须经过内部网络白名单校验。
- 普通功能/验证码插件与秘密提供插件分离信任等级。
- 主系统不加载插件代码，只通过协议调用。
- 插件功能自动进入模板和站点配置。
- 验证码方法自动进入步骤选项。
- 浏览器通过 worker 动作代理，插件不接触 Cookie 和原始浏览器控制权。
- 密码/TOTP 通过一次性 lease 提供，不进入普通任务 payload。
- 插件容器内支持热加载和运行时 pip，但每个插件独立卷、独立依赖、独立资源限制。
- 秘密提供插件可以使用同一套容器机制，但生产环境建议不可变镜像、固定版本和人工审批。
- 控制面和执行面统一使用 gRPC；注册/心跳保留 API HTTP/JSON 接口。
- 第一版使用“主动注册 + gRPC 回调”；持久反向控制通道作为后续增强。
- 远程步骤如下发到 worker，插件通过步骤级 token 调用 worker 浏览器动作代理。
# Remote Secret Providers

Sites select `credential_provider_id` (`builtin.manual`, `builtin.bitwarden`, or
`plugin.<id>`). When null, `bitwarden_enabled` selects Bitwarden or manual credentials. Import/export
retains this field without exporting actual credentials. Existing `challenge.2fa`
steps select `totp_provider_id` (2FA 提供商), defaulting to `builtin.twofauth`;
every provider requires `service_hint` (2FA 服务名（必须）). Optional
`username_hint` (2FA 用户名（可选）) disambiguates 2FAuth accounts by their
`account` field and is also passed to plugin handlers. Global defaults provide the same hint fields.
Both step configuration and global 2FA defaults provide a dynamic provider selector.

Credential plugins appear as sibling tabs of 账户 / Cookie / Bitwarden, not a
generic site-level provider dropdown. The same enable switch, full-domain,
root-domain and custom matching controls serve Bitwarden and plugins. Selection
is mutually exclusive; disabling a provider selects manual credentials, without
deleting them. An unavailable saved plugin remains visible and can be disabled.
The existing site PUT API persists the binding in `credential_provider_id` and
matching preferences in `bitwarden_match_mode` / `bitwarden_custom_match`; these
historically named fields are shared by external providers and survive import/export.
They contain preferences only, never the plugin's resolved username or password.

`GET /api/v1/secret-providers` is authenticated and lists built-ins plus available,
enabled, fully approved plugins with the corresponding `secret.credentials` or
`secret.totp` capability. It returns IDs/names/capabilities, never runtime tokens.
Capability changes clear previous approval and require explicit administrator review.

The backend invokes the shared `SecretProvider.ResolveCredential` or `GenerateTotp`
RPC only inside scoped one-use lease redemption. Endpoint and authentication come
from the plugin registration and backend configuration, not step config. RPCs have
a 20-second deadline. Approval and generation are rechecked after resolution.
Remote requests contain job/site/step/origin scope and saved matching preferences
(`match_mode`, `custom_match`) or TOTP hints (`service_hint`, `username_hint`),
snapshotted from the resolved workflow at job registration, not caller input; no worker capability or
lease is sent to plugins. Ordinary step execution receives neither resolved factors
nor secret capabilities. Browser-capable plugins are still trusted to interact with
the site and see rendered page content; secret isolation is not a browser sandbox.

The complete local fixture and verification command are documented in
`plugins/examples/demo/README.md`. The fixed demo factors are public test data.

## Permission Isolation and Revocation

Runtime payloads include only approved declared capabilities, exact registration
generation and a separate permission epoch. Browser broker registration requires
`browser_actions`; feature-only ExecuteStep receives empty broker URL/token.
Demo now declares this capability and requires explicit administrator reapproval.
Deployment does not automatically approve capabilities.

A separate random job/attempt-scoped plugin authorization token is retained only
as a digest in API memory, bound to plugin generation/permission epoch. It is not
a secret capability and is never sent to plugins. The broker stores an authorization
callback, not a long-lived plugin token. Caddy blocks the internal authorization API.
Checks cover job status, cancellation, attempt, enablement, approval and generation.
Single API process remains required; restart fails closed.

Stop and CSRF-protected `POST /plugin-runtime/plugins/{id}/revoke` remove catalog
access after commit. Revoke preserves metadata and clears approvals. Both permanently
invalidate active grants, including after enable/reapproval, without changing heartbeat
generation or disconnecting plugins. New step/captcha dispatches and every browser
action check backend authority. Active steps/actions poll every 0.5 seconds with a
2-second HTTP timeout: nominal detection is at most 2.5 seconds plus scheduling,
not a hard real-time guarantee. gRPC cancellation follows within a further 0.1-second
check interval. Remote code may ignore cancellation but loses browser authority.
Already authorized actions can have irreversible effects; cancellation cannot undo
a click/navigation. In-flight captcha RPCs are not cancelled but results are checked
again and denied after revocation. Unrelated jobs and heartbeats remain running.

Secret leases check approval and snapshotted generation/permission epoch at issue,
consumption and after resolution. Outstanding leases become unusable on stop/revoke
or metadata change through lazy rejection; digest entries expire within 30 seconds.
Dispatched external requests cannot be recalled. Secrets authorized by the final
backend check before stop may already be in transit and cannot be recalled. Ordinary
plugins receive neither leases nor the worker secret capability.

`plugin_audit_events` is created through idempotent table creation. New registrations
start with permission epoch 1 and require approval. Latest 10,000 events are
retained, pruning on append. Persistent events cover registration, approval, revoke,
stop, enable, lease issue/consume/reject and provider resolution success/failure.
Only controlled event/result/reason codes, trusted scope IDs and admin actor are
stored. Invalid caller scope is not echoed in rejection events. No credentials,
OTP, tokens, leases, configuration, provider bodies or exception text is persisted.
Browser action types remain worker task logs, not persistent security audit events.
Heartbeats, unregister, failed registration and health checks are not persisted here.

Authenticated `GET /api/v1/plugin-runtime/audit` supports `page`, `page_size` (1..100),
`plugin_id`, `event`, `result`, newest-first items and total count. Plugin management
provides these filters and pagination in its security-audit collapse.
