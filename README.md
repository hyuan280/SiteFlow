# SiteFlow

SiteFlow 是一个站点自动化管理工具，当前主要用于 **PT 站点签到**。通过可编排的工作流，将登录、验证、签到和补签串联起来，支持定时执行、浏览器人工接管和运行结果追踪。

支持的主要功能：

- **站点与工作流**：模板继承、站点覆盖、条件判断、步骤跳转、配置导入导出及元素拾取。
- **登录与验证**：账户、Cookie、Bitwarden 和插件凭据；2FAuth；Turnstile、ALTCHA、图形验证码及视觉模型。
- **签到与补签**：按钮操作、iframe 签到、UNIT3D Claim、NexusPHP 日历补签和补签卡购买。
- **自动执行**：手动运行、定时/间隔/随机调度、并发限制、取消与超时处理。
- **浏览器管理**：普通/代理、通用/交互四条车道，按需启动、空闲关闭，以及 noVNC 实时查看和人工操作。
- **结果与通知**：步骤进度、日志、失败截图、运行分析，以及邮件和 HTTP 通知。
- **插件扩展**：功能、验证码方法、凭据和 2FA 提供商接入，支持能力审批、停用和审计。

详细操作见 [使用指南](docs/usage.md)。验证码识别和浏览器自动化不能保证适用于所有站点，请仅用于你有权访问的账户和服务。

## 快速开始

需要 Docker 和 Docker Compose。根目录 `docker-compose.yml` 使用 GHCR 发布镜像，无需在服务器安装 Node.js 或构建工具；浏览器由 Compose 直接拉取官方 CloakBrowser Manager 镜像，以 keyless 模式运行。

```bash
git clone https://github.com/hyuan280/SiteFlow.git
cd SiteFlow
cp .env.example .env
chmod 600 .env

# 分别生成加密主密钥与浏览器后端访问令牌
openssl rand -base64 32
openssl rand -hex 32
```

将生成结果分别填入 `.env` 的 `SITEFLOW_MASTER_KEY` 和 `SITEFLOW_BROWSER_TOKEN`，不要使用示例中的 `CHANGE_ME`。按需设置各组件的镜像版本，再启动：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

打开 **http://localhost:8088**。修改访问端口可在 `.env` 设置 `SITEFLOW_WEB_PORT`。

首次部署前需确保所选版本已发布；私有 GHCR 镜像需要先登录。镜像版本、数据备份及 HTTPS 部署见 [部署与运维](docs/deployment.md) 和 [镜像发布](docs/releases.md)。

### 首次登录

未设置 `SITEFLOW_ADMIN_PASSWORD` 且尚未保存管理员密码时，API 每次启动会生成随机的 `admin` 密码。通过日志查看：

```bash
docker compose logs api
```

登录后通过右上角账户菜单修改密码；保存后，后续启动不再生成随机密码。

也可在 `.env` 中指定管理员账户：

```dotenv
SITEFLOW_ADMIN_USERNAME=admin
SITEFLOW_ADMIN_PASSWORD=<自行设置的强密码>
```

环境变量中的管理员凭据优先使用，且不能在页面修改。

### 配置与安全

- 代理、CFbox、2FAuth、Bitwarden、视觉模型等按需在「系统设置」配置；环境变量配置的字段在页面只读。
- `.env` 不提交到 Git，文件权限保持 `600`。不要公开主密钥、账户密码、Cookie、后端令牌和集成服务凭据。
- 单独备份 `SITEFLOW_MASTER_KEY`；丢失后无法恢复数据库中的加密配置。数据库与浏览器数据备份见部署文档。
- 使用 HTTPS 对外访问时设置 `SITEFLOW_COOKIE_SECURE=true`；本地 HTTP 使用 `false`。
- 只发布 Web 端口。不要公开 Worker、Manager、CDP、VNC 或共享 X11 socket。
- 初始密码可能出现在 API 日志中，需限制日志访问并及时改密。
- 各浏览器车道使用独立 profile，同车道任务使用独立标签页；更新服务前确认没有活动任务或人工浏览器会话。涉及远程后端接入的更新需同时更新 API 和 Worker。

完整配置参考 [.env.example](.env.example) 和 [集成配置](docs/configuration.md)。

## 开发和贡献

欢迎提交问题、使用反馈和 Pull Request。报告问题时请附上版本、相关步骤、预期结果及脱敏日志，不要附带真实凭据或未经处理的站点导出数据。

修改前可先阅读 [架构](docs/architecture.md)。提交功能或修复时，请补充对应测试和使用说明；浏览器相关改动优先在隔离 Demo 中验证，不使用正式站点执行购买、补签等有副作用的操作。

### 规划

以下为可能继续完善的方向，不代表已实现功能或交付承诺：

- 适配更多浏览器后端，以及不依赖共享 X11 的桌面操作方式。
- 更细的插件配置兼容性提示、变更历史和审计关联。
- 加强浏览器与插件故障诊断、隔离回归和运行可观测性。
- 按实际需求评估多 Worker、多 API 和共享授权存储。

当前以单机部署和 SQLite 为主，不提供历史开发版本数据库迁移、自动站点禁用或自动根因判断。

### 目录结构

```text
backend/               API、认证、站点配置、调度、运行记录与测试
worker/                工作流执行、浏览器后端适配、插件 broker
frontend/              Vue 3 管理界面与 noVNC
plugins/               插件运行时及可导入的 Demo
proto/                 插件 gRPC 协议与生成代码
ops/                   Dockerfile、源码部署 Compose、构建脚本
scripts/               协议代码生成等开发工具
docs/                  使用、配置、部署及开发文档
.github/workflows/     组件镜像发布工作流
docker-compose.yml     GHCR 镜像部署入口
```

### 源码构建与镜像发布

源码构建另需 Node.js 22+/npm，先按快速开始准备 `.env`：

```bash
bash ops/build.sh
docker compose -f ops/docker-compose.yml up -d
```

构建网络需要使用 host 时，运行 `bash ops/build.sh --host`。这只改变构建网络，不改变容器运行网络。根目录和 `ops/` Compose 使用相同项目名及数据卷，不要当作两个独立实例同时启动。

GitHub Actions 按组件标签独立发布，例如：

```bash
git tag api-v1.0
git push origin api-v1.0
```

上述标签仅发布 `ghcr.io/hyuan280/siteflow-api:v1.0` 和对应 `latest`；另外支持 `worker-v*`、`web-v*`、`bitwarden-v*`。预发布版本不更新 `latest`，推送 `main` 不会自动发布镜像。细节见 [镜像发布](docs/releases.md)。

### 常用命令

部署命令在项目根目录执行：

```bash
docker compose logs -f api worker browser  # 查看日志
docker compose ps                        # 查看服务状态
docker compose pull                      # 拉取发布镜像
docker compose up -d                      # 启动或应用更新
docker compose down                      # 停止，保留数据卷
```

开发检查使用项目虚拟环境及前端依赖：

```bash
.venv/bin/pytest -q backend/tests
.venv/bin/pytest -q plugins/runtime/tests
npm --prefix frontend run type-check
npm --prefix frontend run build
git diff --check
```

依赖准备见 [后端开发](backend/README.md)，隔离浏览器与故障测试见 [验证说明](docs/verification.md)。

### API

交互式 API 文档：**http://localhost:8088/api/docs**。

常用入口包括 `/api/v1/sites`、`/api/v1/templates`、`/api/v1/features` 和 `/api/v1/runs`。管理接口通常需要登录会话，修改请求还需 `X-CSRF-Token`；不要将内部 Worker 或插件控制接口直接暴露到公网。接口列表见 [后端说明](backend/README.md)。

## 文档

- [使用指南](docs/usage.md)：配置站点、编排流程、执行与排查。
- [部署与运维](docs/deployment.md)：Docker 部署、端口、数据与备份。
- [集成配置](docs/configuration.md)：代理、凭据提供商、CFbox 和视觉模型。
- [通知设置](docs/notifications.md)：邮件、HTTP 通知与内容模板。
- [管理员认证](docs/authentication.md)：首次登录、会话与安全设置。
- [车道与 noVNC](docs/vnc.md)：浏览器控制与人工操作。
- [镜像发布](docs/releases.md)：GHCR、版本标签和权限。
- [浏览器后端](docs/browser-backends.md)：远程后端配置与适配边界。
- [架构](docs/architecture.md)、[功能块模型](docs/feature-model.md)、[数据模型](docs/data-model.md)：开发参考。
- [任务执行](docs/execution.md)、[运行分析](docs/analytics.md)：执行行为与结果查询。
- [插件系统](docs/plugin-system.md)、[插件 Demo](plugins/examples/demo/README.md)：扩展接入与示例。
- [验证说明](docs/verification.md)、[故障回归](plugins/examples/demo/FAULTS.md)：自动化测试。

## License

SiteFlow 原始代码使用 [MIT License](LICENSE)。第三方依赖、官方浏览器镜像、二进制及字体仍适用各自许可，MIT 不授予它们的再分发权；详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
