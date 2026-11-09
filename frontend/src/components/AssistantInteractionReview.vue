<script setup lang="ts">
import { computed } from "vue";
import { interactionReviewRows, isSiteConfigReview, reviewFieldLabels, reviewValue } from "@/utils/assistant-review";

const props = defineProps<{ purpose: string; review: Record<string, unknown> }>();
const rows = computed(() => interactionReviewRows(props.purpose, props.review));
const config = computed(() => props.purpose === "site_config" && isSiteConfigReview(props.review) ? props.review : null);
const trustedUrls = computed(() => rows.value.some(row => row.field === "trusted_urls") || config.value?.changes.some(change => change.field === "trusted_urls"));
const observationTrust = computed(() => config.value?.changes.some(change => change.field === "observation.trusted_origins"));
</script>

<template>
  <section class="interaction-review" aria-label="授权核对内容">
    <dl class="review-fields">
      <div v-for="row in rows" :key="row.field" :class="{ 'review-major': row.field === 'name' || row.field === 'base_url' }">
        <dt>{{ row.label }}</dt><dd><span v-for="(value, index) in row.values" :key="index" class="review-value">{{ value }}</span></dd>
      </div>
    </dl>
    <p v-if="purpose === 'trusted_origins' || observationTrust" class="review-warning">仅允许跳转并观察所列来源，不转发凭据，也不授权完整运行访问这些来源。</p>
    <p v-if="trustedUrls" class="review-warning">可信 URL 正则仅作用于本站点，扩展登录凭据和 TOTP 的使用范围，不改变其他权限。每条 Python 正则全匹配完整 HTTP(S) URL（含路径、查询和片段）；宽泛规则可能向不可信页面泄露凭据。请逐条核对原始正则。</p>
    <template v-if="config">
      <div class="review-changes" aria-label="站点配置变更">
        <article v-for="change in config.changes" :key="change.field" class="review-change">
          <h4>{{ reviewFieldLabels[change.field] }}</h4>
          <dl class="review-comparison">
            <div><dt>变更前</dt><dd><span v-for="(value, index) in reviewValue(change.field, change.before)" :key="index" class="review-value">{{ value }}</span></dd></div>
            <div><dt>变更后</dt><dd><span v-for="(value, index) in reviewValue(change.field, change.after)" :key="index" class="review-value">{{ value }}</span></dd></div>
          </dl>
        </article>
      </div>
      <p v-if="config.successful_run_id" class="review-evidence">成功测试任务：#{{ config.successful_run_id }}（不代表已保存本次变更）</p>
      <ul v-if="config.warnings.length" class="review-warning" aria-label="变更风险提示"><li v-for="(warning, index) in config.warnings" :key="index">{{ warning }}</li></ul>
    </template>
    <p v-else-if="purpose === 'site_config'" class="review-warning" role="alert">站点配置核对内容格式无效，请刷新核对，不要批准。</p>
    <p v-else-if="!rows.length" class="muted">暂无可展示的公开核对字段，请结合请求说明核对。</p>
  </section>
</template>

<style scoped>
.interaction-review { min-width: 0; max-width: 100%; color: var(--el-text-color-primary, #303133); }
.review-fields, .review-comparison { display: grid; gap: 10px; margin: 12px 0; }
.review-fields > div, .review-comparison > div { min-width: 0; }
dt { color: var(--el-text-color-secondary, #70757e); font-size: 12px; }
dd { min-width: 0; margin: 4px 0 0; }
.review-value { display: block; max-width: 100%; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
.review-major dd { font-size: 14px; font-weight: 600; }
.review-changes { display: grid; gap: 10px; min-width: 0; }
.review-change { min-width: 0; padding: 10px 12px; border: 1px solid var(--el-border-color, #dcdfe6); border-radius: 8px; background: var(--el-bg-color, #fff); }
h4 { margin: 0; font-size: 13px; }
.review-comparison { grid-template-columns: repeat(2, minmax(0, 1fr)); margin-bottom: 0; }
.review-warning { color: var(--el-color-warning-dark-2, #854d0e); line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
ul.review-warning { padding-left: 20px; }
.review-evidence { color: var(--el-text-color-secondary, #70757e); }
@media (max-width: 720px) { .review-comparison { grid-template-columns: minmax(0, 1fr); } }
</style>
