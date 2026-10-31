# 数据模型

首次启动使用 SQLAlchemy `create_all` 按当前模型建表。启动额外检查并补齐 `sites.trusted_urls_json` 字段，旧站点默认空数组，不重写已有工作流。此操作可重复执行，不提供其他历史开发版本的通用升级或数据库移植保证。更新前应备份数据库，并避免多个 API 进程同时执行结构更新。

## 1. 已实现

```sql
users(
  id, username UNIQUE, password_hash, created_at, updated_at, password_changed_at
);

sessions(
  id, token_hash UNIQUE, username, auth_source,
  csrf_hash, created_at, last_seen_at, expires_at, ip, user_agent
);

integration_configs(
  key PK, value_ciphertext, nonce, key_version,
  is_secret, fingerprint, updated_at, updated_by
);

app_settings(
  key PK, value_json, updated_at
);

templates(
  id, name UNIQUE, description, parent_id, enabled, version,
  created_at, updated_at
);

sites(
  id, name UNIQUE, base_url, enabled, template_id,
  lane_policy, support_resign, resign_max_days,
  bitwarden_enabled, bitwarden_match_mode, bitwarden_custom_match,
  checkin_after, timezone, tags_json, trusted_urls_json, notes,
  config_revision, created_at, updated_at
);

workflow_steps(
  id, owner_type, owner_id, step_key, feature_type,
  feature_version, label, category, position, enabled,
  config_json, condition, on_error, timeout_seconds,
  created_at, updated_at,
  UNIQUE(owner_type, owner_id, step_key)
);

workflow_signals(
  id, owner_type, owner_id, signal_key, label, description,
  include_texts_json, include_selectors_json,
  exclude_texts_json, exclude_selectors_json,
  match_mode, enabled, created_at, updated_at,
  UNIQUE(owner_type, owner_id, signal_key)
);

site_credentials(
  site_id PK, username_ciphertext, username_nonce,
  password_ciphertext, password_nonce,
  key_version, last_verified_at, last_error, updated_at
);

runs(
  id, mode, trigger, status, requested_sites_json, force,
  cancel_requested, summary_json, created_by,
  created_at, started_at, finished_at
);

site_jobs(
  id, run_id, site_id, site_name, lane, attempt, status,
  message, result_json, worker_id, heartbeat_at,
  started_at, finished_at
);

job_steps(
  id, site_job_id, step_key, feature_type, label, status,
  message, data_json, started_at, finished_at,
  UNIQUE(site_job_id, step_key)
);
```

- `users` 只在用户主动修改密码后写入；env 管理员不写库。
- `sessions` 只存 token/CSRF 的 SHA-256，不存原值。
- `integration_configs` 全部 AES-256-GCM 加密；当前存代理、2FAuth 和 CFbox（`cfbox.url` / `cfbox.api_key`，API Key 可选）。
- `app_settings` 存车道设置、调度模式/参数和调度器的下次运行时间。
- `templates` 通过 `parent_id` 形成继承链；删除前检查子模板和站点引用。
- `sites.template_id` 为空时表示只使用站点自己的步骤。
- `sites.lane_policy` 由站点「优先交互」「使用代理」两个开关映射为四种车道；模板不设置车道。
- `workflow_steps` 是模板/站点共用的步骤表：
  - `owner_type` 为 `template` 或 `site`，`owner_id` 指向对应记录；
  - `enabled`、`condition`、`on_error` 可为空；空值表示继承父级或使用默认值；
  - `position` 是前端拖拽生成的内部排序值，不对用户展示；模板步骤固定，新步骤可插入中间；
  - `config_json` 保存覆盖配置，`null` 表示删除继承字段；
  - `condition` 保存 `always` 或 `signal:<signal_key>:true|false`；
  - `category` 和 `feature_version` 由功能目录写入，用于查询和版本校验。
- `site_credentials` 保存站点登录凭据：
  - 用户名和密码分别加密，使用独立 nonce；
  - AAD 绑定 `site_credential:{site_id}:{field}`，防止密文跨站点/字段搬运；
  - API 只返回是否配置、掩码用户名、时间和错误，不返回密码或原始用户名；
  - 站点删除时一并删除；
  - 后续执行器在登录步骤前解密到内存，用完清除，不写日志。
- `runs` / `site_jobs` / `job_steps` 保存执行历史：
  - 站点名以快照保存，站点删除后历史仍可读；
  - 运行支持排队、运行、成功、失败、等待人工、取消和中断；
  - 步骤保存功能块、状态、消息和页面数据；
  - API 重启时未完成运行标记为 `interrupted`。
- `workflow_signals` 定义页面信号：
  - 按模板链和站点覆盖合并，同名 `signal_key` 覆盖检测规则；
  - 包含文本/选择器与排除文本/选择器共同决定信号真假；
  - 被步骤条件引用的信号不能删除，但可以修改规则；
  - 信号无法判断时，执行器升级为人工接管。
- 站点/模板保存时会解析完整工作流，Schema 校验失败则回滚。

## 2. 报表与归档表（规划）

```sql
site_resign_records(
  site_id, resign_date, status, message, run_id, job_id,
  created_at, updated_at,
  UNIQUE(site_id, resign_date)
);

daily_summary(
  date PK, total, success, fail, error, unknown, deferred, disabled, updated_at
);

artifacts(
  id, run_id, site_job_id, step_key, kind, path,
  mime, size, sha256, created_at, expires_at
);

site_storage_states(
  id, site_id UNIQUE, state_ciphertext, nonce, key_version,
  cookie_count, origin_count, user_agent, exported_at, expires_at, fingerprint
);

site_totp_bindings(
  site_id PK, provider, account_id, service_hint, match_mode,
  verified_at, updated_at
);

proxies(
  id, name, url_ciphertext, nonce, key_version, enabled
);
```

## 3. 状态枚举

沿用旧签到系统的状态值，避免报表断裂：

```text
success / fail / error / deferred / unknown / disabled
```

任务/站点作业状态：

```text
queued / running / success / fail / error / deferred /
unknown / skipped / waiting_human / canceled / interrupted
```

## 4. 保留与归档

- `site_credentials` 保存用户名、密码和浏览器 Cookie 的加密数据；Cookie 只返回数量和时间，不回显内容。
- `site_resign_records` 记录补签结果；成功和“已经签到”日期不再重复补签。
- `sites.support_resign` 控制是否注入系统「断签」信号，默认开启。
- `sites.resign_max_days` 同时控制待补签历史窗口、购卡上限和单次补签上限，默认 30 天。
- `sites.bitwarden_enabled` 启用后，任务从 Bitwarden 包装服务按 `bitwarden_match_mode`（`domain` / `root_domain` / `custom`）查询登录用户名和密码；本地账户密码不再参与登录。
- `runs` / `site_jobs` / `job_steps` 保留执行历史。
- 日志/截图落共享卷文件，数据库 `job_artifacts` 只存相对路径、MIME、大小、sha256、时间和步骤关联。
- 每个站点任务都有日志；步骤执行后不是成功（且不是跳过）就保存截图，任务最终非成功但没有步骤截图时补任务级截图。
- 默认保留 30 天；后台任务每 6 小时清理过期文件和记录，同时清理过期孤儿文件。

## 5. 兼容性

- 状态名、站点名、`checkin_after` 等语义与旧 YAML 保持一致。
