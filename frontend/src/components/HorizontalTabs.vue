<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";

defineOptions({ inheritAttrs: false });

const model = defineModel<string>({ required: true });
const props = defineProps<{
  items: Array<{ value: string; label: string; badge?: string | number }>;
}>();

const scroller = ref<HTMLElement | null>(null);
const panel = ref<HTMLElement | null>(null);
const id = useId();
const dragging = ref(false);
const selectedIndex = computed(() => props.items.findIndex((item) => item.value === model.value));
const focusIndex = computed(() => Math.max(0, selectedIndex.value));
let pointerId: number | null = null;
let startX = 0;
let startScroll = 0;
let suppressClick = false;
let resizeObserver: ResizeObserver | undefined;

function sizePanel(): void {
  const tabs = scroller.value;
  const content = tabs?.closest<HTMLElement>(".app-content");
  if (!tabs || !content || !panel.value) return;
  // Keep enough scroll range to hide the heading even when the selected panel is short.
  panel.value.style.minHeight = `${Math.max(0, content.clientHeight - tabs.offsetHeight -
    parseFloat(getComputedStyle(tabs).marginBottom) - parseFloat(getComputedStyle(content).paddingBottom))}px`;
}

watch(model, async (_value, _previous, onCleanup) => {
  const tabs = scroller.value;
  const content = tabs?.closest<HTMLElement>(".app-content");
  if (!tabs || !content || !tabs.offsetHeight) return;
  const scrollTop = content.scrollTop;
  const sticky = tabs.getBoundingClientRect().top <= content.getBoundingClientRect().top + 1;
  let canceled = false;
  onCleanup(() => { canceled = true; });
  sizePanel();
  await nextTick();
  if (canceled || !panel.value) return;
  sizePanel();
  const tabsTop = content.scrollTop + panel.value.getBoundingClientRect().top -
    content.getBoundingClientRect().top - tabs.offsetHeight - parseFloat(getComputedStyle(tabs).marginBottom);
  content.scrollTop = sticky ? Math.max(scrollTop, tabsTop) : scrollTop;
});

function reveal(index: number): void {
  const element = scroller.value;
  const tab = element?.children[index] as HTMLElement | undefined;
  if (!element || !tab) return;
  const left = tab.offsetLeft;
  const right = left + tab.offsetWidth;
  if (left < element.scrollLeft) element.scrollLeft = left;
  else if (right > element.scrollLeft + element.clientWidth) {
    element.scrollLeft = right - element.clientWidth;
  }
}

function onWheel(event: WheelEvent): void {
  const element = scroller.value;
  if (!element || event.ctrlKey || element.scrollWidth <= element.clientWidth) return;
  if (!event.deltaY || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) return;
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientWidth : 1;
  const next = Math.max(0, Math.min(element.scrollWidth - element.clientWidth, element.scrollLeft + event.deltaY * unit));
  if (next === element.scrollLeft) return;
  event.preventDefault();
  element.scrollLeft = next;
}

function onPointerDown(event: PointerEvent): void {
  suppressClick = false;
  const element = scroller.value;
  if (!element || event.pointerType !== "mouse" || event.button !== 0 || !event.isPrimary) return;
  if (element.scrollWidth <= element.clientWidth) return;
  pointerId = event.pointerId;
  startX = event.clientX;
  startScroll = element.scrollLeft;
}

function onPointerMove(event: PointerEvent): void {
  const element = scroller.value;
  if (!element || event.pointerId !== pointerId) return;
  if (!(event.buttons & 1)) {
    stopDrag();
    return;
  }
  const delta = event.clientX - startX;
  if (!dragging.value && Math.abs(delta) < 5) return;
  if (!dragging.value) {
    dragging.value = true;
    suppressClick = true;
    // Capture only after the threshold so ordinary tab clicks keep their target.
    element.setPointerCapture(event.pointerId);
  }
  event.preventDefault();
  element.scrollLeft = startScroll - delta;
}

function stopDrag(): void {
  const element = scroller.value;
  const captured = pointerId;
  pointerId = null;
  dragging.value = false;
  if (element && captured !== null && element.hasPointerCapture(captured)) {
    element.releasePointerCapture(captured);
  }
}

function onClick(event: MouseEvent): void {
  if (!suppressClick || event.detail === 0) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  suppressClick = false;
}

function onKeydown(event: KeyboardEvent, index: number): void {
  let next: number;
  switch (event.key) {
    case "ArrowRight": next = (index + 1) % props.items.length; break;
    case "ArrowLeft": next = (index - 1 + props.items.length) % props.items.length; break;
    case "Home": next = 0; break;
    case "End": next = props.items.length - 1; break;
    default: return;
  }
  event.preventDefault();
  suppressClick = false;
  const item = props.items[next];
  if (!item) return;
  model.value = item.value;
  (scroller.value?.children[next] as HTMLElement | undefined)?.focus({ preventScroll: true });
  reveal(next);
}

// Polling may replace items or badges without changing the selected tab's position.
watch([model, selectedIndex], () => reveal(focusIndex.value), { flush: "post" });

onMounted(() => {
  sizePanel();
  resizeObserver = new ResizeObserver(sizePanel);
  if (scroller.value) {
    resizeObserver.observe(scroller.value);
    const content = scroller.value.closest(".app-content");
    if (content) resizeObserver.observe(content);
  }
  reveal(focusIndex.value);
});

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  stopDrag();
});
</script>

<template>
  <div
    v-show="items.length"
    ref="scroller"
    class="horizontal-tabs"
    :class="{ 'is-dragging': dragging }"
    role="tablist"
    aria-label="页面选项"
    aria-orientation="horizontal"
    v-bind="$attrs"
    @wheel="onWheel"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="stopDrag"
    @pointercancel="stopDrag"
    @lostpointercapture="stopDrag"
    @pointerleave="!dragging && stopDrag()"
    @click.capture="onClick"
    @dragstart.prevent
  >
    <button
      v-for="(item, index) in items"
      :id="`${id}-tab-${index}`"
      :key="item.value"
      type="button"
      role="tab"
      class="horizontal-tabs__tab"
      :class="{ 'is-active': model === item.value }"
      :aria-selected="model === item.value"
      :tabindex="focusIndex === index ? 0 : -1"
      @click="model = item.value"
      @keydown="onKeydown($event, index)"
      @focus="reveal(index)"
    >
      <span>{{ item.label }}</span>
      <span v-if="item.badge !== undefined" class="horizontal-tabs__badge">{{ item.badge }}</span>
    </button>
  </div>
  <div ref="panel" class="horizontal-tabs__panel">
    <slot />
  </div>
</template>

<style scoped>
.horizontal-tabs {
  position: sticky;
  top: -26px;
  z-index: 10;
  display: flex;
  flex: none;
  gap: 0;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
  margin-bottom: 16px;
  margin-inline: -30px;
  padding: 0 30px;
  border-bottom: 1px solid #e4e9f0;
  background: #f4f6f9;
}

.horizontal-tabs__panel {
  display: flow-root;
  min-width: 0;
  overflow-anchor: none;
}

@media (max-width: 800px) {
  .horizontal-tabs {
    top: -18px;
    margin-inline: -16px;
    padding-inline: 16px;
  }
}

.horizontal-tabs::-webkit-scrollbar {
  display: none;
}

.horizontal-tabs__tab {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  min-height: 36px;
  padding: 6px 14px;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: #606266;
  font: inherit;
  font-size: 14px;
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}

.horizontal-tabs__tab:hover {
  color: #2f7ed8;
  background: #eaf3ff;
}

.horizontal-tabs__tab.is-active {
  color: #2f7ed8;
  background: #eaf3ff;
  font-weight: 600;
}

.horizontal-tabs__tab:focus-visible {
  outline: 2px solid #409eff;
  outline-offset: -2px;
}

.horizontal-tabs__badge {
  flex: none;
  padding: 1px 6px;
  border-radius: 10px;
  background: #e4e9f0;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.is-dragging,
.is-dragging .horizontal-tabs__tab {
  cursor: grabbing;
}
</style>
