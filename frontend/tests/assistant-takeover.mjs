import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from "vue";
import { renderToString } from "@vue/server-renderer";

const venvPython = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));
const contract = JSON.parse(execFileSync(process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python3"), ["-c", `
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models.setting import AppSetting
from app.services.assistant_takeover_settings import KEY, TakeoverSettingsService
engine = create_engine('sqlite://')
AppSetting.__table__.create(engine)
with Session(engine) as db:
    service = TakeoverSettingsService(db)
    defaults = service.view().model_dump(mode='json')
    legacy = dict(revision=2, authorizing_owner='admin', authorized_at='2026-11-07T12:00:00Z', effective_from='2026-11-07T12:00:00Z',
                  values=dict(enabled=True, mode='analysis', site_ids=[7], max_attempts=1))
    db.add(AppSetting(key=KEY, value_json=json.dumps(legacy)))
    db.commit()
    migrated = service.view().model_dump(mode='json')
    legacy['values']['live_access'] = True
    db.get(AppSetting, KEY).value_json = json.dumps(legacy)
    db.commit()
    print(json.dumps(dict(defaults=defaults, migrated=migrated, current=service.view().model_dump(mode='json'))))
`], { cwd: fileURLToPath(new URL("../../backend", import.meta.url)), encoding: "utf8" }));

const exposed = "view, draft, allSites, sites, actions, loading, busy, error, actionsError, notice, needsRefresh, values, canSave, refresh, save, close, selectSession";
const server = await createServer({ optimizeDeps: { noDiscovery: true, include: [] }, server: { middlewareMode: true, hmr: false },
  plugins: [{ name: "takeover-test", enforce: "pre", transform(code, path) {
    if (!path.endsWith("/AssistantTakeoverDialog.vue")) return;
    for (const line of ['import { useRouter } from "vue-router";', 'import { ElMessageBox } from "element-plus";',
      'import { ApiError, apiRequest } from "@/api/client";', 'import { useAuthStore } from "@/stores/auth";']) {
      code = code.replace(line, line.replace(/import (.*?) from .*;/, "const $1 = globalThis.__takeoverMocks;"));
    }
    return code.replace("</script>", `defineExpose({ ${exposed} });\n</script>`);
  } }] });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick(); } };
const requests = [], confirmations = [], routes = [], sessionEvents = [];
let cleared = 0, app;
class ApiError extends Error { constructor(status, message, code) { super(message); this.status = status; this.code = code; } }
globalThis.__takeoverMocks = {
  ApiError,
  apiRequest(url, options = {}) { const pending = deferred(); requests.push({ url, ...options, ...pending }); return pending.promise; },
  useAuthStore: () => ({ clear() { cleared++; } }),
  useRouter: () => ({ replace: async route => { routes.push(route); } }),
  ElMessageBox: { confirm(...args) { const pending = deferred(); confirmations.push({ args, ...pending }); return pending.promise; } },
};
const defaults = { enabled: false, live_access: true, mode: "analysis", site_ids: [], max_attempts: 1, conclusion_ttl_days: 7 };
const settings = (revision = 0, values = defaults) => ({ revision, source: revision ? "db" : "default", values: { ...values } });
const options = [{ id: 7, name: '<img src=x onerror="bad()">', enabled: true, base_url: "https://example.com" }, { id: 8, name: "Disabled", enabled: false }];
const action = { id: randomUUID(), site_id: 7, source_run_id: 42, session_id: randomUUID(), mode: "analysis", status: "completed_analysis", created_at: "2026-11-07T12:00:00Z" };
try {
  const { default: Dialog } = await server.ssrLoadModule("/src/components/AssistantTakeoverDialog.vue");
  const types = await server.ssrLoadModule("/src/types/assistant-takeover.ts");
  assert.deepEqual(contract.defaults.values, defaults, "new policy defaults match the actual backend settings service");
  assert.deepEqual(contract.migrated.values, { ...defaults, enabled: true, live_access: false, site_ids: [7] }, "legacy policies never inherit the new live-access default");
  assert.equal(types.isAssistantTakeoverSettings(contract.defaults), true);
  assert.equal(types.isAssistantTakeoverSettings(contract.migrated), true);
  assert.deepEqual(contract.current.values, { ...defaults, enabled: true, site_ids: [7] }, "current pre-TTL policies keep their live-access decision and default TTL to 7");
  assert.equal(types.isAssistantTakeoverSettings(contract.current), true);
  for (const days of [1, 7, 30]) assert.equal(types.isAssistantTakeoverValues({ ...defaults, conclusion_ttl_days: days }), true);
  for (const days of [undefined, null, true, false, "7", 0, 31, 1.5, NaN, Infinity]) {
    assert.equal(types.isAssistantTakeoverValues({ ...defaults, conclusion_ttl_days: days }), false);
  }
  const { conclusion_ttl_days, ...preTtlValues } = defaults;
  assert.equal(types.isAssistantTakeoverValues(preTtlValues), false, "only the server migrates legacy storage; API values must be complete");
  assert.equal(types.takeoverStatusLabel("constructor"), "状态待核对");
  assert.equal(types.takeoverStatusLabel("updated"), "工作流已保存（尚未运行验证）");
  assert.equal(types.takeoverStatusLabel("failed"), "失败");
  assert.equal(types.takeoverStatusLabel("reused"), "相同失败，复用已有分析");
  for (const fields of [{}, { conclusion: null, notification_message: null }, { conclusion: "已分析" },
    { notification_message: "通知结论" }, { conclusion: "", notification_message: "" },
    { conclusion: "已保存", notification_message: "通知结论" },
    { status: "reused", attempts: 0, reused_from_action_id: randomUUID(), reused_from_run_id: 41 },
    { reused_from_action_id: null, reused_from_run_id: null }]) {
    assert.equal(types.isAssistantTakeoverActions({ items: [{ ...action, ...fields }] }), true);
  }
  for (const fields of [{ attempts: -1 }, { attempts: "0" }, { reused_from_action_id: "bad" },
    { reused_from_run_id: 0 }, { reused_from_run_id: "41" }]) {
    assert.equal(types.isAssistantTakeoverActions({ items: [{ ...action, ...fields }] }), false);
  }
  for (const key of ["conclusion", "notification_message"]) {
    for (const invalid of [true, 42, [], {}]) {
      assert.equal(types.isAssistantTakeoverActions({ items: [{ ...action, [key]: invalid }] }), false);
    }
  }
  assert.equal(types.isAssistantTakeoverSettings({ revision: 0, values: defaults, authorizing_owner: null, authorized_at: null, effective_from: null }), true);
  assert.equal(types.isAssistantTakeoverSettings({ revision: 1, values: defaults, authorizing_owner: "admin", authorized_at: "2026-11-07T12:00:00Z", effective_from: "2026-11-07T12:00:00Z" }), true);
  assert.equal(types.isAssistantTakeoverValues({ ...defaults, live_access: false }), true);
  const { live_access, ...legacyValues } = defaults;
  assert.equal(types.isAssistantTakeoverValues(legacyValues), false, "server migration must supply an explicit boolean; missing values never imply consent");
  const props = reactive({ modelValue: false });
  let ui, bindings;
  const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} });
  app = renderer.createApp({ setup() {
    bindings = Dialog.setup(props, { expose(value) { ui = value; }, emit(event, value) {
      if (event === "update:modelValue") props.modelValue = value;
      if (event === "session") sessionEvents.push(value);
    } });
    return () => null;
  } });
  app.provide(ssrContextKey, { modules: new Set() }); app.mount({});
  const last = () => requests.at(-1);
  const html = async () => {
    const render = createSSRApp({ ...Dialog, setup: () => bindings }); render.config.warnHandler = () => {};
    render.component("el-dialog", { setup: (_, { slots }) => () => h("section", [slots.default?.(), slots.footer?.()]) });
    render.component("router-link", { props: ["to"], setup: (props, { slots }) => () => h("a", { href: `${props.to.path}?session=${props.to.query.session}` }, slots.default?.()) });
    render.component("el-option", { props: ["label"], setup: props => () => h("option", props.label) });
    return renderToString(render);
  };
  const open = () => { props.modelValue = false; props.modelValue = true; return requests.slice(-3); };
  const resolveOpen = async (view = settings(), history = { items: [] }) => {
    const group = open();
    assert.deepEqual(group.map(item => item.url), ["/assistant/takeover/settings", "/sites?view=options", "/assistant/takeover/actions?limit=20"]);
    group[0].resolve(view); group[1].resolve(options); group[2].resolve(history); await tick(); return group;
  };
  const approve = async () => { confirmations.at(-1).resolve(); await tick(); return last(); };
  assert.equal(requests.length, 0);
  const first = open(); assert.equal(ui.canSave.value, false);
  first[0].resolve(contract.defaults); first[1].resolve(options); first[2].resolve({ items: [action] }); await tick();
  assert.deepEqual(ui.values.value, defaults); assert.equal(ui.canSave.value, true);
  assert.equal(ui.draft.conclusion_ttl_days, "7");
  ui.draft.site_ids = [7]; assert.deepEqual(ui.view.value.values.site_ids, []);
  ui.allSites.value = true; assert.equal(ui.values.value.site_ids, null);
  ui.allSites.value = false; assert.deepEqual(ui.values.value.site_ids, [7]);
  let rendered = await html(); assert.match(rendered, /查看接管会话/); assert.match(rendered, /任务 #42/);
  for (const term of ["接管失败、错误和需要人工的任务；已推迟不接管", "仅限策略生效后创建、已结束的手动或调度任务",
    "站点已启用并在授权范围内", "未知、中断、取消及未完成任务不接管", "不保证所有挑战都能自动处理",
    "需要人工且无副作用风险的挑战", "实时授权及宿主检查通过后", "消费结果不确定时只读证据和浏览",
    "不执行变更单步或导入", "不自动重放历史部分消费", "需要人工的动态挑战不复用旧挑战分析",
    "失败/错误任务仍按同配置、同故障去重", "历史失败记录本身不会阻止分析；仅已有有效的同版本同故障调查结论才复用。"]) assert.ok(rendered.includes(term), term);
  assert.equal(requests.length, 3, "opening and rendering status scope only reads policy, site options and history; it never starts a backend model or task");
  assert.match(rendered, /&lt;img/); assert.doesNotMatch(rendered, /<img|\[object Object\]/);
  assert.match(rendered, /所有当前及未来启用/);
  for (const term of ["同一授权用户、同一站点", "同一本地日期内复用接管会话", "打开会话继续聊天", "次日创建新会话", "历史修复摘要仍会保留",
    "任务结束立即发送原任务通知，接管完成后独立发送简短结论通知", "原任务状态不改变，需要人工仍为 waiting_human，不改为成功，修改后不自动重跑", "授权无人值守浏览与单步验证（权限保留供后续对话）",
    "仅分析不保存配置，并非只读", "私密凭据登录", "提交表单", "签到或点击", "真实业务副作用", "脱敏截图和页面内容", "站点已保存的代理设置",
    "interaction 或 proxy_interaction", "所选站点身份及获授权时的 URL", "不代表授权所有来源或任意 URL", "自动接管结束后释放本轮浏览器", "不保留原页面或 DOM",
    "同站点、同 URL 的已授予权限保留供用户后续对话", "用户可要求重新打开新页面", "无需重复授予仍有效的权限", "不会抢占人工浏览器或查看连接",
    "其他操作仍需交互授权", "不授权自动完整运行", "不跳过消费确认", "结果未知时不自动重试", "保存策略本身不会启动浏览器或执行任务"]) assert.ok(rendered.includes(term), term);
  assert.doesNotMatch(rendered, /当前接管不允许浏览器操作|不会访问浏览器或单步执行|保留浏览器|浏览器保留|同页继续|保留到用户对话/);
  assert.doesNotMatch(rendered, /简短结论：/);
  assert.ok(rendered.includes("同配置同故障结论仅在有效期内可复用，过期后遇新合格失败重新调查；历史保留"));
  assert.ok(rendered.includes("修改有效期仅影响后续复用判断，不自动重查旧任务；复用不会延长原结论有效期"));
  const conclusion = '<img src=x onerror="bad()"> 已保存，尚未验证';
  await resolveOpen(settings(), { items: [
    { ...action, mode: "autofix", status: "updated", conclusion, notification_message: "不应覆盖主结论" },
    { ...action, id: randomUUID(), source_run_id: 43, status: "failed", conclusion: null, notification_message: "仍需人工处理" },
    { ...action, id: randomUUID(), source_run_id: 44, conclusion: null, notification_message: null },
    { ...action, id: randomUUID(), source_run_id: 45 },
    { ...action, id: randomUUID(), source_run_id: 46, conclusion: "", notification_message: "" },
  ] });
  rendered = await html();
  assert.match(rendered, /<p class="takeover-conclusion"[^>]*>简短结论：&lt;img/);
  assert.match(rendered, /工作流已保存（尚未运行验证）/);
  assert.match(rendered, /任务 #43[^<]*失败/);
  assert.match(rendered, /简短结论：仍需人工处理/);
  assert.equal((rendered.match(/class="takeover-conclusion"/g) ?? []).length, 2);
  assert.doesNotMatch(rendered, /<img|不应覆盖主结论|等待通知|通知状态/);
  await resolveOpen(settings(), { items: [{ ...action, status: "reused", attempts: 0, conclusion,
    reused_from_action_id: randomUUID(), reused_from_run_id: 41 }] });
  rendered = await html();
  assert.match(rendered, /相同失败，复用已有分析/);
  assert.match(rendered, /历史结论（复用，非本次重新分析）：&lt;img/);
  assert.match(rendered, /尝试次数：0/);
  assert.match(rendered, /复用来源任务 #41/);
  assert.doesNotMatch(rendered, /<img|工作流已保存|简短结论：/);
  const beforeSession = requests.length;
  ui.selectSession(action.session_id);
  assert.equal(props.modelValue, false); assert.deepEqual(sessionEvents, [action.session_id]);
  assert.equal(requests.length, beforeSession, "opening a shared daily session does not create a session or rerun tasks");
  // Old stored delegations arrive with live_access=false after server migration.
  await resolveOpen(contract.migrated);
  assert.equal(ui.draft.live_access, false);
  assert.equal(ui.draft.conclusion_ttl_days, "7");
  rendered = await html();
  assert.match(rendered, /未授权无人值守浏览与单步验证：不会访问浏览器或单步执行/);
  assert.doesNotMatch(rendered, /授权后可无人值守浏览并单步验证/);
  const noLiveSave = ui.save();
  assert.match(confirmations.at(-1).args[0], /无人值守浏览与单步验证：未授权/);
  assert.match(confirmations.at(-1).args[0], /结论复用有效期 7 天/);
  const noLivePut = await approve();
  assert.equal(JSON.parse(noLivePut.body).values.live_access, false);
  noLivePut.resolve(settings(3, JSON.parse(noLivePut.body).values)); await noLiveSave;
  const beforeReconsent = requests.length;
  ui.draft.live_access = true;
  assert.equal(requests.length, beforeReconsent, "checking the grant does not activate a browser or save policy");
  assert.equal(ui.view.value.values.live_access, false, "editing does not alter the saved grant");
  const reconsent = ui.save();
  assert.match(confirmations.at(-1).args[0], /仅分析（不保存配置，并非只读）/);
  assert.match(confirmations.at(-1).args[0], /无人值守浏览与单步验证：授权/);
  assert.equal(requests.length, beforeReconsent, "fresh consent is required before saving the grant");
  const reconsentPut = await approve();
  assert.deepEqual(JSON.parse(reconsentPut.body), { expected_revision: 3, values: { ...defaults, enabled: true, site_ids: [7] }, confirm: true });
  reconsentPut.resolve(settings(4, JSON.parse(reconsentPut.body).values)); await reconsent;
  assert.equal(ui.view.value.values.live_access, true);
  for (const live_access of [false, true]) {
    await resolveOpen();
    ui.draft.live_access = live_access;
    const savingDisabled = ui.save(); const disabledPut = await approve();
    assert.deepEqual(JSON.parse(disabledPut.body).values, { ...defaults, live_access }, "disabled takeover still saves explicit live_access consent");
    disabledPut.resolve(settings(1, JSON.parse(disabledPut.body).values)); await savingDisabled;
  }
  await resolveOpen();
  ui.draft.site_ids = [7]; ui.draft.max_attempts = "3";
  for (const bad of ["", "0", "4", "1.0", "1e0", true, 1, null, " 1 "]) {
    ui.draft.max_attempts = bad; assert.equal(ui.canSave.value, false);
  }
  ui.draft.max_attempts = "3";
  for (const bad of ["", "0", "31", "-1", "7.0", "1e1", "07", " 7 ", "7 ", true, 7, null]) {
    ui.draft.conclusion_ttl_days = bad;
    assert.equal(ui.canSave.value, false);
    const count = confirmations.length;
    await ui.save();
    assert.equal(confirmations.length, count, "invalid TTL never reaches confirmation or PUT");
  }
  for (const days of ["1", "7", "30"]) {
    ui.draft.conclusion_ttl_days = days;
    assert.equal(ui.canSave.value, true);
    assert.equal(ui.values.value.conclusion_ttl_days, Number(days));
  }
  for (const [key, bad] of [["enabled", "true"], ["live_access", "true"], ["live_access", undefined], ["live_access", null], ["live_access", 1], ["mode", "other"], ["site_ids", [0]], ["site_ids", [7, 7]], ["site_ids", ["7"]]]) {
    const previous = ui.draft[key]; ui.draft[key] = bad; assert.equal(ui.canSave.value, false); ui.draft[key] = previous;
  }
  const before = requests.length;
  const cancelled = ui.save(); assert.equal(requests.length, before, "confirmation precedes PUT");
  await ui.save(); await ui.refresh(); ui.close(); assert.equal(props.modelValue, true);
  confirmations.at(-1).reject("cancel"); await cancelled; assert.equal(ui.canSave.value, true);
  ui.draft.enabled = true; ui.draft.mode = "autofix"; ui.allSites.value = true;
  const saving = ui.save();
  const consent = confirmations.at(-1).args[0];
  assert.match(consent, /结论复用有效期 30 天/);
  assert.ok(consent.includes("同配置同故障结论仅在有效期内可复用，过期后遇新合格失败重新调查；历史保留"));
  for (const term of ["所有当前及未来启用", "报告", "日志", "截图", "已配置的模型", "仅允许修改工作流", "不会重新运行完整任务", "凭据", "元数据", "模板", "全局设置",
    "接管失败、错误和需要人工的任务；已推迟不接管", "仅限策略生效后创建、已结束的手动或调度任务", "站点已启用并在授权范围内",
    "未知、中断、取消及未完成任务不接管", "需要人工的任务仍受动态安全检查", "消费结果不确定时只读证据和浏览", "不执行变更单步或导入", "不自动重放历史部分消费",
    "无人值守浏览与单步验证：授权", "私密凭据登录", "真实业务副作用", "脱敏截图和页面内容", "interaction 或 proxy_interaction", "所选站点身份及获授权时的 URL",
    "不代表授权所有来源或任意 URL", "自动接管结束后释放本轮浏览器", "不保留原页面或 DOM", "同站点、同 URL 的已授予权限保留供用户后续对话",
    "用户可要求重新打开新页面", "无需重复授予仍有效的权限", "不会抢占人工浏览器或查看连接", "不跳过消费确认", "结果未知时不自动重试", "保存策略本身不会启动浏览器或执行任务"]) assert.ok(consent.includes(term), term);
  assert.doesNotMatch(consent, /不会访问浏览器|不会单步执行|保留浏览器|浏览器保留|同页继续|保留到用户对话/);
  ui.draft.enabled = false; ui.draft.live_access = false; ui.draft.conclusion_ttl_days = "1"; ui.view.value.revision = 99;
  const put = await approve();
  assert.equal(put.method, "PUT"); assert.equal(put.signal, undefined);
  assert.deepEqual(JSON.parse(put.body), { expected_revision: 0, values: { enabled: true, live_access: true, mode: "autofix", site_ids: null, max_attempts: 3, conclusion_ttl_days: 30 }, confirm: true });
  put.resolve(settings(1, JSON.parse(put.body).values)); await saving;
  assert.equal(ui.view.value.revision, 1); assert.match(ui.notice.value, /已保存/);
  assert.equal(ui.draft.conclusion_ttl_days, "30", "save response replaces the string draft with the confirmed TTL");

  for (const failure of [new ApiError(409, "PRIVATE", "takeover_settings_conflict"), new Error("PRIVATE"), { malformed: true }]) {
    await resolveOpen(settings(3)); ui.draft.max_attempts = "2";
    const pending = ui.save(); const request = await approve();
    if (failure instanceof Error) request.reject(failure); else request.resolve(failure);
    await pending;
    assert.equal(ui.draft.max_attempts, "2"); assert.equal(ui.canSave.value, false); assert.equal(ui.needsRefresh.value, true);
    assert.doesNotMatch(ui.error.value, /PRIVATE/);
    assert.match(ui.error.value, failure instanceof ApiError ? /其他客户端/ : /可能已保存/);
    const count = requests.length; await ui.save(); assert.equal(requests.length, count, "no automatic or manual resend with stale revision");
    const refresh = ui.refresh(); confirmations.at(-1).reject("cancel"); await refresh;
    assert.equal(ui.draft.max_attempts, "2");
  }
  const refresh = ui.refresh(); confirmations.at(-1).resolve(); await tick();
  const reads = requests.slice(-3); reads[0].resolve(settings(4)); reads[1].resolve(options); reads[2].resolve({ items: [] }); await refresh;
  assert.equal(ui.canSave.value, true);

  for (const invalid of [null, {}, settings(-1), { ...settings(), source: "env" }, settings(1, { ...defaults, enabled: "false" }),
    settings(1, legacyValues), ...[null, "false", 0, {}].map(live_access => settings(1, { ...defaults, live_access })),
    settings(1, preTtlValues), ...[null, "7", true, 0, 31, 1.5].map(conclusion_ttl_days => settings(1, { ...defaults, conclusion_ttl_days })),
    settings(1, { ...defaults, max_attempts: 4 }), settings(1, { ...defaults, site_ids: [1, 1] }), settings(1, { ...defaults, mode: "run" })]) {
    assert.equal(types.isAssistantTakeoverSettings(invalid), false);
    await resolveOpen(invalid); assert.equal(ui.view.value, null); assert.equal(ui.canSave.value, false);
  }
  const invalidSites = open(); invalidSites[0].resolve(settings()); invalidSites[1].resolve([{ ...options[0], enabled: "true" }]); invalidSites[2].resolve({ items: [] }); await tick();
  assert.equal(ui.canSave.value, false); assert.equal(ui.view.value, null);
  for (const fields of [{ session_id: "javascript:bad" }, { conclusion: {} }, { notification_message: 42 }]) {
    await resolveOpen(settings(), { items: [{ ...action, ...fields }] });
    assert.match(ui.actionsError.value, /格式无效/); assert.equal(ui.actions.value.length, 0);
  }
  assert.equal(ui.canSave.value, true, "history failure does not fabricate or invalidate an independent policy");
  ui.selectSession(action.session_id); assert.equal(props.modelValue, false); assert.deepEqual(sessionEvents, [action.session_id, action.session_id]);

  const staleReads = open(); ui.close(); assert.ok(staleReads.every(request => request.signal.aborted));
  const freshReads = open(); staleReads[0].reject(new ApiError(401, "PRIVATE")); staleReads[1].resolve(options); staleReads[2].resolve({ items: [action] }); await tick();
  assert.equal(cleared, 0); assert.equal(ui.loading.value, true);
  freshReads[0].resolve(settings(10)); freshReads[1].resolve(options); freshReads[2].resolve({ items: [] }); await tick();
  const staleConfirm = ui.save(); const confirm = confirmations.at(-1); await resolveOpen(settings(11));
  const count = requests.length; confirm.resolve(); await staleConfirm; assert.equal(requests.length, count);
  const lateSave = ui.save(); const oldPut = await approve(); await resolveOpen(settings(13)); oldPut.resolve(settings(12)); await lateSave;
  assert.equal(ui.view.value.revision, 13); assert.equal(ui.notice.value, "");
  const unauthorized = ui.save(); (await approve()).reject(new ApiError(401, "PRIVATE")); await unauthorized;
  assert.equal(cleared, 1); assert.equal(props.modelValue, false); assert.deepEqual(routes, [{ name: "login" }]);
  await resolveOpen(settings(14)); const unmounted = ui.save(); const unmountedPut = await approve(); app.unmount(); app = null;
  unmountedPut.reject(new ApiError(401, "PRIVATE")); await unmounted; assert.equal(cleared, 1); assert.equal(ui.view.value, null);
  assert.ok(requests.every(request => ["/assistant/takeover/settings", "/assistant/takeover/actions?limit=20", "/sites?view=options"].includes(request.url)));
  const source = await readFile(new URL("../src/components/AssistantTakeoverDialog.vue", import.meta.url), "utf8");
  const viewSource = await readFile(new URL("../src/views/AIAssistantView.vue", import.meta.url), "utf8");
  assert.ok(viewSource.indexOf(">助手接管</el-button>") < viewSource.indexOf(">助手设置</el-button>"));
  assert.doesNotMatch(source, /v-html|localStorage|sessionStorage|console\./);
  assert.equal((source.match(/<el-switch\b/g) ?? []).length, 1, "the global takeover switch remains separate from live access consent");
  assert.match(source, /<el-checkbox v-model="draft\.live_access">授权无人值守浏览与单步验证（权限保留供后续对话）/);
  assert.match(source, /<el-input v-model="draft\.conclusion_ttl_days" type="number" min="1" max="30" step="1"/);
  assert.match(source, /@media \(max-width: 540px\)/);
  console.log("Assistant takeover regression passed");
} finally { app?.unmount(); delete globalThis.__takeoverMocks; await server.close(); }
