<script setup lang="ts">
import RFB from "@novnc/novnc";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { apiRequest } from "@/api/client";

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
const clipboardOpen = ref(false);
const clipboardText = ref("");
const clipboardError = ref("");
const clipboardBusy = ref(false);
const clipboardInput = ref<HTMLTextAreaElement | null>(null);
let clipboardAbort: AbortController | null = null;
const receiveOpen = ref(false);
const receiveText = ref("");
const receiveStatus = ref("");
const receiveInput = ref<HTMLTextAreaElement | null>(null);
let copyAbort: AbortController | null = null;
let copySignal: (() => void) | null = null;
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

function clearReceivedClipboard(): void {
  if (copyAbort) {
    copyAbort.abort();
    copyAbort = null;
    clipboardBusy.value = false;
  }
  copySignal = null;
  receiveOpen.value = false;
  receiveText.value = "";
  receiveStatus.value = "";
}

function disconnect(): void {
  connectionGeneration += 1;
  clearReceivedClipboard();
  clipboardAbort?.abort();
  clipboardAbort = null;
  clipboardBusy.value = false;
  clipboardOpen.value = false;
  clipboardText.value = "";
  clipboardError.value = "";
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
    client.showDotCursor = true;
    client.addEventListener("connect", () => {
      if (rfb !== client) return;
      state.value = "connected";
      errorMessage.value = "";
    });
    client.addEventListener("disconnect", () => {
      if (rfb !== client) return;
      clearReceivedClipboard();
      clipboardAbort?.abort();
      clipboardOpen.value = false;
      clipboardText.value = "";
      if (state.value !== "error") {
        state.value = "disconnected";
      }
    });
    client.addEventListener("securityfailure", () => {
      if (rfb !== client) return;
      clearReceivedClipboard();
      state.value = "error";
      errorMessage.value = "VNC 安全校验失败";
    });
    client.addEventListener("clipboard", () => {
      // Only a pending explicit copy uses this signal, never its lossy RFB text.
      // Unsolicited events (including HTTP paste echoes) never write to the host.
      if (rfb === client) copySignal?.();
    });
    client.addEventListener("credentialsrequired", () => {
      // Authentication is handled by the API and browser backend, not RFB credentials.
    });
  } catch (error) {
    if (generation !== connectionGeneration) return;
    state.value = "error";
    errorMessage.value = error instanceof Error ? error.message : "无法建立 VNC 连接";
  }
}

function openClipboard(): void {
  if (state.value !== "connected" || clipboardBusy.value) return;
  clipboardError.value = "";
  clipboardOpen.value = true;
}

async function copyClipboard(remoteCopy = false, buffered = false): Promise<void> {
  const client = rfb;
  if (!client || state.value !== "connected" || !props.modelValue || clipboardBusy.value) return;
  const generation = connectionGeneration;
  const laneKey = props.laneKey;
  const controller = new AbortController();
  const current = () => rfb === client && generation === connectionGeneration
    && props.modelValue && props.laneKey === laneKey && state.value === "connected"
    && copyAbort === controller;
  copyAbort = controller;
  clipboardBusy.value = true;
  receiveStatus.value = "正在复制到本机…";
  if (!buffered) receiveText.value = "";
  let settleTimer = 0;
  const timer = window.setTimeout(() => controller.abort(), 20000);
  const deadline = new Promise<never>((_resolve, reject) => {
    controller.signal.addEventListener("abort", () => reject(new Error("Clipboard copy cancelled or timed out")), { once: true });
  });
  let writing = false;
  try {
    await Promise.race([(async () => {
      if (remoteCopy) {
        const signal = new Promise<void>(resolve => {
          copySignal = resolve;
          // A settling delay, not an acknowledgement that the clipboard is fresh.
          settleTimer = window.setTimeout(resolve, 300);
        });
        for (const [keysym, code] of [
          [0xffe1, "ShiftLeft"], [0xffe2, "ShiftRight"],
          [0xffe3, "ControlLeft"], [0xffe4, "ControlRight"],
          [0xffe9, "AltLeft"], [0xffea, "AltRight"],
          [0xffeb, "MetaLeft"], [0xffec, "MetaRight"],
        ] as const) client.sendKey(keysym, code, false);
        client.sendKey(0xffe3, "ControlLeft", true);
        try {
          client.sendKey(0x63, "KeyC");
        } finally {
          client.sendKey(0xffe3, "ControlLeft", false);
        }
        await Promise.race([signal, deadline]);
        if (!current() || controller.signal.aborted) return;
        window.clearTimeout(settleTimer);
        copySignal = null;
      }
      const result = buffered ? { text: receiveText.value }
        : await apiRequest<{ text: string }>(`/lanes/${encodeURIComponent(laneKey)}/clipboard`, {
          method: "GET", cache: "no-store", signal: controller.signal,
        });
      if (!current() || controller.signal.aborted) return;
      if (typeof result?.text !== "string") throw new Error("Invalid clipboard response");
      receiveText.value = result.text;
      if (!result.text) {
        receiveStatus.value = "远端剪贴板为空，未更改本机剪贴板。请先在远端复制文本后重试。";
        receiveOpen.value = true;
        return;
      }
      writing = true;
      if (!document.hasFocus() || document.visibilityState !== "visible") {
        throw new Error("Clipboard writes require a foreground page");
      }
      await navigator.clipboard.writeText(result.text);
      if (!current() || controller.signal.aborted) return;
      receiveStatus.value = "已复制到本机剪贴板。";
    })(), deadline]);
  } catch {
    if (!current()) return;
    receiveStatus.value = writing
      ? "无法写入本机剪贴板或操作超时。请保持页面在前台，点击下方按钮重试；HTTP 或权限被拒绝时，可选中文本后按 Ctrl+C / Cmd+C。"
      : "读取远端剪贴板失败或超时，请确认连接并先在远端复制文本后重试。";
    receiveOpen.value = true;
    await nextTick();
    if (current() && document.hasFocus() && document.visibilityState === "visible") {
      receiveInput.value?.focus();
      receiveInput.value?.select();
    }
  } finally {
    window.clearTimeout(timer);
    window.clearTimeout(settleTimer);
    controller.abort();
    if (copyAbort === controller) {
      copyAbort = null;
      copySignal = null;
      clipboardBusy.value = false;
      if (!receiveOpen.value) receiveText.value = "";
    }
  }
}

async function pasteClipboard(readText: () => Promise<string>): Promise<void> {
  const client = rfb;
  if (!client || state.value !== "connected" || clipboardBusy.value) return;
  const generation = connectionGeneration;
  const laneKey = props.laneKey;
  const current = () => rfb === client && generation === connectionGeneration
    && props.modelValue && props.laneKey === laneKey && state.value === "connected";
  clipboardBusy.value = true;
  clipboardError.value = "";
  const controller = new AbortController();
  clipboardAbort = controller;
  let timer = 0;
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = window.setTimeout(() => {
      controller.abort();
      reject(new Error("Clipboard request timed out"));
    }, 20000);
  });
  let reading = true;
  try {
    const text = await Promise.race([readText(), deadline]);
    if (!current() || controller.signal.aborted) return;
    reading = false;
    if ([...text].length > 1048576) {
      clipboardError.value = "文本过长，最多支持 1048576 个字符。";
      clipboardOpen.value = true;
      return;
    }
    // Manager's HTTP relay preserves Unicode; ordinary RFB ClientCutText does not.
    const result = await apiRequest<{ ok: boolean }>(`/lanes/${encodeURIComponent(laneKey)}/clipboard`, {
      method: "POST", body: JSON.stringify({ text }), signal: controller.signal,
    });
    if (!current() || controller.signal.aborted) return;
    if (result?.ok !== true) throw new Error("Invalid clipboard response");
    // The local shortcut may already have forwarded its modifiers through noVNC.
    for (const [keysym, code] of [
      [0xffe1, "ShiftLeft"], [0xffe2, "ShiftRight"],
      [0xffe3, "ControlLeft"], [0xffe4, "ControlRight"],
      [0xffe9, "AltLeft"], [0xffea, "AltRight"],
      [0xffeb, "MetaLeft"], [0xffec, "MetaRight"],
    ] as const) client.sendKey(keysym, code, false);
    client.sendKey(0xffe3, "ControlLeft", true);
    try {
      client.sendKey(0x76, "KeyV");
    } finally {
      client.sendKey(0xffe3, "ControlLeft", false);
    }
    clipboardText.value = "";
    clipboardOpen.value = false;
    await nextTick();
    if (current()) client.focus({ preventScroll: true });
  } catch {
    if (!current()) return;
    clipboardError.value = reading
      ? "无法读取本机剪贴板，请在下方粘贴文本后发送。HTTP 或未授予权限时可使用此方式。"
      : "粘贴失败或超时，未发送粘贴按键。请确认车道连接后重试。";
    clipboardOpen.value = true;
    await nextTick();
    clipboardInput.value?.focus();
  } finally {
    window.clearTimeout(timer);
    if (clipboardAbort === controller) {
      clipboardAbort = null;
      clipboardBusy.value = false;
    }
  }
}

function handleKeydown(event: KeyboardEvent): void {
  const key = event.key.toLowerCase();
  const target = event.target as HTMLElement | null;
  if (event.isComposing || target?.isContentEditable || target?.closest?.("input, textarea, select")) return;
  if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey
    || (key !== "v" && key !== "c") || state.value !== "connected") return;
  // Capture before noVNC forwards the host shortcut to the Linux remote.
  event.preventDefault();
  event.stopPropagation();
  if (!event.repeat) {
    if (key === "c") void copyClipboard(true);
    else void pasteClipboard(() => navigator.clipboard.readText());
  }
}

function handlePaste(event: ClipboardEvent): void {
  if (state.value !== "connected" || !event.clipboardData?.types.includes("text/plain")) return;
  event.preventDefault();
  event.stopPropagation();
  const text = event.clipboardData.getData("text/plain");
  void pasteClipboard(async () => text);
}

function close(): void {
  disconnect();
  emit("update:modelValue", false);
}

watch(
  () => [props.modelValue, props.laneKey] as const,
  ([open, laneKey], [wasOpen, previousLane]) => {
    if (!open) {
      disconnect();
    } else if (props.embedded || (wasOpen && laneKey !== previousLane)) {
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
      <el-button size="small" :disabled="state !== 'connected' || clipboardBusy" @click="openClipboard">粘贴文本</el-button>
      <el-button size="small" title="先在远端复制文本；仅在页面处于前台并获得焦点时写入本机，不自动同步右键复制。画面内可用 Ctrl+C / Cmd+C。" :disabled="state !== 'connected' || clipboardBusy" @click="copyClipboard()">复制到本机</el-button>
      <span v-if="receiveStatus" role="status">{{ receiveStatus }}</span>
      <el-button size="small" plain @click="connect">重新连接</el-button>
    </div>
    <div class="vnc-stage">
      <div ref="screen" class="vnc-screen" @keydown.capture="handleKeydown" @paste.capture="handlePaste"></div>
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
      <el-button size="small" :disabled="state !== 'connected' || clipboardBusy" @click="openClipboard">粘贴文本</el-button>
      <el-button size="small" title="先在远端复制文本；仅在页面处于前台并获得焦点时写入本机，不自动同步右键复制。画面内可用 Ctrl+C / Cmd+C。" :disabled="state !== 'connected' || clipboardBusy" @click="copyClipboard()">复制到本机</el-button>
      <span v-if="receiveStatus" role="status">{{ receiveStatus }}</span>
      <el-button type="danger" plain size="small" @click="close">退出 VNC</el-button>
    </div>

    <div class="vnc-stage">
      <div ref="screen" class="vnc-screen" @keydown.capture="handleKeydown" @paste.capture="handlePaste"></div>
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

  <el-dialog
    v-model="clipboardOpen"
    title="粘贴文本到 VNC"
    width="min(560px, 92vw)"
    append-to-body
    :close-on-click-modal="false"
    @opened="clipboardInput?.focus()"
    @closed="clipboardText = ''; clipboardError = ''"
    @close-auto-focus="rfb?.focus({ preventScroll: true })"
  >
    <p class="vnc-clipboard-hint">先在远端点击目标输入框，再粘贴文本并发送。也可在 VNC 画面中使用 Ctrl+V / Cmd+V。</p>
    <p v-if="clipboardError" class="vnc-clipboard-error" role="alert">{{ clipboardError }}</p>
    <textarea
      ref="clipboardInput"
      v-model="clipboardText"
      class="vnc-clipboard-input"
      aria-label="要粘贴到 VNC 的文本"
      placeholder="在此粘贴文本，支持中文和多行内容"
      :disabled="clipboardBusy"
      spellcheck="false"
    ></textarea>
    <template #footer>
      <el-button :disabled="clipboardBusy" @click="clipboardOpen = false">取消</el-button>
      <el-button type="primary" :loading="clipboardBusy" :disabled="state !== 'connected'" @click="pasteClipboard(async () => clipboardText)">发送并粘贴</el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="receiveOpen"
    title="复制到本机"
    width="min(560px, 92vw)"
    append-to-body
    :close-on-click-modal="false"
    @close="clearReceivedClipboard"
  >
    <p class="vnc-clipboard-hint">先在远端复制文本，再点击“复制到本机”；也可在 VNC 画面中按 Ctrl+C / Cmd+C。仅在页面位于前台且获得焦点时尝试写入，不自动同步右键复制。若内容尚未更新，请稍后重试。</p>
    <p role="status">{{ receiveStatus }}</p>
    <textarea
      ref="receiveInput"
      :value="receiveText"
      class="vnc-clipboard-input"
      aria-label="从 VNC 读取的文本，可选中后手动复制"
      readonly
      spellcheck="false"
      @focus="receiveInput?.select()"
    ></textarea>
    <template #footer>
      <el-button @click="clearReceivedClipboard">关闭</el-button>
      <el-button :disabled="state !== 'connected' || clipboardBusy" @click="copyClipboard()">重新读取</el-button>
      <el-button type="primary" :loading="clipboardBusy" :disabled="state !== 'connected' || !receiveText" @click="copyClipboard(false, true)">复制此文本</el-button>
    </template>
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
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}

.vnc-toolbar .el-button:last-child {
  margin-left: auto;
}

.vnc-lane {
  color: #7a8794;
  font-size: 12px;
}

.vnc-clipboard-hint {
  margin-top: 0;
  color: var(--el-text-color-secondary);
}

.vnc-clipboard-error {
  color: var(--el-color-danger);
}

.vnc-clipboard-input {
  box-sizing: border-box;
  width: 100%;
  min-height: 160px;
  padding: 10px;
  resize: vertical;
  color: var(--el-text-color-primary);
  background: var(--el-fill-color-blank);
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  font: inherit;
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
