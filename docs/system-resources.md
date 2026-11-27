# 系统资源监控

## 功能前提

系统设置中的「显示系统资源占用」默认关闭，开启后在数据面板显示资源卡片。不挂载 Docker Socket 时仅采集 API 所在系统的操作系统可见资源（host），不能据此推断 Worker、浏览器或整个 SiteFlow 的用量。

**此部署配置需要配套支持 Docker Socket 采样的 API 镜像。** 只有 host 采样的旧 API 不会因为新增挂载就获得容器拆分能力；先将 `SITEFLOW_API_VERSION` 更新为确实包含该实现的已发布版本并拉取镜像。若 Web 仍是旧版，也需更新到支持资源拆分显示的版本。本文说明支持该功能版本的部署方式和显示口径，不代表镜像已经发布或完成运行验证；不需要改动 Worker。

## 安全边界

- **挂载 Docker Socket 属于高权限授权。** 对常规 rootful Docker daemon，访问 socket 通常等同于拥有宿主机 root 权限；API 被攻破可能导致宿主机被控制。rootless daemon 也会暴露该 daemon 及其所属用户可控制的资源。
- `:ro` 只是只读文件系统挂载，**不限制通过 socket 发出的 Docker API 写操作**，不是只读 Docker API 权限控制。
- 可选 override 只向 `api` 挂载 socket，不向 Worker、Web、browser 或其他服务挂载。不要公开 socket、无鉴权 Docker TCP API，也不要通过 Web 反向代理对外转发 Docker API。
- `com.docker.compose.project` label 只用于应用层筛选要统计的容器，**不是安全隔离或 daemon 授权边界**；持有 socket 的进程仍可能访问其他项目。
- 采样逻辑只读取 Docker API，不自动写入 Docker API，不启动、停止、重建或修改容器，无需在 API 镜像内安装 Docker CLI。这是应用行为约束，不是 socket 本身的权限限制。
- 此方案没有实现受限 Docker Socket 代理。若不能接受上述风险，请不要挂载，继续使用 host 指标或独立监控系统。

## 启用部署

在仓库根目录操作，先按[部署与运维](deployment.md)准备 `.env`。根目录 `docker-compose.yml` 使用发布镜像，默认没有任何 Docker Socket 挂载；`docker-compose.resources.yml` 不是自动加载的 override，必须显式指定。

1. 更新并拉取支持此功能的 API 镜像；如需更新旧 Web，也拉取对应版本。
2. 使用下面的双文件命令创建或更新服务，给 API 增加 socket 挂载。更新前等待活动任务和人工浏览器会话结束。
3. 登录后在系统设置中开启「显示系统资源占用」，再刷新数据面板。仅挂载而不开启开关不会显示卡片。

```bash
docker compose -f docker-compose.yml -f docker-compose.resources.yml pull api
docker compose -f docker-compose.yml -f docker-compose.resources.yml up -d
```

默认挂载为 `/var/run/docker.sock:/var/run/docker.sock:ro`。之后执行更新、重建服务等操作时继续带上这两个 `-f`，否则按主文件重建 API 会移除该挂载。仅 `restart` 不会加载新的挂载或环境变量。

### 环境变量与项目名

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SITEFLOW_DOCKER_SOCKET` | `/var/run/docker.sock` | 后端采样器使用的容器内 Unix socket 路径；不是宿主机源路径 |
| `SITEFLOW_DOCKER_PROJECT` | `siteflow` | 后端筛选 `com.docker.compose.project` label 的值 |
| `COMPOSE_PROJECT_NAME` | 主文件的 `name: siteflow` | Compose 实际项目名；根主文件显式映射 `SITEFLOW_DOCKER_PROJECT: '${COMPOSE_PROJECT_NAME:-siteflow}'` |
| `SITEFLOW_DOCKER_SOCKET_HOST` | `/var/run/docker.sock` | 仅供可选 override 插值的宿主机 socket 源路径 |

保留现有部署的实际项目名，否则可能创建另一套容器和空数据卷。主文件有 `name: siteflow`，所以未覆盖时不取仓库目录名，后端和 Compose 均使用 `siteflow`。自定义项目名请在 shell 或 `.env` 中显式设置 `COMPOSE_PROJECT_NAME`，不要只修改 `SITEFLOW_DOCKER_PROJECT`：主文件的 `environment` 映射优先于 `env_file` 中的同名值。

若手动使用 `-p`，必须显式设置同值的 `COMPOSE_PROJECT_NAME`，避免 CLI 项目名与监控筛选条件不一致；后续命令也保持相同设置。例如已有项目名为 `my-siteflow` 时：

```bash
COMPOSE_PROJECT_NAME=my-siteflow docker compose -p my-siteflow -f docker-compose.yml -f docker-compose.resources.yml up -d
```

这里刻意只使用单层 `${COMPOSE_PROJECT_NAME:-siteflow}`，不额外引入独立的项目筛选覆盖或嵌套插值。非 Compose 部署的后端默认仍是 `SITEFLOW_DOCKER_PROJECT=siteflow`，需由部署方按实际 label 设置。

rootless Docker 可在私有 `.env` 设置宿主机实际路径，例如 `SITEFLOW_DOCKER_SOCKET_HOST=/run/user/1000/docker.sock`；容器内目标仍是 `/var/run/docker.sock`，不必修改 `SITEFLOW_DOCKER_SOCKET`。先确认源路径确实是 socket，且 API 运行用户有连接权限；不要通过 `chmod 666` 或公开 TCP 端口解决权限问题。统计范围只包含该 daemon 可见、且 label 匹配的容器，不跨 daemon 或跨主机汇总。

`ops/docker-compose.yml` 的源码部署不在本次配置接入范围内，不要把根主文件和 `ops/` 主文件叠加使用。

## 指标口径

支持容器拆分的 UI 使用三色区分 **SiteFlow 子集 / 其他 host 用量（other host）/ 空闲（idle）**，不是把 SiteFlow 和 host 总量相加；SiteFlow 本来就是 host 已用量的一部分。

- **CPU**：统一按 host 总 CPU 容量归一到 `0-100%`，不是 Docker CLI 多核累计可超过 `100%` 的口径。例如 8 个逻辑 CPU 中持续占满 1 个核，约占 host 总容量的 `12.5%`。SiteFlow 为匹配项目 label 的容器用量之和；其他 host 用量为 host 已用减去 SiteFlow 子集，空闲为 host 未用部分。采样时间差可能产生偏差，不应将其解读为精确进程账单。
- **内存**：容器用量按 Docker stats 的缓存扣除口径，扣除可回收文件缓存，而不是直接把全部原始 usage 当作应用占用；缓存字段依赖 daemon/cgroup 版本。SiteFlow 子集求和后，百分比的分母是 **host 总内存**，不是容器 `memory limit`。其他 host 用量与空闲也使用同一 host 分母，不重复计算 SiteFlow。
- **不是配额监控**：以上均不表示容器 cgroup 的 CPU/内存配额使用率，也不表示 API 单进程或 Worker 单进程用量。API 容器所见 OS 视图在特殊隔离环境下可能不同于物理宿主机；Docker Desktop 下通常对应 Docker 所在虚拟机，不是桌面操作系统全部资源。

## 失效与撤销

容器采集失败时保留可用的主机指标，不把不可用显示为 `0`。常见原因见[资源监控故障排查](troubleshooting.md#通知未送达与资源监控不可用)。

停止显示只需关闭系统设置开关，但这**不会撤销 socket 权限**。要撤销访问，使用原项目名，仅按主文件重建 API（不是 `restart`）：

```bash
docker compose -f docker-compose.yml up -d --no-deps --force-recreate api
```

这会移除本 override 增加的 socket 挂载，保留数据卷；不要用 `down -v`。若还有其他自定义 override，也须确认其中没有重新挂载 socket。
