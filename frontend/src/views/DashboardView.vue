<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import type { Feature, SiteOption, TemplateSummary } from "@/types/workflow";
import { laneLabel } from "@/utils/workflow";

interface LaneStatus {
  key: string;
  label: string;
  status: string;
  in_use: number;
  viewers: number;
}

interface LanesResponse {
  worker_connected: boolean;
  lanes: LaneStatus[];
  error: string | null;
}

const auth = useAuthStore();
const router = useRouter();
const loading = ref(true);
const error = ref("");
const sites = ref<SiteOption[]>([]);
const templates = ref<TemplateSummary[]>([]);
const features = ref<Feature[]>([]);
const lanes = ref<LaneStatus[]>([]);
const workerConnected = ref(false);

const validSites = computed(() => sites.value.filter((site) => site.valid === true).length);
const invalidSites = computed(() => sites.value.length - validSites.value);
const runningLanes = computed(() => lanes.value.filter((lane) => lane.status === "running").length);
const enabledSites = computed(() => sites.value.filter((site) => site.enabled).length);

async function handleUnauthorized(cause: unknown): Promise<boolean> {
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const [siteList, templateList, featureList, laneResponse] = await Promise.all([
      apiRequest<SiteOption[]>("/sites?view=options&include_valid=true"),
      apiRequest<TemplateSummary[]>("/templates"),
      apiRequest<Feature[]>("/features"),
      apiRequest<LanesResponse>("/lanes"),
    ]);
    sites.value = siteList;
    templates.value = templateList;
    features.value = featureList;
    lanes.value = laneResponse.lanes;
    workerConnected.value = laneResponse.worker_connected;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    error.value = cause instanceof Error ? cause.message : "加载数据面板失败";
  } finally {
    loading.value = false;
  }
}

function go(name: string): void {
  router.push({ name });
}

onMounted(load);
</script>

<template>
  <section class="page-head">
    <div>
      <p class="eyebrow">DASHBOARD</p>
      <h1>数据面板</h1>
      <p class="muted">站点、模板、功能块和车道状态的当前概况。</p>
    </div>
  </section>

  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />

  <div class="stat-grid" v-loading="loading">
    <button class="stat-card" type="button" @click="go('sites')">
      <span class="stat-label">站点</span>
      <strong>{{ sites.length }}</strong>
      <small>启用 {{ enabledSites }} · 有效 {{ validSites }}</small>
    </button>
    <button class="stat-card" type="button" @click="go('templates')">
      <span class="stat-label">模板</span>
      <strong>{{ templates.length }}</strong>
      <small>可复用功能步骤</small>
    </button>
    <button class="stat-card" type="button" @click="go('features')">
      <span class="stat-label">功能块</span>
      <strong>{{ features.length }}</strong>
      <small>已注册功能类型</small>
    </button>
    <button class="stat-card" type="button" @click="go('browsers')">
      <span class="stat-label">车道</span>
      <strong>{{ runningLanes }} / {{ lanes.length }}</strong>
      <small>{{ workerConnected ? "Worker 在线" : "Worker 不可达" }}</small>
    </button>
  </div>

  <el-row :gutter="18" class="detail-row">
    <el-col :xs="24" :lg="14">
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <strong>站点状态</strong>
            <el-button link type="primary" @click="go('sites')">管理站点</el-button>
          </div>
        </template>
        <div v-if="sites.length" class="site-list">
          <button
            v-for="site in sites.slice(0, 8)"
            :key="site.id"
            type="button"
            class="site-row"
            @click="go('sites')"
          >
            <span>
              <strong>{{ site.name }}</strong>
              <small class="mono">{{ site.base_url }}</small>
            </span>
            <span class="site-tags">
              <el-tag size="small" effect="plain">{{ laneLabel(site.lane) }}</el-tag>
              <el-tag :type="site.valid ? 'success' : 'danger'" size="small" effect="plain">
                {{ site.valid ? "有效" : "无效" }}
              </el-tag>
              <el-tag :type="site.enabled ? 'success' : 'info'" size="small" effect="plain">
                {{ site.enabled ? "启用" : "停用" }}
              </el-tag>
            </span>
          </button>
        </div>
        <p v-else class="muted">还没有站点。进入“站点管理”创建第一个站点。</p>
      </el-card>
    </el-col>

    <el-col :xs="24" :lg="10">
      <el-card shadow="never" class="panel">
        <template #header>
          <div class="card-head">
            <strong>车道状态</strong>
            <el-button link type="primary" @click="go('browsers')">查看状态</el-button>
          </div>
        </template>
        <div class="lane-list">
          <div v-for="lane in lanes" :key="lane.key" class="lane-row">
            <span>
              <strong>{{ lane.label }}</strong>
              <small class="mono">{{ lane.key }}</small>
            </span>
            <span>
              <el-tag :type="lane.status === 'running' ? 'success' : 'info'" size="small" effect="plain">
                {{ lane.status === "running" ? "运行中" : lane.status === "stopped" ? "已停止" : lane.status }}
              </el-tag>
              <small v-if="lane.viewers" class="muted"> VNC {{ lane.viewers }}</small>
            </span>
          </div>
          <p v-if="!lanes.length" class="muted">暂无车道数据。</p>
        </div>
      </el-card>

      <el-card v-if="invalidSites" shadow="never" class="panel warning-panel">
        <strong>{{ invalidSites }} 个站点工作流无效</strong>
        <p class="muted">进入站点管理检查步骤配置和模板引用。</p>
        <el-button size="small" @click="go('sites')">处理</el-button>
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.page-head {
  margin-bottom: 22px;
}

.eyebrow {
  margin: 0 0 6px;
  color: #409eff;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.16em;
}

.page-head h1 {
  margin: 0 0 6px;
  font-size: 26px;
}

.page-head p {
  margin: 0;
}

.notice {
  margin-bottom: 14px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 18px;
}

.stat-card {
  text-align: left;
  border: 1px solid #e4e9f0;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.82);
  padding: 16px;
  cursor: pointer;
  color: inherit;
  transition: border-color 0.15s ease, transform 0.15s ease;
}

.stat-card:hover {
  border-color: #b9d7fb;
  transform: translateY(-1px);
}

.stat-label {
  display: block;
  color: #7a8794;
  font-size: 12px;
}

.stat-card strong {
  display: block;
  margin: 6px 0 2px;
  font-size: 28px;
}

.stat-card small {
  color: #8a96a3;
  font-size: 11px;
}

.panel {
  border-radius: 16px;
  border-color: #e4e9f0;
  margin-bottom: 18px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.site-list,
.lane-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.site-row,
.lane-row {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px;
  border: 1px solid #edf1f6;
  border-radius: 10px;
  background: #fbfcfe;
  color: inherit;
  text-align: left;
}

.site-row {
  cursor: pointer;
}

.site-row:hover {
  border-color: #b9d7fb;
}

.site-row strong,
.lane-row strong {
  display: block;
  font-size: 13px;
}

.site-row small,
.lane-row small {
  display: block;
  margin-top: 2px;
  color: #8a96a3;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}

.site-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.warning-panel {
  border-color: #f3d19e;
  background: #fffbf2;
}

@media (max-width: 1000px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 600px) {
  .stat-grid {
    grid-template-columns: 1fr;
  }

  .site-row,
  .lane-row {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
