<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { consumptionFeatureLabels, interactionReviewRows, isConsumptionReview, isCookieClearReview, isSiteConfigReview, templateImportReview, templateWorkflowDifferences, templateReviewValue, isSchedulerConfigReview, runCancelReview, reviewFieldLabels, reviewValue, schedulerFieldLabels, schedulerReviewValue, runStatusLabels } from "@/utils/assistant-review";

const props = defineProps<{ purpose: string; review: Record<string, unknown> }>();
const rows = computed(() => interactionReviewRows(props.purpose, props.review));
const config = computed(() => props.purpose === "site_config" && isSiteConfigReview(props.review) ? props.review : null);
const consumption = computed(() => props.purpose === "consumption" && isConsumptionReview(props.review) ? props.review : null);
const templateImport = computed(() => props.purpose === "template_import" ? templateImportReview(props.review) : null);
const scheduler = computed(() => props.purpose === "scheduler_config" && isSchedulerConfigReview(props.review) ? props.review : null);
const cancellation = computed(() => props.purpose === "run_cancel" ? runCancelReview(props.review) : null);
const cookieClear = computed(() => props.purpose === "cookie_clear" && isCookieClearReview(props.review) ? props.review : null);
const trustedUrls = computed(() => rows.value.some(row => row.field === "trusted_urls") || config.value?.changes.some(change => change.field === "trusted_urls"));
const observationTrust = computed(() => config.value?.changes.some(change => change.field === "observation.trusted_origins"));
const candidateOpen = ref(false), differencesOpen = ref(false);
watch(() => props.review, () => { candidateOpen.value = false; differencesOpen.value = false; });
const candidateText = computed(() => candidateOpen.value && templateImport.value?.candidate ? templateReviewValue(templateImport.value.candidate) : "");
const differences = computed(() => differencesOpen.value && templateImport.value ? templateWorkflowDifferences(templateImport.value) : null);
const differenceLabels = { parent_id: "父模板", steps: "步骤", groups: "分组", signals: "信号" };
const differenceStatusLabels = { added: "新增", removed: "移除", updated: "更新", unchanged: "未变更" };
function toggleTemplateSection(section: "candidate" | "differences", event: Event) {
  const open = (event.currentTarget as HTMLDetailsElement).open;
  if (section === "candidate") candidateOpen.value = open;
  else differencesOpen.value = open;
}
</script>

<template>
  <section class="interaction-review" aria-label="授权核对内容">
    <dl class="review-fields">
      <div v-for="row in rows" :key="row.field" :class="{ 'review-major': row.field === 'name' || row.field === 'base_url' }">
        <dt>{{ row.label }}</dt><dd><span v-for="(value, index) in row.values" :key="index" class="review-value">{{ value }}</span></dd>
      </div>
    </dl>
    <p v-if="purpose === 'target'" class="review-warning">明确绑定已保存站点时，打开观察浏览器会在首次导航前自动恢复该站点保存的精确主机 Cookie（host / .host），适用于所有凭据模式。优先使用工作流绑定站点，否则使用凭据绑定站点；不会按主机查找其他站点，绑定失效也不会回退。Cookie 模式仍不授权使用账号、密码或 OTP；不会自动导出浏览器 Cookie。</p>
    <p v-if="purpose === 'trusted_origins' || observationTrust" class="review-warning">仅允许跳转并观察所列来源，不转发凭据，也不授权完整运行访问这些来源。</p>
    <p v-if="trustedUrls" class="review-warning">可信 URL 正则仅作用于本站点，扩展登录凭据和 TOTP 的使用范围，不改变其他权限。每条 Python 正则全匹配完整 HTTP(S) URL（含路径、查询和片段）；宽泛规则可能向不可信页面泄露凭据。请逐条核对原始正则。</p>
    <template v-if="cookieClear">
      <dl class="review-fields">
        <div><dt>当前已保存站点 ID</dt><dd>#{{ cookieClear.site_id }}</dd></div>
        <div class="review-major"><dt>网站名称</dt><dd class="review-value">{{ cookieClear.name }}</dd></div>
        <div class="review-major"><dt>网站地址</dt><dd class="review-value">{{ cookieClear.base_url }}</dd></div>
        <div><dt>当前共享浏览器车道</dt><dd>{{ reviewValue('lane_key', cookieClear.lane_key)[0] }}</dd></div>
        <div><dt>当前已保存 Cookie 数量</dt><dd>{{ cookieClear.saved_cookie_count }}</dd></div>
      </dl>
      <p class="review-warning" role="alert">批准将删除该站点保存的 Cookie，并仅清除当前共享车道中该精确主机的浏览器 Cookie（host 和 .host，所有路径）。同一车道中访问相同主机的其他标签页可能退出登录。</p>
      <p class="review-warning">不会清除父域、兄弟域或其他车道的 Cookie；账号、密码和 localStorage 保持不变。不会刷新页面；浏览器清除结果未知时不代表全部成功，也不会自动重试。</p>
      <ul v-if="cookieClear.warnings.length" class="review-warning" aria-label="Cookie 清除风险提示"><li v-for="(warning, index) in cookieClear.warnings" :key="index">{{ warning }}</li></ul>
    </template>
    <p v-else-if="purpose === 'cookie_clear'" class="review-warning" role="alert">Cookie 清除核对内容格式无效或范围不完整，不能批准。请拒绝并要求重新发起完整确认。</p>
    <template v-else-if="consumption">
      <p class="review-warning" role="alert">这是真实消费授权：运行会改变网站状态，购买补签卡或补签可能消耗积分、费用或补签卡。请核对下列每个步骤、数量、费用上限和全部日期。</p>
      <p class="review-warning">仅授权本次完整运行的一次性许可，不可复用，不开启自动重试；不会修改全局自动授权设置，全局默认授权也不能跳过本次消费确认。拒绝或超时不会运行。</p>
      <dl class="review-fields">
        <div><dt>运行站点</dt><dd>#{{ consumption.site_id }}</dd></div>
        <div><dt>日期计算时区</dt><dd>{{ consumption.timezone }}</dd></div>
        <div><dt>该时区今天</dt><dd>{{ consumption.today }}</dd></div>
      </dl>
      <div class="review-scope" role="region" aria-label="全部消费步骤与上限" tabindex="0">
        <ol class="review-steps">
          <li v-for="step in consumption.steps" :key="step.step_key" class="review-change">
            <h4>{{ consumptionFeatureLabels[step.feature_type] }}</h4>
            <p class="review-value">步骤标识：{{ step.step_key }}</p>
            <p v-if="step.label !== consumptionFeatureLabels[step.feature_type]" class="review-value">步骤名称：{{ step.label }}</p>
            <dl v-if="step.feature_type === 'post.buy_card'" class="review-comparison">
              <div><dt>购买数量策略</dt><dd>{{ step.purchase_quantity === 0 ? '自动补齐（配置数量 0）：按待补签天数减去已有卡数，不小于 0' : `固定购买 ${step.purchase_quantity} 张，即使没有待补签日期也会购买` }}</dd></div>
              <div><dt>最多购买</dt><dd>{{ step.maximum_quantity }} 张</dd></div>
              <div><dt>单张价格上限</dt><dd>{{ step.max_price }}（网站积分或计价单位）</dd></div>
              <div><dt>本步骤总消费上限</dt><dd>{{ step.maximum_spend }}（网站积分或计价单位）</dd></div>
            </dl>
          </li>
        </ol>
      </div>
      <h4>全部批准补签日期（{{ consumption.date_count }} 天）</h4>
      <p v-if="!consumption.dates.length" class="review-warning">无待补签日期（0 天）；固定数量购卡步骤仍可能购买。</p>
      <div v-else class="review-scope" role="region" aria-label="全部批准补签日期" tabindex="0">
        <ol class="review-dates"><li v-for="day in consumption.dates" :key="day"><time :datetime="day">{{ day }}</time></li></ol>
      </div>
      <p class="review-warning">上述数量与费用为配置上限，不是网站实时报价；每个购卡步骤分别列出上限。条件步骤也纳入审核，普通点击的业务效果无法仅凭选择器识别。</p>
      <ul class="review-warning" aria-label="消费风险提示"><li v-for="(warning, index) in consumption.warnings" :key="index">{{ warning }}</li></ul>
    </template>
    <p v-else-if="purpose === 'consumption'" class="review-warning" role="alert">消费核对内容格式无效或缺少风险与范围信息，不能批准。请拒绝并让助手重新发起完整确认。</p>
    <template v-else-if="config">
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
    <template v-else-if="templateImport">
      <p class="review-warning">仅将来源站点的工作流步骤、分组与信号导入模板，不复制站点配置、凭据或 Cookie，也不会运行任务。更新模板可能影响所有引用它的站点，请核对目标与父模板。</p>
      <p class="review-warning">本地工作流整体替换，不会隐式合并旧内容。后端禁止删除、重命名或重排原有效分组，禁止删除、移动到其他分组或重排原有效步骤；允许在任意位置插入新分组和步骤，已有步骤的内容与配置可更新。顺序约束比较原有项目的相对顺序，不要求 position 数值相等。</p>
      <dl class="review-fields">
        <div><dt>来源站点</dt><dd class="review-value">{{ templateImport.source_site.name }} (#{{ templateImport.source_site.id }})</dd></div>
        <div><dt>目标模板</dt><dd class="review-value">{{ templateImport.destination.name }}<span v-if="templateImport.destination.id"> (#{{ templateImport.destination.id }})</span></dd></div>
        <div><dt>操作</dt><dd>{{ templateImport.destination.operation === 'create' ? '创建模板' : '更新已有模板' }}</dd></div>
        <div><dt>父模板</dt><dd class="review-value">{{ templateImport.parent ? `${templateImport.parent.name} (#${templateImport.parent.id})` : '无父模板' }}</dd></div>
        <div><dt>导入工作流数量</dt><dd>步骤：{{ templateImport.counts.steps }} · 分组：{{ templateImport.counts.groups }} · 信号：{{ templateImport.counts.signals }}</dd></div>
        <div v-if="templateImport.description !== undefined"><dt>模板说明</dt><dd class="review-value">{{ templateImport.description || '空值' }}</dd></div>
        <div v-if="templateImport.enabled !== undefined"><dt>启用模板</dt><dd>{{ templateImport.enabled ? '是' : '否' }}</dd></div>
      </dl>
      <p v-if="templateImport.before === undefined" class="review-warning">旧版待审批记录：无详细基线，无法生成差异；不推测原工作流。{{ templateImport.candidate ? '仍可核对完整本地候选。' : '此记录仅有范围摘要，无完整本地候选。' }}</p>
      <details v-if="templateImport.candidate" :open="candidateOpen" class="template-details" @toggle="toggleTemplateSection('candidate', $event)">
        <summary>完整本地候选（步骤配置、分组顺序与锚点、信号、父模板）</summary>
        <div v-if="candidateOpen" class="review-scope template-scope" role="region" aria-label="完整本地候选" tabindex="0">
          <p>以下为来源站点的实际本地候选，保留数组顺序及 position / step_anchor 原值；不是合并结果，也不是展开继承后的有效工作流。</p>
          <pre class="review-value">{{ candidateText }}</pre>
        </div>
      </details>
      <details v-if="templateImport.candidate && templateImport.before !== undefined" :open="differencesOpen" class="template-details" @toggle="toggleTemplateSection('differences', $event)">
        <summary>本地工作流差异（旧值 / 新值）</summary>
        <div v-if="differencesOpen && differences" class="review-scope template-scope" role="region" aria-label="本地工作流差异" tabindex="0">
          <p>{{ templateImport.before === null ? '创建模板：无原目标工作流，以下项目为新候选。' : '仅比较原目标模板的本地工作流与新候选，不含继承展开；不代表后端有效结构检查结果。' }}</p>
          <p>按标识比较全部项目。position / step_anchor 是原始字段差异；array_order 仅比较两侧共有项目的数组相对顺序，不将插入新项目视为重排。</p>
          <p v-if="differences.every(item => item.status === 'unchanged')">无差异：本地工作流未变更。</p>
          <article v-for="item in differences" :key="`${item.section}:${item.key}`" class="review-change">
            <h4>{{ differenceLabels[item.section] }} · {{ item.key }} · {{ differenceStatusLabels[item.status] }}</h4>
            <p v-if="!item.fields.length">未变更</p>
            <div v-for="field in item.fields" :key="field.field">
              <h4 class="review-value">{{ field.field }}</h4>
              <dl class="review-comparison">
                <div><dt>旧值</dt><dd><pre class="review-value">{{ templateReviewValue(field.before) }}</pre></dd></div>
                <div><dt>新值</dt><dd><pre class="review-value">{{ templateReviewValue(field.after) }}</pre></dd></div>
              </dl>
            </div>
          </article>
        </div>
      </details>
      <ul v-if="templateImport.warnings.length" class="review-warning" aria-label="模板导入风险提示"><li v-for="(warning, index) in templateImport.warnings" :key="index">{{ warning }}</li></ul>
    </template>
    <template v-else-if="scheduler">
      <p class="review-warning">这是全局调度变更，不仅影响当前站点。仅保存以下差异，不代表已经启动或取消任务。</p>
      <dl class="review-fields"><div><dt>适用范围</dt><dd>全局调度器</dd></div><div><dt>时区</dt><dd>{{ scheduler.timezone }}</dd></div><div><dt>当前修订</dt><dd>{{ scheduler.revision }}</dd></div></dl>
      <div class="review-changes" aria-label="全局调度变更">
        <article v-for="change in scheduler.changes" :key="change.field" class="review-change">
          <h4>{{ schedulerFieldLabels[change.field] }}</h4>
          <dl class="review-comparison">
            <div><dt>变更前</dt><dd><span v-for="(value, index) in schedulerReviewValue(change.field, change.before)" :key="index" class="review-value">{{ value }}</span></dd></div>
            <div><dt>变更后</dt><dd><span v-for="(value, index) in schedulerReviewValue(change.field, change.after)" :key="index" class="review-value">{{ value }}</span></dd></div>
          </dl>
        </article>
      </div>
      <ul v-if="scheduler.warnings.length" class="review-warning" aria-label="全局调度风险提示"><li v-for="(warning, index) in scheduler.warnings" :key="index">{{ warning }}</li></ul>
    </template>
    <template v-else-if="cancellation">
      <p class="review-warning">批准只会请求取消任务，不代表任务已停止。正在执行的操作可能继续完成，已产生的业务副作用不会撤销；请在运行记录核对最终状态。</p>
      <dl class="review-fields">
        <div><dt>目标任务</dt><dd>#{{ cancellation.run_id }}</dd></div>
        <div><dt>当前状态</dt><dd>{{ Object.hasOwn(runStatusLabels, cancellation.status) ? runStatusLabels[cancellation.status] : '状态待核对' }}（{{ cancellation.status }}）</dd></div>
        <div><dt>涉及站点数</dt><dd>{{ cancellation.site_count === null ? '记录已截断，无法确定总数' : cancellation.site_count }}</dd></div>
      </dl>
      <div v-if="cancellation.jobs?.length" class="review-scope" role="region" aria-label="任务中的站点" tabindex="0">
        <ul><li v-for="job in cancellation.jobs" :key="job.id" class="review-value">{{ job.site_name }} · {{ job.site_id === null ? '站点已删除' : `站点 #${job.site_id}` }} · {{ Object.hasOwn(runStatusLabels, job.status) ? runStatusLabels[job.status] : job.status }}</li></ul>
      </div>
      <p v-if="cancellation.jobs_truncated" class="review-warning">仅展示前 50 条站点任务；取消范围是整个指定任务，而非仅以上列表。</p>
      <ul v-if="cancellation.warnings.length" class="review-warning" aria-label="取消任务风险提示"><li v-for="(warning, index) in cancellation.warnings" :key="index">{{ warning }}</li></ul>
    </template>
    <p v-else-if="['template_import', 'scheduler_config', 'run_cancel'].includes(purpose)" class="review-warning" role="alert">公开核对内容格式无效或范围不完整，不能批准。请拒绝并要求重新发起完整确认。</p>
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
.review-scope { max-height: 240px; overflow: auto; margin: 12px 0; border: 1px solid var(--el-border-color, #dcdfe6); border-radius: 8px; }
.review-steps { display: grid; gap: 10px; padding: 0 12px 0 32px; }
.review-dates { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px 24px; padding: 0 12px 0 40px; }
.review-scope:focus-visible { outline: 2px solid var(--el-color-primary, #409eff); outline-offset: 2px; }
.template-details { min-width: 0; margin: 12px 0; }
.template-details > summary { cursor: pointer; overflow-wrap: anywhere; }
.template-scope { max-height: min(60vh, 560px); padding: 12px; }
.template-scope .review-change + .review-change { margin-top: 10px; }
.template-scope pre { margin: 6px 0; font-size: 12px; }
@media (max-width: 720px) { .review-comparison { grid-template-columns: minmax(0, 1fr); } }
</style>
