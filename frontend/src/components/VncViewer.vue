<script setup lang="ts">
import RFB from "@novnc/novnc";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps<{
  modelValue: boolean;
  laneKey: string;
  laneLabel: string;
  embedded?: boolean;
}>();

const emit = defineEmits<{
  (event: "update:modelValue", value: boolean): void;
}>();

const screen = ref<HTMLDivElement | null>(null);
const state = ref<"idle" | "connecting" | "connected" | "disconnected" | "error">("idle");
const errorMessage = ref("");
let rfb: RFB | null = null;
let connectionGeneration = 0;

const stateLabel = computed(() => {
  switch (state.value) {
    case "connecting":
      return "连接中";
    case "connected":
      return "已连接";
    case "disconnected":
      return "已断开";
    case "error":
      return "连接失败";
    default:
      return "未连接";
  }
});

const stateType = computed(() => {
  if (state.value === "connected") return "success";
  if (state.value === "error") return "danger";
  if (state.value === "connecting") return "warning";
  return "info";
});

function disconnect(): void {
  connectionGeneration += 1;
  if (rfb) {
    try {
      rfb.disconnect();
    } catch {
      // ignore
    }
    rfb = null;
  }
  state.value = "idle";
}

async function connect(): Promise<void> {
  disconnect();
  const generation = connectionGeneration;
  errorMessage.value = "";
  state.value = "connecting";
  await nextTick();
  if (generation !== connectionGeneration || !props.modelValue) {
    return;
  }
  if (!screen.value) {
    state.value = "error";
    errorMessage.value = "VNC 容器未初始化";
    return;
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${window.location.host}/api/v1/lanes/${encodeURIComponent(props.laneKey)}/vnc`;
  try {
    const client = new RFB(screen.value, url, {
      credentials: { password: "" },
    });
    if (generation !== connectionGeneration || !props.modelValue) {
      client.disconnect();
      return;
    }
    rfb = client;
    client.background = "#0b1016";
    client.scaleViewport = true;
    client.resizeSession = false;
    client.focusOnClick = true;
    client.viewOnly = false;
    client.clipViewport = true;
    client.addEventListener("connect", () => {
      if (rfb !== client) return;
      state.value = "connected";
      errorMessage.value = "";
    });
    client.addEventListener("disconnect", () => {
      if (rfb !== client) return;
      if (state.value !== "error") {
        state.value = "disconnected";
      }
    });
    client.addEventListener("securityfailure", () => {
      if (rfb !== client) return;
      state.value = "error";
      errorMessage.value = "VNC 安全校验失败";
    });
    client.addEventListener("credentialsrequired", () => {
      // KasmVNC is exposed without VNC-level auth; the API WebSocket is authenticated.
    });
  } catch (error) {
    if (generation !== connectionGeneration) return;
    state.value = "error";
    errorMessage.value = error instanceof Error ? error.message : "无法建立 VNC 连接";
  }
}

function close(): void {
  disconnect();
  emit("update:modelValue", false);
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) {
      disconnect();
    } else if (props.embedded) {
      void connect();
    }
  },
);

onMounted(() => {
  if (props.embedded && props.modelValue) {
    void connect();
  }
});

onBeforeUnmount(disconnect);
</script>

<template>
  <div v-if="embedded" class="vnc-embedded">
    <div class="vnc-toolbar">
      <el-tag :type="stateType" effect="plain">{{ stateLabel }}</el-tag>
      <span class="vnc-lane mono">{{ laneKey }}</span>
      <el-button size="small" plain @click="connect">重新连接</el-button>
    </div>
    <div class="vnc-stage">
      <div ref="screen" class="vnc-screen"></div>
      <div v-if="state !== 'connected'" class="vnc-overlay">
        <div v-if="state === 'error'" class="vnc-error">
          <strong>无法连接 VNC</strong>
          <p>{{ errorMessage || "通道不可用，请刷新车道状态后重试" }}</p>
        </div>
        <div v-else-if="state === 'disconnected'">
          <strong>VNC 连接已断开</strong>
          <p>可以重新连接。</p>
        </div>
        <div v-else>
          <strong>正在连接 VNC…</strong>
          <p>首次连接可能需要几秒钟。</p>
        </div>
        <el-button type="primary" size="small" @click="connect">重新连接</el-button>
      </div>
    </div>
  </div>

  <el-dialog
    v-else
    :model-value="modelValue"
    :title="`VNC · ${laneLabel}`"
    width="92vw"
    top="4vh"
    destroy-on-close
    class="vnc-dialog"
    @update:model-value="emit('update:modelValue', $event)"
    @opened="connect"
    @closed="disconnect"
  >
    <div class="vnc-toolbar">
      <el-tag :type="stateType" effect="plain">{{ stateLabel }}</el-tag>
      <span class="vnc-lane mono">{{ laneKey }}</span>
      <el-button type="danger" plain size="small" @click="close">退出 VNC</el-button>
    </div>

    <div class="vnc-stage">
      <div ref="screen" class="vnc-screen"></div>
      <div v-if="state !== 'connected'" class="vnc-overlay">
        <div v-if="state === 'error'" class="vnc-error">
          <strong>无法连接 VNC</strong>
          <p>{{ errorMessage || "通道不可用，请刷新车道状态后重试" }}</p>
        </div>
        <div v-else-if="state === 'disconnected'">
          <strong>VNC 连接已断开</strong>
          <p>可以重新连接，或退出 VNC。</p>
        </div>
        <div v-else>
          <strong>正在连接 VNC…</strong>
          <p>首次连接可能需要几秒钟。</p>
        </div>
        <el-button type="primary" size="small" @click="connect">重新连接</el-button>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.vnc-embedded {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.vnc-embedded .vnc-stage {
  flex: 1;
  height: auto;
  min-height: 0;
}

.vnc-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.vnc-toolbar .el-button {
  margin-left: auto;
}

.vnc-lane {
  color: #7a8794;
  font-size: 12px;
}

.vnc-stage {
  position: relative;
  width: 100%;
  height: 76vh;
  min-height: 360px;
  overflow: hidden;
  border-radius: 10px;
  background: #0b1016;
}

.vnc-screen {
  width: 100%;
  height: 100%;
  background: #0b1016;
}

.vnc-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: #dbe7f1;
  text-align: center;
  background: rgba(11, 16, 22, 0.72);
}

.vnc-overlay p {
  margin: 0 0 8px;
  color: #9bb0c2;
  font-size: 13px;
}

.vnc-error strong {
  color: #ff9b9b;
}
</style>
