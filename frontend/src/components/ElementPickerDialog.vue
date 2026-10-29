<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { ApiError, apiRequest } from "@/api/client";
import VncViewer from "@/components/VncViewer.vue";

interface PickerSelection {
  selector: string;
  tag: string;
  text: string;
  id: string;
  name: string;
  url: string;
}

interface PickerSession {
  session_id: string;
  lane_key: string;
  url: string;
  current_url: string | null;
  picked: PickerSelection | null;
  expires_in: number;
}

const props = defineProps<{
  modelValue: boolean;
  baseUrl?: string;
  title?: string;
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: boolean): void;
  (event: "picked", value: string): void;
}>();

const url = ref("");
const session = ref<PickerSession | null>(null);
const vncOpen = ref(false);
const starting = ref(false);
const error = ref("");
let pollTimer: ReturnType<typeof setInterval> | null = null;
let closing = false;

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function closeSession(): Promise<void> {
  stopPolling();
  vncOpen.value = false;
  const current = session.value;
  session.value = null;
  if (!current || closing) return;
  closing = true;
  try {
    await apiRequest(`/picker/sessions/${current.session_id}`, { method: "DELETE" });
  } catch {
    // Session may already be expired or the lane may have been stopped.
  } finally {
    closing = false;
  }
}

async function poll(): Promise<void> {
  const current = session.value;
  if (!current) return;
  try {
    const response = await apiRequest<PickerSession>(
      `/picker/sessions/${current.session_id}`,
    );
    session.value = response;
    if (response.picked?.selector) {
      const selector = response.picked.selector;
      vncOpen.value = false;
      await closeSession();
      emit("picked", selector);
      emit("update:modelValue", false);
      ElMessage.success(`已拾取：${selector}`);
    }
  } catch (cause) {
    session.value = null;
    stopPolling();
    if (cause instanceof ApiError) {
      error.value = cause.message;
    } else {
      error.value = cause instanceof Error ? cause.message : "拾取会话已失效";
    }
  }
}

function startPolling(): void {
  stopPolling();
  pollTimer = setInterval(() => void poll(), 1200);
}

async function start(): Promise<void> {
  error.value = "";
  const value = url.value.trim();
  try {
    const parsed = new URL(value);
    if (!["http:", "https:"].includes(parsed.protocol)) {
      throw new Error();
    }
  } catch {
    error.value = "请填写有效的 http(s) 地址";
    return;
  }
  await closeSession();
  starting.value = true;
  try {
    const response = await apiRequest<PickerSession>("/picker/sessions", {
      method: "POST",
      body: JSON.stringify({ url: value, lane_key: "interaction" }),
    });
    session.value = response;
    vncOpen.value = true;
    startPolling();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "无法打开拾取页面";
  } finally {
    starting.value = false;
  }
}

function close(): void {
  emit("update:modelValue", false);
}

function handleClosed(): void {
  vncOpen.value = false;
  void closeSession();
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      url.value = props.baseUrl?.trim() ?? "";
      error.value = "";
      session.value = null;
    } else {
      vncOpen.value = false;
      void closeSession();
    }
  },
);

onBeforeUnmount(() => {
  void closeSession();
});
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title || '元素拾取器'"
    width="min(680px, 94vw)"
    destroy-on-close
    @update:model-value="emit('update:modelValue', $event)"
    @closed="handleClosed"
  >
    <div class="picker-body">
      <p class="picker-hint">
        打开页面后可通过 VNC 正常操作；按住 <strong>Alt / Option</strong> 点击元素即可拾取选择器。
      </p>
      <div class="picker-url">
        <el-input v-model="url" placeholder="https://example.com/login" />
        <el-button type="primary" :loading="starting" @click="start">打开页面</el-button>
      </div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <div v-if="session" class="picker-session">
        <el-tag type="success" effect="plain">页面已打开</el-tag>
        <span class="mono">{{ session.lane_key }}</span>
        <el-button size="small" @click="vncOpen = true">打开 VNC</el-button>
        <el-button size="small" plain @click="closeSession">关闭页面</el-button>
      </div>
      <p v-if="session" class="picker-current mono">
        {{ session.current_url || session.url }}
      </p>
    </div>
    <template #footer>
      <el-button @click="close">取消</el-button>
    </template>
  </el-dialog>

  <VncViewer
    v-model="vncOpen"
    :lane-key="session?.lane_key || 'interaction'"
    lane-label="普通交互 · 元素拾取"
  />
</template>

<style scoped>
.picker-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.picker-hint {
  margin: 0;
  color: #7a8794;
  font-size: 12px;
  line-height: 1.6;
}

.picker-url {
  display: flex;
  gap: 8px;
}

.picker-url :deep(.el-input) {
  flex: 1;
}

.picker-session {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.picker-current {
  color: #7a8794;
  font-size: 12px;
  overflow-wrap: anywhere;
}
</style>
