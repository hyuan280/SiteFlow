# 管理员认证

SiteFlow 采用单管理员模式，凭据来源按以下优先级解析：

| 环境变量管理员 | 数据库用户 | 登录依据 | 页面修改密码 |
|----------------|------------|----------|--------------|
| 已配置 | 有/无 | `SITEFLOW_ADMIN_USERNAME` + `SITEFLOW_ADMIN_PASSWORD` | 禁止（403） |
| 未配置 | 有 | SQLite 中的 Argon2id 哈希 | 允许 |
| 未配置 | 无 | 每次启动随机生成 `admin` 密码 | 允许；修改后写入 SQLite |

## 1. 环境变量管理员

```dotenv
SITEFLOW_ADMIN_USERNAME=admin          # 可选，默认 admin
SITEFLOW_ADMIN_PASSWORD=CHANGE_ME      # 设置后 env 管理员生效
```

- 只有 `SITEFLOW_ADMIN_PASSWORD` 存在时 env 管理员生效。
- 凭据不写入 SQLite。
- 页面显示「密码由环境变量管理」，修改密码接口返回 403。
- 修改 `.env` 后需要重启容器；下次登录使用新值，已登录会话保留到过期。

## 2. 初始随机管理员

没有 env 管理员，且数据库中没有用户时：

- 每次启动生成 `admin` + 随机密码。
- 只打印到 API 启动日志，不写文件、不写数据库。
- 启动时删除旧的 bootstrap 会话，旧密码立即不能用于新会话。
- 在页面修改密码后创建数据库用户；下次启动不再生成随机密码。

查看当前初始密码：

```bash
docker compose -f ops/docker-compose.yml logs api | sed -n 's/^api-1  | password: //p' | tail -1
```

> 初始密码在容器日志中明文可见。首次登录后应立即修改密码，并限制日志访问权限。

## 3. 数据库管理员

修改密码后：

- 密码使用 Argon2id 保存到 `users.password_hash`。
- 修改密码会删除该用户全部旧会话，并创建立即生效的新会话。
- 后续启动从数据库读取密码，不再打印初始密码。

## 4. 会话与 CSRF

- 会话 Token：32 字节随机值，只存 SHA-256；Cookie `siteflow_session` 为 HttpOnly。
- CSRF：Cookie `siteflow_csrf`（非 HttpOnly）+ 请求头 `X-CSRF-Token`，服务端校验两者一致且匹配会话哈希。
- Cookie：`SameSite=Lax`、`Path=/`、`Secure` 由 `SITEFLOW_COOKIE_SECURE` 控制。
- 会话默认 7 天（`SITEFLOW_SESSION_TTL_HOURS=168`）。
- 登录失败限速：5 次/5 分钟（可配置），返回 429。
- 登出、修改密码、配置写操作均要求 CSRF。

| 变量 | 默认 | 作用 |
|------|------|------|
| `SITEFLOW_SESSION_TTL_HOURS` | `168` | 会话绝对有效期（小时）；168 = 7 天，过期后必须重新登录 |
| `SITEFLOW_LOGIN_MAX_ATTEMPTS` | `5` | 同一用户名+客户端 IP 在窗口内允许的失败次数；超过后返回 429 |
| `SITEFLOW_LOGIN_LOCKOUT_SECONDS` | `300` | 失败计数的滑动窗口，同时也是锁定时长；响应带 `Retry-After` |

## 4.1 Cookie Secure 的作用

`SITEFLOW_COOKIE_SECURE` 控制会话 Cookie 与 CSRF Cookie 是否带 `Secure` 属性：

- `true`：浏览器只通过 HTTPS 发送这两个 Cookie；
- `false`：HTTP/HTTPS 都会发送，仅适用于本地开发或纯内网 HTTP。

当前 Compose 入口是 `http://localhost:8088`，因此设置为 `false`。如果前面加了 HTTPS 反向代理，应改为 `true`。

它不能从 `SITEFLOW_MASTER_KEY` 生成，原因：

1. `Secure` 是浏览器传输策略，不是密钥，不需要熵；
2. 主密钥只用于 AES-256-GCM 加密数据库中的敏感配置；
3. 从主密钥派生会让 Cookie 行为随密钥轮换而变化，语义错误；
4. 更危险的是：在 HTTP 下误设为 `true` 会导致浏览器不发送 Cookie，表现为登录后立刻掉登录。

推荐保持显式布尔配置。如果希望自动判断，应根据实际请求协议（`HTTPS` 或可信代理的 `X-Forwarded-Proto`）决定，而不是根据主密钥。

## 5. API

```text
POST /api/v1/auth/login       {username, password}
GET  /api/v1/auth/me
POST /api/v1/auth/logout
POST /api/v1/auth/password    {current_password, new_password}
GET  /api/v1/health           公开
```

登录响应：

```json
{
  "user": {
    "username": "admin",
    "auth_source": "bootstrap",
    "can_change_password": true
  },
  "csrf_token": "<redacted>"
}
```

`auth_source`：

- `env`：环境变量管理员
- `bootstrap`：启动随机密码
- `db`：数据库密码

`can_change_password=false` 表示当前密码由环境变量管理。

## 6. 容器部署

```bash
# 宿主机构建 SPA，再构建运行时镜像；全程不使用 build host network
./ops/build.sh
docker compose -f ops/docker-compose.yml up -d
docker compose -f ops/docker-compose.yml logs -f api
```

访问 `http://localhost:8088`。

- 前端在宿主机执行 `npm ci && npm run build`，Docker 只把 `frontend/dist` 复制进 Caddy 镜像；容器构建不访问 npm registry。
- 只有 `web` 发布宿主机端口 `8088`；`api` 仅在 Compose 内部 `expose: 8000`，不对宿主机暴露。
- 不使用 host 网络；`api` 与 `web` 都运行在 `siteflow_default` bridge 网络。
- `worker` 同样不使用 host 网络：Xvnc、xdotool、ffmpeg 全部在 worker 容器内部执行，只通过内部网络向 `api` 提供状态和 KasmVNC WebSocket。
- 车道状态与 noVNC 见 `docs/vnc.md`。
- SQLite 使用命名卷 `siteflow_siteflow_data`。
- 本地 HTTP 使用 `SITEFLOW_COOKIE_SECURE=false`；生产 HTTPS 应设为 `true`。
- `.env` 不进入镜像，权限保持 `600`。

## 7. 测试覆盖

后端测试覆盖：

- env 管理员登录、保护接口、CSRF。
- env 管理员修改密码返回 403。
- 登录失败限速 429。
- 随机初始密码登录、修改密码写入数据库、重启使用数据库密码。
- 随机密码每次启动重新生成，旧密码失效。
- env 密码变更后下次登录生效。
- 集成配置接口未登录 401、缺少 CSRF 403。

前端冒烟验证：

- 登录页渲染。
- 使用初始密码登录成功。
- 登录后进入数据面板；顶部账户菜单可修改密码、退出登录。

## 站点 Cookie

- 站点凭据卡片支持粘贴 `Cookie:` 请求头（`name=value; ...`）或浏览器导出的 JSON Cookie。
- Cookie 使用与用户名/密码相同的 AES-GCM 主密钥加密；接口只返回是否配置、数量和更新时间。
- Cookie 不参与站点导出，不写入日志。
- 执行任务时 API 解密 Cookie 并随任务发送给 worker，worker 在创建任务标签页前调用 `context.add_cookies()`。
- 站点任务成功后，worker 导出当前站点 Cookie，API 加密覆盖保存；失败时保留原配置。
- 移除登录凭据不会删除 Cookie；清除 Cookie 不会删除用户名/密码。
