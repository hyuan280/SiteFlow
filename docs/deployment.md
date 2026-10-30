# 部署与运维

## 1. 前置条件

- Docker + Docker Compose。
- GHCR 镜像部署无需 Node.js；源码构建另需 Node.js 22/npm。
- 可选：可达的 SOCKS5/HTTP(S) 代理、2FAuth 服务、Bitwarden 自托管保险库。
- 图形验证码的 ddddocr 和 NexusPHP GD 5 模板识别已内置在 worker 镜像中，无需额外服务或配置；视觉模型方式需要配置大模型。
- 元素拾取器使用交互车道 + VNC；拾取会话默认 15 分钟过期，可用 `SITEFLOW_PICKER_TTL_SECONDS` 调整。
- 运行详情 SSE 依赖反向代理不缓冲：Caddy 已配置 `flush_interval -1`；换成其他代理时需关闭对应路径的响应缓冲。

## 2. 配置

```bash
cp .env.example .env
chmod 600 .env
openssl rand -base64 32   # 填入 SITEFLOW_MASTER_KEY
openssl rand -hex 32     # 填入 SITEFLOW_BROWSER_TOKEN（不是许可证密钥）
```

最少配置：

```dotenv
SITEFLOW_MASTER_KEY=<base64 32 bytes>
SITEFLOW_BROWSER_TOKEN=<random token>
```

可选：

```dotenv
SITEFLOW_PROXY_URL=socks5://user:pass@host:port
SITEFLOW_TWOFAUTH_URL=https://2fa.example.com
SITEFLOW_TWOFAUTH_TOKEN=CHANGE_ME
# Bitwarden 包装容器（compose 中的 bitwarden 服务）
BW_SERVER=https://vault.example.com
BW_CLIENTID=
BW_CLIENTSECRET=
BW_PASSWORD=
BW_API_TOKEN=
# SITEFLOW_ADMIN_USERNAME=admin
# SITEFLOW_ADMIN_PASSWORD=
SITEFLOW_COOKIE_SECURE=false
# SITEFLOW_LANE_IDLE_STOP_SECONDS=300
```

- 环境变量优先于数据库。
- `.env` 已被 `.gitignore` 排除，必须保持 600。
- 首次启动自动按当前模型创建数据库表并提供默认设置。v1 面向全新安装，不包含旧版本数据库升级或数据库移植工具；不要直接接入旧开发版本的数据库。

## 3. 构建与启动

根目录使用发布镜像，无需本地构建：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

镜像版本、GHCR 访问权限和组件标签发布见 [镜像发布](releases.md)。

源码构建与启动仍使用 `ops/`：

```bash
./ops/build.sh
docker compose -f ops/docker-compose.yml up -d
docker compose -f ops/docker-compose.yml ps
```

访问 `http://localhost:8088`。

`ops/build.sh` 在宿主机执行 `npm ci && npm run build`，再直接使用 `docker build` 构建并加载四个 SiteFlow 镜像。浏览器官方镜像由 Compose 直接拉取，不在项目内构建。默认使用标准构建网络；需要 host 构建网络时运行 `bash ops/build.sh --host`，脚本同时传入 `--network=host` 和 `--allow=network.host`，也不改变容器运行网络。需要支持 Buildx/BuildKit 的 Docker。Python 包默认使用清华 PyPI 源，npm 使用公共 registry。

## 4. 服务与端口

| 服务 | 宿主机端口 | 内部端口 | 说明 |
|------|------------|----------|------|
| web | 8088 | 80 | Caddy，SPA + `/api` 反代 |
| api | 无 | 8000 | FastAPI，SQLite |
| worker | 无 | 6300 | 工作流与浏览器后端客户端 |
| browser | 无 | 8080 | 官方 Manager，CDP/VNC 转发与浏览器 |
| cfbox | 无 | 8000 | `ghcr.io/hyuan280/cfbox`，截图识别与内置 ONNX 模型 |
| bitwarden | 无 | 8090 | 可选，`bw` CLI HTTP 包装服务；API 自动检测 |

- 不使用 host 网络。
- 只有 web 对宿主机暴露；cfbox、bitwarden、browser 仅在 compose 内部网络提供端口。
- KasmVNC 端口不要发布；VNC 通过 API WebSocket 代理访问。

## 5. 首次登录

- 未配置 `SITEFLOW_ADMIN_PASSWORD` 且数据库无用户时，API 启动日志打印随机 admin 密码。
- 每次重启会重新生成，直到在页面修改密码写入 SQLite。
- 查看：

```bash
docker compose -f ops/docker-compose.yml logs api | sed -n 's/^api-1  | password: //p' | tail -1
```

- 配置了 env 管理员时使用 env 凭据，页面禁止修改密码。
- 首次登录后建议立即修改密码或配置 env 管理员。

## 6. 数据与备份

- `siteflow_data`：SQLite、API 数据。
- `bitwarden_data`：Bitwarden CLI 登录状态与本地配置；保险库数据仍在远端 Bitwarden 服务。
- `worker_data`：浏览器 profile ID 映射及车道运行数据。
- `browser_data`：外部浏览器 profiles 和 Manager 数据，需与 worker 映射一起备份。
- `browser_x11`：仅供 browser/worker 的 X11 socket，不是持久化业务数据。
- `siteflow_artifacts`：任务日志和失败截图，API/worker 共享挂载到 `/app/artifacts`。
- 备份时需要同时备份 `.env` 中的 `SITEFLOW_MASTER_KEY`；主密钥丢失则数据库中的加密配置不可恢复。
- 如需保留日志和截图，同时备份 `siteflow_artifacts` 卷（实际卷名通常为 `siteflow_siteflow_artifacts`）。
- 日志/截图默认保留 30 天，清理任务每 6 小时运行；可在系统设置中查看统计并手动清理。
- 示例：

```bash
docker run --rm \
  -v siteflow_siteflow_data:/data:ro \
  -v "$PWD/backups:/backup" \
  alpine:3.20 tar czf /backup/siteflow-data-$(date +%F).tgz -C /data .
```

## 7. TLS 与生产

- 生产建议在 Caddy 前加 HTTPS 域名，或修改 Caddyfile 启用 TLS。
- HTTPS 下设置 `SITEFLOW_COOKIE_SECURE=true`。
- 限制 `docker logs` 访问权限，因为未改密前日志含 bootstrap 密码。

## 8. 常用操作

```bash
# 查看日志
docker compose -f ops/docker-compose.yml logs -f api
docker compose -f ops/docker-compose.yml logs -f worker

# 重启
docker compose -f ops/docker-compose.yml restart

# 停止但保留数据
docker compose -f ops/docker-compose.yml down

# 停止并删除数据（谨慎）
docker compose -f ops/docker-compose.yml down -v
```

## 9. 故障排查

- `worker` 不可达：检查 `api` 内 `SITEFLOW_WORKER_URL`、Compose 网络、worker 健康状态。
- 车道启动失败：查看 worker 与 browser 服务日志、令牌是否一致及 keyless 版本检查。
- VNC 黑屏：检查 `.vnc-screen` 容器、noVNC target 是否为 div、WebSocket 是否建立。
- 代理站点失败：确认 `SITEFLOW_PROXY_URL`；系统不会回退直连。
- 2FAuth 失败：确认 URL/Token，或查看 `docs/verification.md` 的绑定说明。
