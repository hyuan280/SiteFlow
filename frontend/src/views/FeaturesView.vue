<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { ApiError, apiRequest } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import type { Feature } from "@/types/workflow";
import type { JsonSchema } from "@/utils/schema";
import { categoryLabel, featureSupportsLlm } from "@/utils/workflow";
import FeatureSettingsEditor from "@/components/FeatureSettingsEditor.vue";

const auth = useAuthStore();
const router = useRouter();
const features = ref<Feature[]>([]);
const loading = ref(true);
const error = ref("");
const activeCategory = ref("all");

function updateFeature(feature: Feature): void {
  features.value = features.value.map(item => item.key === feature.key ? feature : item);
}

const categories = computed(() => {
  const values = Array.from(new Set(features.value.map((item) => item.category)));
  return ["all", ...values];
});

const visibleFeatures = computed(() =>
  activeCategory.value === "all"
    ? features.value
    : features.value.filter((item) => item.category === activeCategory.value),
);

function requiredFields(schema: JsonSchema): string[] {
  return (schema.required ?? []).map((key) => schema.properties?.[key]?.title ?? key);
}

function defaultEntries(schema: JsonSchema): { key: string; value: unknown }[] {
  return Object.entries(schema.properties ?? {})
    .filter(([, property]) => Object.prototype.hasOwnProperty.call(property, "default"))
    .map(([key, property]) => ({ key, value: property.default }));
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value || "（空字符串）";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "[]";
  if (value !== null && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    features.value = await apiRequest<Feature[]>("/features");
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 401) {
      auth.clear();
      await router.replace({ name: "login" });
      return;
    }
    error.value = cause instanceof Error ? cause.message : "加载功能目录失败";
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <section class="page-head">
    <div>
      <p class="eyebrow">FEATURES</p>
      <h1>功能管理</h1>
      <p class="muted">功能类型由后端代码注册；可编辑全局默认值、必填策略及支持功能的执行行为，不能修改字段类型及代码约束。</p>
    </div>
  </section>

  <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="notice" />
  <el-alert title="这里编辑的是全局默认设置，不是单个步骤配置。已有步骤中显式填写的值（包括创建时写入的默认值）不受影响；仅缺省字段使用当前全局默认值。必填策略变更可能影响后续验证。" type="warning" :closable="false" show-icon class="notice" />
  <el-alert title="验证码识别方式可在步骤中覆盖，功能管理只设置默认值；视觉模型提示词属于执行行为，仅在功能管理修改，步骤编辑器不提供覆盖。" type="info" :closable="false" show-icon class="notice" />

  <div class="category-filter">
    <el-button
      v-for="category in categories"
      :key="category"
      size="small"
      :type="activeCategory === category ? 'primary' : 'default'"
      @click="activeCategory = category"
    >
      {{ category === "all" ? "全部" : categoryLabel(category) }}
    </el-button>
  </div>

  <div class="feature-grid" v-loading="loading">
    <article v-for="feature in visibleFeatures" :key="feature.key" class="feature-card">
      <div class="feature-head">
        <div>
          <h2>{{ feature.name }}</h2>
          <p class="mono">{{ feature.key }}</p>
        </div>
        <div class="feature-tags">
          <el-tag size="small" :type="feature.customized ? 'warning' : 'info'">{{ feature.customized ? "已自定义" : "代码默认" }}</el-tag>
          <el-tag v-if="featureSupportsLlm(feature)" size="small" type="success" effect="plain">大模型</el-tag>
          <el-tag v-if="feature.source && feature.source !== 'builtin'" size="small" type="warning" effect="plain">
            插件 · {{ feature.source }}
          </el-tag>
          <el-tag size="small" effect="plain">{{ categoryLabel(feature.category) }}</el-tag>
          <el-tag size="small" effect="plain">
            {{ feature.cardinality === "single" ? "单例" : "多例" }}
          </el-tag>
        </div>
      </div>

      <p class="muted description">{{ feature.description }}</p>
      <p v-if="feature.requires.length" class="requirements">
        依赖条件：
        <el-tag v-for="item in feature.requires" :key="item" size="small" type="warning" effect="plain">
          {{ item }}
        </el-tag>
      </p>

      <div class="field-summary">
        <div>
          <span class="label">必填字段</span>
          <span v-if="requiredFields(feature.config_schema).length">
            {{ requiredFields(feature.config_schema).join("、") }}
          </span>
          <span v-else class="muted">无</span>
        </div>
        <div>
          <span class="label">默认值</span>
          <span v-if="defaultEntries(feature.config_schema).length" class="mono defaults">
            {{ defaultEntries(feature.config_schema).map((item) => `${item.key}=${formatValue(item.value)}`).join(" · ") }}
          </span>
          <span v-else class="muted">无</span>
        </div>
      </div>

      <FeatureSettingsEditor :feature="feature" @updated="updateFeature" />
      <el-collapse class="schema-collapse">
        <el-collapse-item title="查看字段 Schema" :name="feature.key">
          <div class="schema-list">
            <div
              v-for="(property, key) in feature.config_schema.properties ?? {}"
              :key="key"
              class="schema-row"
            >
              <div>
                <strong>{{ property.title ?? key }}</strong>
                <span class="mono">{{ key }}</span>
                <el-tag v-if="(feature.config_schema.required ?? []).includes(key)" size="small" type="danger" effect="plain">
                  必填
                </el-tag>
              </div>
              <p>{{ property.description ?? "无描述" }}</p>
              <p class="muted mono">
                type={{ property.type ?? "-" }}
                <template v-if="'default' in property"> · default={{ formatValue(property.default) }}</template>
                <template v-if="property.enum"> · enum={{ property.enum.join(" / ") }}</template>
                <template v-if="property.minimum !== undefined"> · min={{ property.minimum }}</template>
                <template v-if="property.maximum !== undefined"> · max={{ property.maximum }}</template>
                <template v-if="property.minLength !== undefined"> · minLength={{ property.minLength }}</template>
                <template v-if="property.maxLength !== undefined"> · maxLength={{ property.maxLength }}</template>
              </p>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </article>
  </div>
</template>

<style scoped>
.page-head {
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

.notice {
  margin-bottom: 14px;
}

.category-filter {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.feature-card {
  border: 1px solid #e4e9f0;
  border-radius: 14px;
  padding: 16px;
  background: rgba(255, 255, 255, 0.86);
}

.feature-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.feature-head h2 {
  margin: 0;
  font-size: 16px;
}

.feature-head p {
  margin: 3px 0 0;
  color: #8a96a3;
  font-size: 11px;
}

.feature-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.description {
  margin: 10px 0;
  font-size: 13px;
  line-height: 1.6;
}

.requirements {
  margin: 8px 0;
  font-size: 12px;
}

.requirements :deep(.el-tag) {
  margin-left: 4px;
}

.field-summary {
  border-top: 1px solid #edf1f6;
  padding-top: 10px;
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12px;
}

.label {
  display: inline-block;
  min-width: 58px;
  color: #7a8794;
  font-weight: 600;
}

.defaults {
  color: #5d6b79;
  overflow-wrap: anywhere;
}

.schema-collapse {
  margin-top: 8px;
}

.schema-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.schema-row {
  padding: 8px 10px;
  border-radius: 8px;
  background: #f7f9fc;
}

.schema-row > div {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.schema-row .mono {
  color: #8a96a3;
  font-size: 11px;
}

.schema-row p {
  margin: 4px 0 0;
  font-size: 12px;
  line-height: 1.5;
}

@media (max-width: 900px) {
  .feature-grid {
    grid-template-columns: 1fr;
  }
}
</style>
