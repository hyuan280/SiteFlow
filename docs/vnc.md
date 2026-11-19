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

通用车道并行的是任务，不是同时前台输入；完整输入动作和短暂创建/焦点转换共享输入门，关闭任务页不占该门。当前输入、执行限流、生命周期隔离或真实资源仍可让其他任务等待，详见[多标签等待排查](usage.md#多标签等待排查)。

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
- 存在任务租约（`in_use > 0`，包括未确认关闭的清理租约）时 `stop` 返回 409，前端「关闭浏览器」按钮禁用；这不一定表示业务仍在执行。
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

任务执行器必须先 `lease`，确认任务自有页面关闭且无未决创建后才 `release`。未确认时保留租约，由 reaper 重试，不能为显示空闲强行清零 `in_use`。release 后车道保持运行，便于人工检查或通过 VNC 接管；需要释放资源时由前端或后续空闲策略关闭。缺少远端目标身份的遗留可能需要管理员核查，不保证自动恢复，见[任务生命周期](browser-backends.md#job-lifecycle)。

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
- 启用 noVNC `showDotCursor`，远端未提供可见光标时显示圆点；远端提供正常光标时保留其形状，不覆盖远端光标样式或隐藏辅助光标画布。
- 先点击远端目标输入框，在 VNC 画面按 `Ctrl+V` / `Cmd+V` 可读取本机剪贴板并粘贴文本。仅在用户操作时读取，不轮询或自动同步剪贴板。
- HTTP 访问、浏览器未授权或不支持 Clipboard API 时，使用工具栏「粘贴文本」，将内容粘贴到本地文本框后点击「发送并粘贴」。支持中文、Emoji 和多行文本，最多 1,048,576 个字符；不支持图片或文件。
- 文本通过 `POST /api/v1/lanes/{lane_key}/clipboard`，经 Worker 转发至当前 profile 的 Manager HTTP 剪贴板接口；确认写入后才通过 VNC 发送远端 `Ctrl+V`。不使用会损失中文的普通 RFB `ClientCutText`。
- 剪贴板写入要求管理员会话和 CSRF 校验，内部接口要求令牌；不自动启动已停止车道，响应禁止缓存，异常不回显剪贴板内容。关闭或切换连接后，旧请求不会继续发送粘贴按键。
- 在远端选中文本后，在 VNC 画面按 `Ctrl+C` / `Cmd+C` 会发送远端 Linux `Ctrl+C`，等待剪贴板通知或短暂延迟后，通过 `GET /api/v1/lanes/{lane_key}/clipboard` 读取文本并尝试写入本机剪贴板。显示「已复制到本机剪贴板」后即可移出 VNC 粘贴；通知及延迟不构成远端复制完成的协议确认，远端很慢时可重试。
- 使用远端右键菜单复制后，点击工具栏「复制到本机」。此操作只读取已复制的内容，不再发送远端按键。不会后台轮询或因任意远端事件覆盖本机剪贴板。
- 本机剪贴板写入被拒绝或使用 HTTP 时，会显示只读文本框，可点击「复制此文本」重试，也可选中文本后用本机 `Ctrl+C` / `Cmd+C`。远端返回空字符串时不会清空本机剪贴板。
- 默认 X11 部署由 Worker 用 `xclip -selection clipboard -o -target UTF8_STRING` 读取当前系统剪贴板，避免 Manager 多标签页捕获缓存返回旧文本。Worker 镜像需要重新构建以安装 `xclip`，继续使用共享 X11 socket；非 X11 模式回退 Manager GET 接口，其多标签页缓存可能滞后。两条读取路径均限制大小、验证 UTF-8、设置超时并禁止缓存。
- 读取要求管理员会话和内部令牌，仅读取运行中的车道。关闭或切换连接会丢弃待处理读取，不再触发本机写入；已经调用的浏览器系统剪贴板写入无法撤销。

### 5.1 任务对照

- 运行详情中的车道名可进入 `/runs/{runId}/lanes/{laneKey}?job={jobId}`。
- 左侧约 1/3 显示该站点任务的状态步骤，右侧嵌入 VNC 画面。
- worker 为每个运行中的任务保存 `task_pages[job_id]`；`POST /api/v1/lanes/{key}/focus` 调用 `page.bring_to_front()` 切到任务标签页。
- 进入页面后，如果任务处于 `running`，前端自动重试定位标签页；也可点击「定位任务标签页」手动触发。
- 确认任务自有页面关闭并移除映射后，定位接口返回 404；未确认清理时可能保留映射，定位失败也不证明租约已释放。清理不会主动切回欢迎页，VNC 可能显示其他任务或欢迎页。
- 该布局供后续人工接管复用。

Vite 构建目标为 `es2022`，因为 `@novnc/novnc` 1.7 使用 top-level await。

实现细节：

- `RFB` 的 target 必须是**容器 div**（`.vnc-screen`），不能直接传 `<canvas>`；noVNC 会在容器内创建自己的 canvas。直接传 canvas 会出现「已连接但黑屏、canvas 仍是 300×150」。
- 在 `el-dialog` 的 `@opened` 后再创建 RFB，避免在弹窗过渡期间把 RFB 绑到会被替换的 DOM。
- 使用单个 watcher 处理 `modelValue` 和 `laneKey`，弹窗首次连接仍等待 `opened`；连接代次（generation）防止重连及剪贴板异步请求竞态。

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
