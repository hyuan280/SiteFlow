# 架构

SiteFlow 是面向站点自动化任务的管理系统，第一个业务场景是 PT 站点签到。设计目标：前后端分离、配置入库、功能块可编排、浏览器任务可观测、单机低依赖（SQLite）。

## 1. 组件

```text
Browser
  │ HTTP / WebSocket :8088
  ▼
web (Caddy)
  ├── /                         Vue 3 SPA
  └── /api/*                    reverse proxy
        │
        ▼
api (FastAPI)
  ├── 单管理员认证 / CSRF
  ├── 代理 / 2FAuth / CFbox 集成配置
  ├── 站点登录凭据（本地加密）
  ├── 车道状态 / VNC WebSocket 代理
  ├── 运行队列 / 站点任务 / 步骤结果
  └── SQLite (WAL)
        │ internal network
        ▼
worker (FastAPI + Playwright CDP client)
  ├── 4 lanes: general / interaction / proxy_general / proxy_interaction
  ├── BrowserBackend / BrowserSession / optional Desktop
  ├── mss / xdotool clients through dedicated X11 socket volume
  └── lease / viewer / idle lifecycle
        │ HTTP / CDP / VNC
        ▼
browser (unmodified official CloakBrowser Manager, keyless)
  └── persistent profiles + browser + Xvnc / KasmVNC + fonts
```

- 只有 `web` 发布宿主机端口 `8088`。
- `api:8000`、`worker:6300`、`browser:8080` 仅存在于 Compose 内部网络，KasmVNC 仅在 browser 容器内部。
- 不使用 host 网络。
- 前端在宿主机预构建，Docker 只把 `frontend/dist` 复制到 Caddy；运行和构建容器都不依赖 npm registry。

## 2. 关键约束

1. **单实例 worker**：车道 profile 映射及租约不允许并行 worker 抢占；浏览器进程由外部后端所有。
2. **SQLite 单写入者**：API 与 worker 不共享数据库；需要任务结果时通过 API 或独立 DB 层写入。
3. **按需启动**：车道默认停止；前端或任务 lease 才启动，空闲后自动关闭。
4. **任务 lease 优先**：任务运行期间禁止关闭车道；release 后重新开始空闲计时。
5. **VNC 鉴权边界**：API 校验管理员会话和 Origin，Worker 使用独立令牌连接 Manager。Manager 处理 Kasm 协议适配，SiteFlow 不重复改写。
6. **配置优先级**：环境变量 > SQLite > 默认值；密钥环境变量不写库，库内值使用 AES-256-GCM。
7. **不做自动禁用/自动根因判断**：站点失败由人或后续任务策略处理。

## 3. 当前实现状态

已实现：

- 单管理员登录、会话、CSRF、登录限速。
- 代理 / 2FAuth / CFbox 配置：env 优先、DB 加密、掩码、连通性测试。
- 站点登录凭据本地 AES-256-GCM 加密；独立 API 管理，站点详情只返回掩码元数据。
- 四车道生命周期、按需启动/关闭、lease/release、VNC viewer 计数、空闲自动关闭。
- API → Worker → Manager VNC WebSocket 代理、noVNC 前端。
- 站点 / 模板 / 功能块模型：功能目录、模板继承、站点覆盖、配置 Schema 校验、解析预览。
- 单页管理界面：数据面板、车道状态、功能管理、模板管理、站点管理、系统设置。
- 功能 Schema 驱动字段表单：默认值、必填校验、数组增删、字段级继承/覆盖。
- 自定义页面信号与步骤执行条件；信号无法判断时升级人工接管。
- CFbox 截图识别集成：外部服务返回 CF 复选框坐标，供 xdotool 点击。
- 执行器基础版：运行记录、站点任务、步骤结果、条件/信号求值、取消与超时。
- Artifact：每个任务写详细执行日志，失败步骤保存截图；共享卷存储，数据库索引，运行详情查看详细日志。
- 车道并发、运行/任务队列限流、可配置 cron/间隔/随机时间范围调度器；Turnstile 使用 Playwright 点击并带 X11/xdotool fallback。
- 运行分析：状态矩阵、失败分析、趋势；基于原始运行数据实时聚合。
- Docker Compose 部署与无 host 网络运行。

- 工作流分组与排序、元素拾取器、通知、artifact 自动保留期清理。
- 插件运行时、能力审批、凭据提供商和安全审计。

首次安装通过 SQLAlchemy `create_all` 创建当前模型的表，管理员使用环境变量或安全的首次登录引导。功能和调度设置使用当前代码默认值；启动不执行历史数据库升级、数据回填或工作流清洗。

## 4. 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Vue 3 + TypeScript + Vite + Element Plus + Pinia + noVNC |
| 后端 | Python 3.12、FastAPI、SQLAlchemy 2、Pydantic v2、Argon2id |
| 数据库 | SQLite（WAL、单写入者） |
| worker | Playwright CDP、OCR、浏览器后端适配、mss/xdotool 客户端，无浏览器二进制 |
| browser | 官方 Manager，固定 digest、keyless Chromium 146、上游字体/显示服务 |
| 部署 | Docker Compose、Caddy、命名卷 |

## 5. 相关文档

- 功能块模型：`docs/feature-model.md`
- 数据模型：`docs/data-model.md`
- 配置：`docs/configuration.md`
- 认证：`docs/authentication.md`
- 车道与 VNC：`docs/vnc.md`
- 部署：`docs/deployment.md`
- 验证结论：`docs/verification.md`
- 使用指南：`docs/usage.md`；后续方向见 README 的“规划”。
