<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { clearVisionSettings, saveVisionSettings, testVisionSettings } from "@/api/client";
import type { VisionUpdate, VisionView } from "@/types/integrations";

const props = defineProps<{ vision: VisionView }>();
const emit = defineEmits<{ updated: [value: VisionView] }>();
const fields = ["base_url", "model", "api_key"] as const;
const form = ref({ base_url: "", model: "", api_key: "", supports_vision: true });
const busy = ref(false);
const result = ref<{ ok: boolean; message: string } | null>(null);
const allLocked = computed(
  () =>
    !props.vision.supports_vision.editable ||
    fields.every((key) => !props.vision[key].editable),
);
const clearDisabled = computed(
  () =>
    !props.vision.supports_vision.editable ||
    fields.some((key) => !props.vision[key].editable) ||
    !fields.some((key) => props.vision[key].source === "db") &&
      props.vision.supports_vision.source !== "db",
);
watch(() => props.vision, value => {
  form.value = {
    base_url: value.base_url.value ?? "",
    model: value.model.value ?? "",
    api_key: "",
    supports_vision: value.supports_vision.value,
  };
  result.value = null;
}, { immediate: true });

async function run(action: "save" | "clear" | "test") {
  busy.value = true;
  result.value = null;
  try {
    if (action === "test") {
      result.value = await testVisionSettings();
      return;
    }
    if (action === "clear") {
      emit("updated", (await clearVisionSettings()).vision);
    } else {
      const payload: VisionUpdate = {};
      for (const key of fields) {
        if (!props.vision[key].editable) continue;
        const value = form.value[key].trim();
        if (key === "api_key" && !value) continue;
        if (key !== "api_key" && value === props.vision[key].value) continue;
        if (!value) throw new Error("地址和模型不能为空；如需移除配置，请使用清除。");
        payload[key] = value;
      }
      if (
        props.vision.supports_vision.editable &&
        form.value.supports_vision !== props.vision.supports_vision.value
      ) {
        payload.supports_vision = form.value.supports_vision;
      }
      if (!Object.keys(payload).length) { ElMessage.info("配置没有变化"); return; }
      emit("updated", (await saveVisionSettings(payload)).vision);
    }
    ElMessage.success(action === "clear" ? "Vision 配置已清除" : "Vision 配置已保存");
  } catch (error) {
    result.value = { ok: false, message: error instanceof Error ? error.message : "操作失败" };
  } finally { busy.value = false; }
}
</script>

<template>
  <el-card shadow="never" class="vision-card">
    <template #header>
      <div class="card-head">
        <strong>大模型支持</strong>
        <el-tag :type="vision.available ? 'success' : 'info'" size="small">
          {{ vision.available ? "已启用" : "未配置" }}
        </el-tag>
      </div>
    </template>
    <el-form label-position="top" :disabled="busy" class="config-form">
      <el-form-item v-for="key in fields" :key="key">
        <template #label>
          {{ key === 'base_url' ? 'OpenAI 兼容 Base URL（以 /v1 结尾）' : key === 'model' ? '模型名称' : 'API Key' }}
          <el-tag size="small" :type="vision[key].source === 'env' ? 'warning' : 'info'">{{ vision[key].source === 'env' ? '环境变量锁定' : vision[key].source === 'db' ? '数据库' : '未配置' }}</el-tag>
        </template>
        <el-input v-model="form[key]" :disabled="!vision[key].editable" :type="key === 'api_key' ? 'password' : 'text'" :show-password="key === 'api_key'" :maxlength="key === 'base_url' ? 2048 : key === 'model' ? 256 : 4096" :placeholder="key === 'api_key' ? '留空保留已保存的密钥' : key === 'base_url' ? 'https://api.example.com/v1' : '支持图片输入的模型 ID'" />
        <span v-if="key === 'api_key'" class="muted">当前：{{ vision.api_key.configured ? vision.api_key.masked ?? '已配置（不显示原文）' : '未配置' }}</span>
        <span v-else-if="vision[key].error" class="error">{{ vision[key].error }}</span>
      </el-form-item>
    </el-form>
    <div class="vision-switch">
      <strong>支持视觉（可用于识别图片）</strong>
      <el-switch
        v-model="form.supports_vision"
        :disabled="!vision.supports_vision.editable"
      />
      <span :class="['switch-state', { 'is-on': form.supports_vision }]">
        {{ form.supports_vision ? "支持视觉识别" : "不支持视觉识别" }}
      </span>
    </div>
    <el-alert v-if="result" :title="result.message" :type="result.ok ? 'success' : 'error'" :closable="false" class="result" />
    <div class="actions">
      <el-button type="primary" :disabled="allLocked" :loading="busy" @click="run('save')">保存模型配置</el-button>
      <el-button :disabled="!vision.available" :loading="busy" @click="run('test')">测试已保存配置</el-button>
      <el-button type="danger" plain :disabled="clearDisabled" :loading="busy" @click="run('clear')">清除</el-button>
    </div>
    <p class="muted">
      测试使用已保存配置：开启支持视觉时发送测试图片，关闭时发送文本请求。环境变量锁定项不可修改；存在锁定项时不能整体清除。
    </p>
  </el-card>
</template>

<style scoped>
.vision-card { border-radius: 16px; border-color: #e4e9f0; margin-bottom: 18px; }
.card-head, .actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.card-head { justify-content: flex-start; }
.config-form { margin-top: 0; }
.vision-switch {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: 2px 0 16px;
}
.switch-state { color: #7a8794; font-size: 12px; font-weight: 600; }
.switch-state.is-on { color: #2f7d48; }
.result { margin-bottom: 14px; }
.muted, .error { font-size: 12px; overflow-wrap: anywhere; }
.error { color: #d9534f; }
</style>
