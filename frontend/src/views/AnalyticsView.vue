<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { ApiError, apiRequest } from "@/api/client";
import TrendChart from "@/components/TrendChart.vue";
import { useAuthStore } from "@/stores/auth";
import type {
  FailureItemView,
  FailuresView,
  MatrixCellView,
  MatrixView,
  TrendsView,
} from "@/types/analytics";
import type { SiteOption } from "@/types/workflow";
import { formatServerDateTime } from "@/utils/datetime";
import { laneLabel } from "@/utils/workflow";

const auth = useAuthStore();
const router = useRouter();
const days = ref(14);
const siteId = ref<number | undefined>(undefined);
const includeDisabled = ref(false);
const sites = ref<SiteOption[]>([]);
const matrix = ref<MatrixView | null>(null);
const trends = ref<TrendsView | null>(null);
const failures = ref<FailuresView | null>(null);
const overviewLoading = ref(true);
const failureLoading = ref(false);
const error = ref("");
const failureError = ref("");
const ALL_ABNORMAL_STATUSES =
  "fail,error,unknown,waiting_human,deferred,canceled,interrupted";
const failureStatus = ref(ALL_ABNORMAL_STATUSES);
const failureFeature = ref("");
const failureQuery = ref("");
const failurePage = ref(1);
const failurePageSize = 20;
const screenshotOpen = ref(false);
const activeFailure = ref<FailureItemView | null>(null);
const displayTimezone = ref("Asia/Shanghai");

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  success: "成功",
  fail: "失败",
  error: "错误",
  deferred: "已推迟",
  unknown: "未知",
  skipped: "跳过",
  waiting_human: "等待人工",
  canceled: "已取消",
  interrupted: "已中断",
};

const STATUS_OPTIONS = [
  { value: ALL_ABNORMAL_STATUSES, label: "全部未成功" },
  { value: "fail,error", label: "失败 / 错误" },
  { value: "waiting_human", label: "等待人工" },
  { value: "unknown", label: "未知" },
  { value: "deferred", label: "已推迟" },
  { value: "canceled,interrupted", label: "已取消 / 中断" },
];

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "success") return "success";
  if (status === "running" || status === "queued" || status === "deferred") return "warning";
  if (
    status === "waiting_human" ||
    status === "unknown" ||
    status === "interrupted" ||
    status === "fail" ||
    status === "error"
  ) {
    return "danger";
  }
  return "info";
}

function formatDate(value: string | null): string {
  return formatServerDateTime(value, displayTimezone.value);
}

async function loadTimezone(): Promise<void> {
  try {
    const settings = await apiRequest<{ timezone: string }>("/scheduler-settings");
    displayTimezone.value = settings.timezone || "Asia/Shanghai";
  } catch {
    // Keep the default display timezone.
  }
}

async function handleUnauthorized(cause: unknown): Promise<boolean> {
  if (cause instanceof ApiError && cause.status === 401) {
    auth.clear();
    await router.replace({ name: "login" });
    return true;
  }
  return false;
}

function querySuffix(): string {
  const params = new URLSearchParams();
  params.set("days", String(days.value));
  if (siteId.value !== undefined) params.set("site_id", String(siteId.value));
  return params.toString();
}

async function loadOverview(): Promise<void> {
  overviewLoading.value = true;
  error.value = "";
  try {
    const matrixParams = new URLSearchParams(querySuffix());
    matrixParams.set("include_disabled", includeDisabled.value ? "true" : "false");
    const [matrixData, trendsData] = await Promise.all([
      apiRequest<MatrixView>(`/analytics/matrix?${matrixParams.toString()}`),
      apiRequest<TrendsView>(`/analytics/trends?${querySuffix()}`),
    ]);
    matrix.value = matrixData;
    trends.value = trendsData;
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    error.value = cause instanceof Error ? cause.message : "加载运行分析失败";
  } finally {
    overviewLoading.value = false;
  }
}

async function loadFailures(): Promise<void> {
  failureLoading.value = true;
  failureError.value = "";
  try {
    const params = new URLSearchParams(querySuffix());
    if (failureStatus.value) params.set("status", failureStatus.value);
    if (failureFeature.value.trim()) params.set("feature_type", failureFeature.value.trim());
    if (failureQuery.value.trim()) params.set("q", failureQuery.value.trim());
    params.set("page", String(failurePage.value));
    params.set("page_size", String(failurePageSize));
    failures.value = await apiRequest<FailuresView>(`/analytics/failures?${params.toString()}`);
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
    failureError.value = cause instanceof Error ? cause.message : "加载失败分析失败";
  } finally {
    failureLoading.value = false;
  }
}

function refreshAll(): void {
  failurePage.value = 1;
  void Promise.all([loadOverview(), loadFailures()]);
}

function applyFailureFilters(): void {
  failurePage.value = 1;
  void loadFailures();
}

function resetFailureFilters(): void {
  failureStatus.value = ALL_ABNORMAL_STATUSES;
  failureFeature.value = "";
  failureQuery.value = "";
  failurePage.value = 1;
  void loadFailures();
}

function changeFailurePage(page: number): void {
  failurePage.value = page;
  void loadFailures();
}

function statusGlyph(status: string): string {
  if (status === "success") return "✓";
  if (status === "waiting_human") return "!";
  if (status === "unknown") return "?";
  if (status === "deferred") return "…";
  if (status === "fail" || status === "error") return "×";
  if (status === "canceled" || status === "interrupted") return "–";
  return "·";
}

function cellClass(cell: MatrixCellView | undefined): string {
  if (!cell) return "cell-empty";
  return `cell-${cell.status}`;
}

function cellTitle(siteName: string, date: string, cell: MatrixCellView | undefined): string {
  if (!cell) return `${siteName} · ${date}：未运行`;
  const counts = Object.entries(cell.counts)
    .map(([status, count]) => `${statusLabel(status)} ${count}`)
    .join("，");
  return [
    `${siteName} · ${date}`,
    `状态：${statusLabel(cell.status)}`,
    `尝试：${cell.attempts} 次`,
    counts ? `明细：${counts}` : "",
    cell.last_message ? `消息：${cell.last_message}` : "",
    cell.last_run_id ? "点击打开运行详情并定位站点" : "",
  ]
    .filter(Boolean)
    .join("\n");
}

function openCell(cell: MatrixCellView | undefined, siteId: number): void {
  if (!cell?.last_run_id) return;
  const query: Record<string, string> = {
    run: String(cell.last_run_id),
    site: String(siteId),
  };
  if (cell.last_job_id) query.job = String(cell.last_job_id);
  void router.push({ name: "runs", query });
}

function openRun(item: FailureItemView): void {
  const query: Record<string, string> = {
    run: String(item.run_id),
    job: String(item.job_id),
  };
  if (item.site_id !== null) query.site = String(item.site_id);
  void router.push({ name: "runs", query });
}

function openFailureScreenshot(item: FailureItemView): void {
  if (!item.screenshot_id) return;
  activeFailure.value = item;
  screenshotOpen.value = true;
}

function failedStepText(item: FailureItemView): string {
  if (!item.failed_steps.length) return "任务级失败";
  const first = item.failed_steps[0];
  const extra = item.failed_steps.length > 1 ? ` +${item.failed_steps.length - 1}` : "";
  return `${first.label}${extra}`;
}

const summaryCards = computed(() => {
  const data = trends.value;
  return [
    { label: "总执行", value: data?.total ?? 0, sub: `${days.value} 天` },
    {
      label: "成功率",
      value: `${data?.success_rate ?? 0}%`,
      sub: `成功 ${data?.success ?? 0} 次`,
    },
    {
      label: "失败/错误",
      value: (data?.fail ?? 0) + (data?.error ?? 0),
      sub: `未知 ${data?.unknown ?? 0} 次`,
    },
    {
      label: "等待人工",
      value: data?.waiting_human ?? 0,
      sub: `推迟 ${data?.deferred ?? 0} 次`,
    },
  ];
});

watch([days, siteId, includeDisabled], refreshAll);
watch(failureStatus, applyFailureFilters);

onMounted(async () => {
  void loadTimezone();
  try {
    sites.value = await apiRequest<SiteOption[]>("/sites?view=options");
  } catch (cause) {
    if (await handleUnauthorized(cause)) return;
  }
  refreshAll();
});
</script>

<template>
  <section class="page-head">
    <div>
      <p class="eyebrow">ANALYTICS</p>
      <h1>运行分析</h1>
      <p class="muted">站点每日状态、失败分析和执行趋势。</p>
    </div>
    <div class="head-actions">
      <el-radio-group v-model="days" size="small">
        <el-radio-button :value="7">7 天</el-radio-button>
        <el-radio-button :value="14">14 天</el-radio-button>
        <el-radio-button :value="30">30 天</el-radio-button>
        <el-radio-button :value="90">90 天</el-radio-button>
      </el-radio-group>
      <el-select
        v-model="siteId"
        clearable
        filterable
        placeholder="全部站点"
        size="small"
        class="site-filter"
      >
        <el-option
          v-for="site in sites"
          :key="site.id"
          :label="site.name"
          :value="site.id"
        />
      </el-select>
    </div>
  </section>

  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />

  <div class="summary-grid">
    <div v-for="card in summaryCards" :key="card.label" class="summary-card">
      <span>{{ card.label }}</span>
      <strong>{{ card.value }}</strong>
      <small>{{ card.sub }}</small>
    </div>
  </div>

  <el-card shadow="never" class="panel" v-loading="overviewLoading">
    <template #header>
      <div class="card-head">
        <div>
          <strong>状态矩阵</strong>
          <span class="muted">{{ matrix?.start_date }} ~ {{ matrix?.end_date }}</span>
        </div>
        <div class="card-actions">
          <el-checkbox v-model="includeDisabled">包含停用站点</el-checkbox>
          <el-button size="small" @click="loadOverview">刷新</el-button>
        </div>
      </div>
    </template>

    <div v-if="matrix?.sites.length" class="matrix-scroll">
      <table class="matrix-table">
        <thead>
          <tr>
            <th class="site-column">站点</th>
            <th v-for="date in matrix.dates" :key="date" :title="date">
              {{ date.slice(5) }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="site in matrix.sites" :key="site.site_id">
            <th class="site-column">
              <div class="site-name">
                <span
                  class="site-name-text"
                  :class="{ 'is-disabled': !site.enabled }"
                >
                  {{ site.name }}
                </span>
                <el-tag v-if="!site.enabled" size="small" type="info" effect="plain">
                  停用
                </el-tag>
              </div>
            </th>
            <td
              v-for="date in matrix.dates"
              :key="`${site.site_id}-${date}`"
              :class="['matrix-cell', cellClass(site.cells[date])]"
              :title="cellTitle(site.name, date, site.cells[date])"
              @click="openCell(site.cells[date], site.site_id)"
            >
              <span v-if="site.cells[date]">{{ statusGlyph(site.cells[date]!.status) }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="muted">暂无站点数据。</p>

    <div class="matrix-legend">
      <span><i class="legend-success"></i>成功</span>
      <span><i class="legend-fail"></i>失败/错误</span>
      <span><i class="legend-waiting"></i>等待人工</span>
      <span><i class="legend-unknown"></i>未知</span>
      <span><i class="legend-deferred"></i>推迟</span>
      <span><i class="legend-empty"></i>未运行</span>
    </div>
  </el-card>

  <el-card shadow="never" class="panel" v-loading="overviewLoading">
    <template #header>
      <div class="card-head">
        <div>
          <strong>执行趋势</strong>
          <span class="muted">柱状为状态数量，折线为成功率</span>
        </div>
      </div>
    </template>
    <TrendChart v-if="trends?.points.length" :points="trends.points" />
    <p v-else class="muted">暂无趋势数据。</p>
  </el-card>

  <el-card shadow="never" class="panel" v-loading="failureLoading">
    <template #header>
      <div class="card-head">
        <div>
          <strong>失败分析</strong>
          <span class="muted">每日运行状态异常的任务</span>
        </div>
        <span class="muted">共 {{ failures?.total ?? 0 }} 条</span>
      </div>
    </template>

    <div class="failure-filters">
      <el-select v-model="failureStatus" size="small" class="filter-status">
        <el-option
          v-for="option in STATUS_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>
      <el-input
        v-model="failureFeature"
        size="small"
        clearable
        placeholder="功能类型，例如 sign"
        class="filter-input"
        @keyup.enter="applyFailureFilters"
      />
      <el-input
        v-model="failureQuery"
        size="small"
        clearable
        placeholder="搜索站点、步骤或错误消息"
        class="filter-query"
        @keyup.enter="applyFailureFilters"
      />
      <el-button size="small" type="primary" @click="applyFailureFilters">查询</el-button>
      <el-button size="small" @click="resetFailureFilters">重置</el-button>
    </div>

    <el-alert
      v-if="failureError"
      :title="failureError"
      type="error"
      :closable="false"
      show-icon
      class="notice"
    />

    <el-table v-if="failures?.items.length" :data="failures.items" row-key="job_id">
      <el-table-column label="时间" min-width="160">
        <template #default="{ row }">{{ formatDate(row.finished_at) }}</template>
      </el-table-column>
      <el-table-column prop="site_name" label="站点" min-width="120" show-overflow-tooltip />
      <el-table-column label="车道" width="100">
        <template #default="{ row }">{{ laneLabel(row.lane) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small" effect="plain">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="失败步骤" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ failedStepText(row) }}</template>
      </el-table-column>
      <el-table-column label="错误消息" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">{{ row.message || "-" }}</template>
      </el-table-column>
      <el-table-column label="尝试" width="70" prop="attempt" />
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openRun(row)">详情</el-button>
          <el-button
            v-if="row.screenshot_id"
            link
            type="danger"
            @click="openFailureScreenshot(row)"
          >
            截图
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <p v-else class="muted">没有每日最终状态异常的站点记录。</p>

    <div v-if="failures && failures.total > failurePageSize" class="pagination-row">
      <el-pagination
        background
        layout="total, prev, pager, next"
        :total="failures.total"
        :page-size="failurePageSize"
        :current-page="failurePage"
        @current-change="changeFailurePage"
      />
    </div>
  </el-card>

  <el-dialog v-model="screenshotOpen" title="失败截图" width="min(1000px, 96vw)">
    <div v-if="activeFailure?.screenshot_id" class="screenshot-preview">
      <img
        :src="`/api/v1/artifacts/${activeFailure.screenshot_id}`"
        :alt="`${activeFailure.site_name} 失败截图`"
      />
      <p class="muted">
        {{ activeFailure.site_name }} · {{ formatDate(activeFailure.finished_at) }}
      </p>
    </div>
    <template #footer>
      <el-button @click="screenshotOpen = false">关闭</el-button>
      <el-button
        v-if="activeFailure?.screenshot_id"
        type="primary"
        @click="
          router.push({
            name: 'runs',
            query: {
              run: String(activeFailure.run_id),
              job: String(activeFailure.job_id),
              ...(activeFailure.site_id !== null
                ? { site: String(activeFailure.site_id) }
                : {}),
            },
          })
        "
      >
        打开运行详情
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 18px;
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

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.site-filter {
  width: 180px;
}

.notice {
  margin-bottom: 14px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.summary-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px;
  border: 1px solid #e4e9f0;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.86);
}

.summary-card span {
  color: #7a8794;
  font-size: 12px;
}

.summary-card strong {
  font-size: 26px;
}

.summary-card small {
  color: #8a96a3;
  font-size: 11px;
}

.panel {
  border-radius: 16px;
  border-color: #e4e9f0;
  margin-bottom: 16px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.card-head > div {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.matrix-scroll {
  overflow-x: auto;
  padding-bottom: 4px;
}

.matrix-table {
  border-collapse: separate;
  border-spacing: 2px;
  min-width: 100%;
}

.matrix-table th,
.matrix-table td {
  text-align: center;
  font-weight: 500;
}

.matrix-table thead th {
  color: #8a96a3;
  font-size: 11px;
  padding: 4px 2px;
}

.site-column {
  position: sticky;
  left: 0;
  z-index: 1;
  width: 1%;
  max-width: 30ch;
  padding: 6px 10px;
  background: #fff;
  box-shadow: 1px 0 0 #e4e9f0;
  text-align: left;
  color: #303133;
  font-size: 12px;
  white-space: nowrap;
}

.site-name {
  display: flex;
  align-items: center;
  gap: 5px;
  max-width: 30ch;
}

.site-name-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.site-column .is-disabled {
  color: #a8b0b9;
}

.site-column .el-tag {
  flex: none;
}

.matrix-table tbody tr:hover .site-column {
  background: #eaf4ff;
  color: #1f6fd1;
}

.matrix-table tbody tr:hover .matrix-cell {
  box-shadow: 0 0 0 1px rgba(64, 158, 255, 0.55);
}

.matrix-cell {
  position: relative;
  width: 26px;
  height: 26px;
  border-radius: 5px;
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  transition: transform 0.1s ease, box-shadow 0.1s ease;
}

.matrix-cell:hover {
  z-index: 2;
  transform: scale(1.05);
}

.cell-success {
  background: #67c23a;
}

.cell-fail,
.cell-error {
  background: #f56c6c;
}

.cell-waiting_human {
  background: #e6a23c;
}

.cell-unknown {
  background: #909399;
}

.cell-deferred {
  background: #409eff;
}

.cell-canceled,
.cell-interrupted {
  background: #c0c4cc;
}

.cell-empty {
  background: #f2f5f9;
  cursor: default;
}

.cell-empty:hover {
  transform: none;
}

.matrix-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 10px;
  color: #7a8794;
  font-size: 11px;
}

.matrix-legend span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.matrix-legend i {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 2px;
}

.legend-success {
  background: #67c23a;
}

.legend-fail {
  background: #f56c6c;
}

.legend-waiting {
  background: #e6a23c;
}

.legend-unknown {
  background: #909399;
}

.legend-deferred {
  background: #409eff;
}

.legend-empty {
  background: #f2f5f9;
  border: 1px solid #e4e9f0;
}

.failure-filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.filter-status {
  width: 150px;
}

.filter-input {
  width: 190px;
}

.filter-query {
  width: 240px;
}

.pagination-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.screenshot-preview {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
}

.screenshot-preview img {
  max-width: 100%;
  max-height: 66vh;
  border: 1px solid #e7edf5;
  border-radius: 8px;
}

.screenshot-preview p {
  margin: 0;
  font-size: 12px;
}

@media (max-width: 1000px) {
  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 600px) {
  .summary-grid {
    grid-template-columns: 1fr;
  }

  .site-filter,
  .filter-status,
  .filter-input,
  .filter-query {
    width: 100%;
  }
}
</style>
