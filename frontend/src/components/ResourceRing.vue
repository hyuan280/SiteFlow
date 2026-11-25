<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  label: string;
  hostPercent: number | null;
  siteflowPercent: number | null;
  detail: string;
}>();

const host = computed(() => props.hostPercent !== null && Number.isFinite(props.hostPercent)
  ? Math.max(0, Math.min(100, props.hostPercent)) : null);
const siteflow = computed(() => host.value !== null && props.siteflowPercent !== null && Number.isFinite(props.siteflowPercent)
  ? Math.max(0, Math.min(host.value, props.siteflowPercent)) : null);
const segments = computed(() => {
  if (host.value === null) return [];
  const used = siteflow.value === null
    ? [{ label: "宿主机已用", value: host.value, color: "#409eff" }]
    : [
      { label: "SiteFlow", value: siteflow.value, color: "#409eff" },
      { label: "其他已用", value: host.value - siteflow.value, color: "#e6a23c" },
    ];
  let offset = 0;
  return [...used, { label: "空闲", value: 100 - host.value, color: "#e4e9f0" }].map((segment) => {
    const result = { ...segment, offset };
    offset += segment.value;
    return result;
  });
});
const accessibleLabel = computed(() => host.value === null ? `${props.label}资源暂不可用`
  : `${props.label}，宿主机已用 ${host.value.toFixed(1)}%，${segments.value.map((item) => `${item.label} ${item.value.toFixed(1)}%`).join("，")}`);
</script>

<template>
  <section class="resource-ring">
    <svg class="resource-ring__chart" viewBox="0 0 120 120" role="img" :aria-label="accessibleLabel">
      <circle cx="60" cy="60" r="48" fill="none" stroke="#edf1f6" stroke-width="10" />
      <g transform="rotate(-90 60 60)">
        <circle v-for="segment in segments" :key="segment.label" cx="60" cy="60" r="48"
          fill="none" :stroke="segment.color" stroke-width="10" pathLength="100"
          :stroke-dasharray="`${segment.value} ${100 - segment.value}`" :stroke-dashoffset="-segment.offset" />
      </g>
      <text x="60" y="59" text-anchor="middle" class="resource-ring__value">{{ host === null ? '--' : `${host.toFixed(1)}%` }}</text>
      <text x="60" y="77" text-anchor="middle" class="resource-ring__caption">宿主机已用</text>
    </svg>
    <div class="resource-ring__info">
      <h3>{{ label }}</h3>
      <p class="resource-ring__detail">{{ detail }}</p>
      <div v-for="segment in segments" :key="segment.label" class="resource-ring__legend">
        <i :style="{ background: segment.color }" /><span>{{ segment.label }}</span><strong>{{ segment.value.toFixed(1) }}%</strong>
      </div>
      <p v-if="host === null" class="resource-ring__detail">采样暂不可用</p>
    </div>
  </section>
</template>

<style scoped>
.resource-ring { display: flex; align-items: center; gap: 18px; min-width: 0; }
.resource-ring__chart { width: 120px; height: 120px; flex: none; }
.resource-ring__value { font-size: 18px; font-weight: 600; fill: #1f2d3d; font-variant-numeric: tabular-nums; }
.resource-ring__caption { font-size: 10px; fill: #8a96a3; }
.resource-ring__info { min-width: 146px; }
.resource-ring h3 { margin: 0 0 6px; font-size: 15px; }
.resource-ring__detail { margin: 0 0 10px; color: #8a96a3; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.resource-ring__legend { display: flex; align-items: center; gap: 7px; margin-top: 5px; color: #606266; font-size: 12px; }
.resource-ring__legend i { width: 7px; height: 7px; border-radius: 50%; flex: none; }
.resource-ring__legend strong { margin-left: auto; font-weight: 500; font-variant-numeric: tabular-nums; }
@media (max-width: 600px) {
  .resource-ring { gap: 12px; }
  .resource-ring__chart { width: 106px; height: 106px; }
  .resource-ring__info { min-width: 126px; }
}
</style>
