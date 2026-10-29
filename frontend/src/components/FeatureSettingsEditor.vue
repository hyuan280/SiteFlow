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
  if (executionSettings.value && (!executionSettings.value.llm_prompt.trim() || [...executionSettings.value.llm_prompt].length > 4000)) return `${isImageQuiz.value ? "图片答题提示词" : "识别提示词"}必须为 1–4000 字符`;
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
      <p class="muted">仅编辑默认值和必填策略；字段名称、类型、枚举及代码约束不可修改。移除 JSON 字段会恢复该字段的代码默认值；完整重置请使用下方按钮。跨字段约束由服务端最终校验。</p>
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
          <p class="muted">仅在功能管理中配置，全局生效；不是步骤字段，不能在步骤中覆盖。</p>
          <el-alert
            v-if="isImageQuiz"
            title="图片答题需要系统设置中的大模型开启「支持视觉」；未配置或关闭时图片答题需要人工处理。"
            type="warning"
            :closable="false"
          />
          <el-form-item :label="isImageQuiz ? '图片答题提示词' : '视觉模型提示词'">
            <el-input v-model="executionSettings.llm_prompt" type="textarea" :rows="4" :maxlength="4000" />
          </el-form-item>
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
:deep(textarea) { font-family: monospace; }
</style>
