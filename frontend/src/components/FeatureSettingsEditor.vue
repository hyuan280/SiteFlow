<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { resetFeatureSettings, saveFeatureSettings } from "@/api/client";
import type { Feature, FeatureExecutionSettings } from "@/types/workflow";
import type { JsonSchema } from "@/utils/schema";
import SecretProviderSelect from "@/components/SecretProviderSelect.vue";

const props = defineProps<{ feature: Feature }>();
const emit = defineEmits<{ updated: [value: Feature] }>();
const text = ref("");
const required = ref<string[]>([]);
const executionSettings = ref<FeatureExecutionSettings | null>(null);
const isImageQuiz = computed(() => props.feature.key === "sign.image_quiz");
const isGridCaptcha = computed(() => props.feature.key === "challenge.grid_captcha");
const isResult = computed(() => props.feature.key === "flow.result");
const methodProperty = computed(() => props.feature.config_schema.properties?.captcha_method);
const methodOptions = computed(() =>
  (methodProperty.value?.enum ?? []).map((item) => {
    const value = String(item);
    return { value, label: methodProperty.value?.enum_titles?.[value] ?? value };
  }),
);
const busy = ref(false);
const serverError = ref("");
watch(() => props.feature, feature => {
  text.value = JSON.stringify(feature.defaults, null, 2);
  required.value = [...feature.required_fields];
  executionSettings.value = feature.execution_settings ? { ...feature.execution_settings } : null;
  serverError.value = "";
}, { immediate: true });

const parsed = computed(() => {
  try {
    const value: unknown = JSON.parse(text.value);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("默认值必须是 JSON 对象");
    return { value: value as Record<string, unknown>, error: "" };
  } catch (error) {
    return { value: null, error: error instanceof Error ? error.message : "JSON 格式错误" };
  }
});

const captchaMethod = computed<string>({
  get() {
    const value = parsed.value.value?.captcha_method;
    return typeof value === "string" ? value : String(methodProperty.value?.default ?? "");
  },
  set(value) {
    if (!parsed.value.value) return;
    text.value = JSON.stringify({ ...parsed.value.value, captcha_method: value }, null, 2);
  },
});

const totpProvider = computed<string>({
  get: () => String(parsed.value.value?.totp_provider_id ?? "builtin.twofauth"),
  set(value) {
    if (parsed.value.value) text.value = JSON.stringify({ ...parsed.value.value, totp_provider_id: value }, null, 2);
  },
});

const methodDescription = computed(() => {
  const value = captchaMethod.value;
  return methodProperty.value?.enum_descriptions?.[value] ?? "";
});

function validate(value: unknown, schema: JsonSchema, path: string): string {
  if (schema.anyOf) {
    return schema.anyOf.some(option => !validate(value, option, path)) ? "" : `${path} 不符合允许的类型或约束`;
  }
  const type = schema.type;
  if (type === "null") return value === null ? "" : `${path} 必须为 null`;
  if (type === "string" && typeof value !== "string" || type === "boolean" && typeof value !== "boolean" ||
      (type === "integer" || type === "number") && (typeof value !== "number" || !Number.isFinite(value) || type === "integer" && !Number.isInteger(value)) ||
      type === "array" && !Array.isArray(value) || type === "object" && (!value || typeof value !== "object" || Array.isArray(value))) return `${path} 类型必须为 ${type}`;
  if (schema.enum && !schema.enum.some(item => JSON.stringify(item) === JSON.stringify(value))) return `${path} 必须为枚举值`;
  if (typeof value === "number" && (schema.minimum !== undefined && value < schema.minimum || schema.maximum !== undefined && value > schema.maximum || schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum || schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum)) return `${path} 超出数值范围`;
  if (typeof value === "string" && (schema.minLength !== undefined && [...value].length < schema.minLength || schema.maxLength !== undefined && [...value].length > schema.maxLength)) return `${path} 长度不符合约束`;
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems || schema.maxItems !== undefined && value.length > schema.maxItems) return `${path} 数组长度不符合约束`;
    for (const [index, item] of value.entries()) {
      const error = validate(item, schema.items ?? {}, `${path}[${index}]`);
      if (error) return error;
    }
  }
  return "";
}

const validationError = computed(() => {
  if (executionSettings.value && (!executionSettings.value.llm_prompt.trim() || [...executionSettings.value.llm_prompt].length > 4000)) return `${isResult.value ? "结果判断提示词" : isImageQuiz.value ? "图片答题提示词" : isGridCaptcha.value ? "九宫格验证码提示词" : "识别提示词"}必须为 1–4000 字符`;
  if (isResult.value && [...(executionSettings.value?.model ?? "")].length > 256) return "模型 ID 最多为 256 字符";
  if (!parsed.value.value) return parsed.value.error;
  const properties = props.feature.config_schema.properties ?? {};
  for (const [key, value] of Object.entries(parsed.value.value)) {
    if (!Object.hasOwn(properties, key)) return `未知字段：${key}`;
    const error = validate(value, properties[key]!, key);
    if (error) return error;
  }
  for (const key of props.feature.base_required_fields) {
    if (!required.value.includes(key) && !Object.hasOwn(parsed.value.value, key)) return `取消代码必填字段 ${key} 前，必须提供有效默认值`;
  }
  return "";
});

async function save() {
  if (validationError.value || !parsed.value.value) return;
  busy.value = true;
  serverError.value = "";
  try {
    emit("updated", await saveFeatureSettings(props.feature.key, parsed.value.value, required.value, executionSettings.value));
    ElMessage.success("全局功能默认设置已保存");
  } catch (error) { serverError.value = error instanceof Error ? error.message : "保存失败"; }
  finally { busy.value = false; }
}

async function reset() {
  try { await ElMessageBox.confirm("恢复代码默认值、必填策略和执行行为？已有步骤的显式配置不会改变；全局执行行为将恢复代码默认。", "重置全局设置", { type: "warning", confirmButtonText: "重置", cancelButtonText: "取消" }); }
  catch { return; }
  busy.value = true;
  serverError.value = "";
  try {
    emit("updated", await resetFeatureSettings(props.feature.key));
    ElMessage.success("已恢复代码默认设置");
  } catch (error) { serverError.value = error instanceof Error ? error.message : "重置失败"; }
  finally { busy.value = false; }
}
</script>

<template>
  <el-collapse>
    <el-collapse-item title="编辑全局设置" :name="feature.key">
      <p class="muted">编辑默认值、必填策略及受支持的全局执行行为；字段名称、类型、枚举及代码约束不可修改。移除 JSON 字段会恢复该字段的代码默认值；完整重置请使用下方按钮。跨字段约束由服务端最终校验。</p>
      <el-form label-position="top" :disabled="busy">
        <el-form-item v-if="feature.key === 'challenge.2fa'" label="2FA 提供商">
          <SecretProviderSelect v-model="totpProvider" capability="secret.totp" />
          <p class="muted">所有 2FA 提供商均必须填写服务名，用户名可选。每个步骤可以覆盖提供商。</p>
        </el-form-item>
        <template v-if="methodProperty">
          <h3>验证码识别方式</h3>
          <p class="muted">这里设置步骤默认值；每个验证码步骤可单独覆盖。系统新增识别方法后会直接出现在选项中。</p>
          <el-form-item label="默认识别方式">
            <el-select v-model="captchaMethod" class="method-select">
              <el-option
                v-for="option in methodOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
            <p v-if="methodDescription" class="method-hint">{{ methodDescription }}</p>
          </el-form-item>
        </template>
        <template v-if="executionSettings">
          <h3>执行行为</h3>
          <p v-if="isResult" class="muted">这里设置全局总开关、判断范围、模型及默认提示词。每个输出步骤还需单独开启模型判断，可自定义提示词；其余执行行为不能在步骤中覆盖。</p>
          <p v-else class="muted">仅在功能管理中配置，全局生效；不是步骤字段，不能在步骤中覆盖。</p>
          <template v-if="isResult">
            <el-form-item label="大模型结果判断总开关">
              <el-switch v-model="executionSettings.llm_enabled" aria-label="启用大模型结果判断" />
              <p class="muted">默认关闭。关闭时直接使用步骤的 output_value，保留各步骤的模型判断配置但不生效；开启后，仅对已开启“开启模型判断”的步骤按下方范围调用大模型。旧步骤未配置时默认不调用模型。</p>
            </el-form-item>
            <el-form-item label="使用视觉判断">
              <el-switch v-model="executionSettings.use_vision" :disabled="!executionSettings.llm_enabled" aria-label="使用视觉判断" />
              <p class="muted">默认开启：仅发送当前页面截图，不发送页面文本；系统设置中的大模型必须开启「支持视觉」（supports_vision）。关闭视觉后使用页面文本模式，不发送截图。</p>
            </el-form-item>
            <el-form-item label="判断范围">
              <el-radio-group v-model="executionSettings.judgment_scope" :disabled="!executionSettings.llm_enabled" class="result-scope" aria-label="结果判断范围">
                <el-radio value="success_only">仅复核输出 True 的步骤（默认）</el-radio>
                <el-radio value="all">判断所有已开启模型判断的输出步骤（True / False）</el-radio>
              </el-radio-group>
              <p class="muted">仅对已开启模型判断的步骤生效。范围按步骤显式配置、继承值或全局默认值中的有效 output_value 决定；仅复核 True 时，输出 False 的步骤保持原值。</p>
            </el-form-item>
            <el-form-item label="模型 ID 覆盖（可选）">
              <el-input v-model="executionSettings.model" :disabled="!executionSettings.llm_enabled" :maxlength="256" placeholder="留空使用系统默认模型" aria-label="结果判断模型 ID" />
              <p class="muted">最多 256 字符。直接填写模型 ID，无需注册；沿用系统大模型提供商的接口地址和 API 密钥，不切换提供商。</p>
            </el-form-item>
            <el-form-item label="结果判断提示词">
              <el-input v-model="executionSettings.llm_prompt" :disabled="!executionSettings.llm_enabled" type="textarea" :rows="4" :maxlength="4000" aria-label="结果判断提示词" />
              <p class="muted">步骤自定义提示词留空时使用这里的全局提示词；步骤也可继承模板提示词。</p>
            </el-form-item>
            <el-alert
              title="自定义提示词必须明确要求以 True 或 False 作结论。默认提示词检查是否已登录且已签到，仅输出 True/False；系统始终强制最终结果为 True/False。"
              type="warning"
              :closable="false"
            />
            <p class="muted">启用后，外部模型提供商会收到页面截图或页面文本，其中可能包含账号等敏感信息。模型未配置、不支持视觉、请求错误或无效回答会导致步骤失败，不会静默回退到原始输出或其他判断模式。</p>
          </template>
          <template v-else>
            <el-alert
              v-if="isImageQuiz"
              title="图片答题需要系统设置中的大模型开启「支持视觉」；未配置或关闭时图片答题需要人工处理。"
              type="warning"
              :closable="false"
            />
            <el-alert
              v-if="isGridCaptcha"
              title="九宫格验证码需要系统设置中的大模型开启「支持视觉」；未配置或关闭时九宫格验证码需要人工处理。"
              type="warning"
              :closable="false"
            />
            <el-form-item :label="isImageQuiz ? '图片答题提示词' : isGridCaptcha ? '九宫格验证码提示词' : '视觉模型提示词'">
              <el-input v-model="executionSettings.llm_prompt" type="textarea" :rows="4" :maxlength="4000" />
            </el-form-item>
          </template>
        </template>
        <h3>步骤默认值 / 必填策略</h3>
        <el-form-item label="默认值 JSON（不是步骤显式配置）">
          <el-input v-model="text" type="textarea" :rows="12" spellcheck="false" aria-label="全局默认值 JSON" />
        </el-form-item>
        <el-form-item label="必填字段（代码必填项取消前需提供默认值）">
          <el-checkbox-group v-model="required">
            <el-checkbox v-for="(property, key) in feature.config_schema.properties ?? {}" :key="key" :value="key">{{ property.title ?? key }} ({{ key }}){{ feature.base_required_fields.includes(key) ? ' · 代码必填' : '' }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <el-alert v-if="validationError || serverError" :title="validationError || serverError" type="error" :closable="false" class="notice" />
      <el-button type="primary" :loading="busy" :disabled="!!validationError" @click="save">保存全局设置</el-button>
      <el-button :loading="busy" :disabled="!feature.customized" @click="reset">恢复代码默认</el-button>
    </el-collapse-item>
  </el-collapse>
</template>

<style scoped>
.muted { font-size: 12px; line-height: 1.6; }
.notice { margin-bottom: 12px; }
.el-form { margin-top: 14px; }
.method-select { width: min(360px, 100%); }
.method-hint { margin: 4px 0 0; color: #7a8794; font-size: 12px; line-height: 1.5; }
.result-scope { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
.result-scope :deep(.el-radio) { height: auto; margin-right: 0; white-space: normal; }
.result-scope :deep(.el-radio__label) { line-height: 1.6; }
:deep(textarea) { font-family: monospace; }
</style>
