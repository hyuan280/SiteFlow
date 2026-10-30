# 镜像发布

## 镜像与版本

根目录 `docker-compose.yml` 只拉取 GHCR 镜像；`ops/docker-compose.yml` 用于源码构建部署。
四个组件分别发布，版本互不绑定：

| Git 标签 | GHCR 镜像 |
| --- | --- |
| `api-v1.0` | `ghcr.io/hyuan280/siteflow-api:v1.0` |
| `worker-v1.0` | `ghcr.io/hyuan280/siteflow-worker:v1.0` |
| `web-v1.0` | `ghcr.io/hyuan280/siteflow-web:v1.0` |
| `bitwarden-v1.0` | `ghcr.io/hyuan280/siteflow-bitwarden:v1.0` |

支持 `v1.0`、`v1.0.1` 和 `v1.0-rc.1` 等版本，不包含组件前缀的镜像版本便于独立配置。
正式版本同时更新该组件的 `latest`；包含连字符的预发布版本只发布版本标签。
首次部署需要四个镜像均已发布，推送 main 不会自动发布镜像。
当前发布平台为 `linux/amd64`，不声明尚未验证的 ARM64 支持。

```bash
git tag api-v1.0
git push origin api-v1.0
```

Web 发布会先构建前端。Worker 不下载或打包浏览器、KasmVNC 服务端和字体，
仅构建执行器及远程后端客户端；构建层使用 GitHub Actions 缓存。
CI 使用官方 PyPI，宿主机构建仍默认使用清华 PyPI、阿里云 apt 源。

## GitHub 配置

- 工作流使用仓库的 `GITHUB_TOKEN`，需要 `contents: read` 和 `packages: write`，无需配置个人访问令牌。
- 确认仓库允许 GitHub Actions 运行。首次发布后检查 GHCR 包的访问权限；
  若需要匿名拉取，需在包设置中将可见性设为 Public。仓库公开不代表包必然公开。
- 私有包需在部署主机运行 `docker login ghcr.io`，使用有读取包权限的凭据。
- Fork 自动使用小写的仓库所有者命名镜像；部署时设置 `SITEFLOW_IMAGE_OWNER`。
- 也可在 Actions 手工运行工作流，选择组件与版本。应选定发布提交，避免覆盖已有正式版本。

Worker 不包含 CloakBrowser 二进制，因此不再要求原预装方案的
`CLOAKBROWSER_REDISTRIBUTION_ALLOWED` 变量。官方 Manager 镜像由用户在部署时直接拉取，
SiteFlow 不重新打包、托管或发布该镜像。其上游许可独立适用；MIT 不覆盖浏览器和字体。

## 使用发布镜像

将 `.env.example` 复制为 `.env`，设置加密主密钥、随机 `SITEFLOW_BROWSER_TOKEN`，并按需要固定各组件版本：

```dotenv
SITEFLOW_API_VERSION=v1.0
SITEFLOW_WORKER_VERSION=v1.0
SITEFLOW_WEB_VERSION=v1.0
SITEFLOW_BITWARDEN_VERSION=v1.0
```

```bash
docker compose pull
docker compose up -d
```

根目录部署不需要 Node.js、编译工具或浏览器资源缓存。只发布 Web 端口，内部浏览器、
API 和 Bitwarden 不对外暴露。Demo 插件及测试站继续使用 `ops/` 源码部署，不随正式镜像
部署默认启动。两份 Compose 使用相同项目名和数据卷，不能作为两个独立实例同时启动。

更换镜像前先确认无活动任务及人工浏览器会话，并备份数据库和加密主密钥。
首次发行面向当前数据库结构，不提供历史开发版本数据库升级保证。
