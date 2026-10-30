# 车道状态与 noVNC

## 1. 拓扑

```text
浏览器
  │  http://localhost:8088
  ▼
web (Caddy)
  ├── /              → Vue SPA
  └── /api/*         → api:8000
                         │
                         ├── /api/v1/lanes         车道状态
                         └── /api/v1/lanes/{key}/vnc
                                     │ WebSocket
                                     ▼
                               worker:6300/lanes/{key}/vnc
                                     │
                               browser:8080/api/profiles/{id}/vnc
                                     │
                               KasmVNC / browser (official Manager)
```

- 只有 `web` 发布宿主机端口 `8088`。
- `api:8000`、`worker:6300`、`browser:8080` 都只在 Compose 内部网络可见。
- 不使用 host 网络。
- Xvnc 和浏览器在外部 browser 容器；mss/xdotool 通过专用 X11 socket 卷操作显示。
- KasmVNC 仅绑定 browser 容器内部 loopback；Manager API 使用随机令牌。不要公开 browser 管理端口或 X11 socket。

## 2. 车道

| key | 名称 | 并发语义 | 代理 |
|-----|------|----------|------|
| `general` | 普通通用 | 并行 | 否 |
| `interaction` | 普通交互 | 串行 | 否 |
| `proxy_general` | 代理通用 | 并行 | 是 |
| `proxy_interaction` | 代理交互 | 串行 | 是 |

- `SITEFLOW_PROXY_URL` 未配置时，两条代理车道显示为「未启用」，不会启动。
- worker 启动时**不启动任何浏览器**，所有车道初始为 `stopped`。
- 前端点击「启动浏览器」或任务 lease 才通过后端启动专用 profile；显示号动态分配。
- 状态包含：`status`、`browser`、`display`、`vnc_available`、`in_use`、`can_start`、`can_stop`、`error`。

## 3. 状态 API

```http
GET /api/v1/lanes
```

需要管理员会话。示例：

```json
{
  "worker_connected": true,
  "lanes": [
    {
      "key": "general",
      "label": "普通通用",
      "enabled": true,
      "status": "running",
      "browser": "running",
      "display": ":120",
      "profile": "profiles/general",
      "vnc_available": true,
      "in_use": 0,
      "can_start": false,
      "can_stop": true,
      "error": null,
      "started_at": "..."
    }
  ],
  "error": null
}
```

worker 不可达时返回四条 `status=unavailable` 的车道，前端禁用「进入 VNC」。

## 3.1 生命周期控制

```http
POST /api/v1/lanes/{key}/start
POST /api/v1/lanes/{key}/stop
```

- `start` 幂等：已运行时直接返回；并发点击由 worker 内部 per-lane 锁串行化，不会重复启动。
- `stop` 幂等：已停止时直接返回。
- 任务运行中（`in_use > 0`）时 `stop` 返回 409，前端「关闭浏览器」按钮禁用。
- 前端按钮：
  - `can_start=true` → 启动浏览器
  - `running` 且 `can_stop=true` → 关闭浏览器
  - `in_use>0` → 显示「任务运行中（N），禁止关闭浏览器」，关闭按钮禁用
- 关闭车道会断开 CDP 并调用后端停止专用 profile；不会删除持久化 profile 或 SQLite 中的登录 Cookie。

worker 内部任务接口（不对外暴露）：

```http
POST http://worker:6300/lanes/{key}/lease     # 自动启动车道并占用
POST http://worker:6300/lanes/{key}/release   # 释放；不会自动关闭浏览器
```

任务执行器必须先 `lease`，任务结束后 `release`。release 后车道保持运行，便于人工检查或通过 VNC 接管；需要释放资源时由前端或后续空闲策略关闭。

## 3.2 空闲自动关闭

条件：车道 `running`、无任务 lease（`in_use=0`）、无前端 VNC 连接（`viewers=0`）。

- 默认空闲时间 **300 秒**。
- `0` 表示禁用自动关闭。
- lease、release、VNC 连接/断开、手动启动都会刷新空闲计时。
- VNC 连接期间不会自动关闭；断开后重新开始计时。
- 任务 lease 期间不会自动关闭。

配置来源优先级：

| 来源 | 优先级 | 前端 |
|------|--------|------|
| `SITEFLOW_LANE_IDLE_STOP_SECONDS` | 最高 | 只读 |
| SQLite `app_settings.lane_idle_stop_seconds` | 次之 | 可编辑 |
| 默认值 `300` | 最低 | 可编辑 |

API：

```http
GET /api/v1/lane-settings
PUT /api/v1/lane-settings
```

车道状态页下方有「车道设置」卡片，可设置空闲关闭时间、普通车道并行任务数和代理车道并行任务数。并行任务数限制 1–10；环境变量存在时对应输入禁用，并显示「环境变量」来源。

worker 内部：

```http
PUT  http://worker:6300/settings
POST http://worker:6300/lanes/{key}/viewer
POST http://worker:6300/lanes/{key}/viewer/release
```

API VNC WebSocket 建立时自动向 worker 登记 viewer，断开时释放，因此自动关闭不会误杀正在查看的 VNC。

## 4. VNC WebSocket

```text
WS /api/v1/lanes/{lane_key}/vnc
```

- 校验管理员会话 Cookie。
- 校验同源 Origin。
- 连接 Worker 的固定车道 WebSocket，由 Worker 使用令牌转发到 Manager 的 profile VNC，子协议 `binary`。
- Manager 负责 Kasm 兼容消息改写；SiteFlow 两层代理仅透传，不重复转换指针和剪贴板。
- 前端关闭弹窗或点击「退出 VNC」时，只断开 RFB/WebSocket，不停止车道。

## 5. 前端

「车道状态」页面展示四类车道：

- 卡片显示「并行/串行 - 状态」；运行中显示「显示号:120 · N 正在使用」。
- 页面每 10 秒自动刷新车道状态，也可手动刷新。
- 「进入 VNC」打开弹窗，使用 `@novnc/novnc` 连接 API WebSocket。
- 弹窗内显示连接状态：连接中/已连接/已断开/连接失败。
- 「退出 VNC」关闭连接并关闭弹窗。
- 连接失败提供「重新连接」。

### 5.1 任务对照

- 运行详情中的车道名可进入 `/runs/{runId}/lanes/{laneKey}?job={jobId}`。
- 左侧约 1/3 显示该站点任务的状态步骤，右侧嵌入 VNC 画面。
- worker 为每个运行中的任务保存 `task_pages[job_id]`；`POST /api/v1/lanes/{key}/focus` 调用 `page.bring_to_front()` 切到任务标签页。
- 进入页面后，如果任务处于 `running`，前端自动重试定位标签页；也可点击「定位任务标签页」手动触发。
- 任务结束后标签页关闭，定位接口返回 404，VNC 仍可查看车道欢迎页或重新连接。
- 该布局供后续人工接管复用。

Vite 构建目标为 `es2022`，因为 `@novnc/novnc` 1.7 使用 top-level await。

实现细节：

- `RFB` 的 target 必须是**容器 div**（`.vnc-screen`），不能直接传 `<canvas>`；noVNC 会在容器内创建自己的 canvas。直接传 canvas 会出现「已连接但黑屏、canvas 仍是 300×150」。
- 在 `el-dialog` 的 `@opened` 后再创建 RFB，避免在弹窗过渡期间把 RFB 绑到会被替换的 DOM。
- 同时避免 `modelValue` 与 `laneKey` 两个 watcher 重复连接；当前用连接代次（generation）防止竞态。

## 6. 运行

```bash
./ops/build.sh
docker compose -f ops/docker-compose.yml up -d
docker compose -f ops/docker-compose.yml ps
```

worker 启动后所有车道为「已停止」。在「车道状态」页点击「启动浏览器」或由任务 lease 后，才通过后端启动专用 profile。代理未配置时两条代理车道不可启动。后端说明见 `docs/browser-backends.md`。

查看 worker 状态：

```bash
docker compose -f ops/docker-compose.yml logs -f worker
```

worker 内部健康检查：

```bash
docker compose -f ops/docker-compose.yml exec worker \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:6300/status').read().decode())"
```

## 7. 排障

黑屏但状态显示「已连接」：

1. 打开浏览器控制台，检查 `.vnc-screen canvas` 的 `width/height` 是否为 1920×1080；如果是 300×150，说明 RFB 绑到了错误元素或弹窗尚未 opened。
2. 确认 `VncViewer` 使用的是 `<div ref="screen">` 容器，而不是 `<canvas>`。
3. 确认 WebSocket 只建立一次；重复连接会互相抢占同一个画布。
4. 检查 API 日志是否出现 RFB 消息；正常情况下应看到 ServerInit（1920×1080）和 FramebufferUpdate。

## 8. 当前限制

- 空闲自动关闭是全局设置，不是 per-lane；并行任务数只对普通/代理普通车道生效，交互车道固定 1。
- VNC 操作为管理员全权限；尚未写细粒度审计日志。
- worker 重启会关闭所有车道；下次启动按需重新启动。SQLite 中的登录 Cookie 不受影响。
- 代理车道未配置时不会回退直连。
