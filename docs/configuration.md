# 集成配置规范：代理、2FAuth、CFbox 与 Bitwarden

范围：代理、2FAuth、CFbox 与 Bitwarden 的解析优先级、加密存储、API 与前端行为。

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

环境变量使用 `SITEFLOW_*` 前缀。

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
checkbox_center_pct: {x, y}
widget_box_pct
```

配置：

```dotenv
SITEFLOW_CFBOX_URL=http://cfbox:8000
SITEFLOW_CFBOX_API_KEY=          # 可选
```

- URL 必需，API Key 可选；环境变量优先于数据库。
- 页面「系统设置 → CFbox」可保存、测试、清除；API Key 只显示掩码。
- 测试通过 `POST /predict` 发送空图片：认证通过时返回 400 `empty_image` 视为连通。
- 未配置 CFbox 时，`challenge.turnstile` 功能块保存会返回 422。
- 后续 Compose 内置 CFbox 时仍保留外部 URL 配置，容器内可直接使用服务名。

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

## 4. 配置解析优先级

按字段独立解析：

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
