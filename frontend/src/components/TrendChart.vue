<script setup lang="ts">
import { computed } from "vue";

import type { TrendPointView } from "@/types/analytics";

const props = defineProps<{
  points: TrendPointView[];
}>();

const WIDTH = 1000;
const HEIGHT = 280;
const PAD = { left: 44, right: 48, top: 18, bottom: 34 };
const CHART_W = WIDTH - PAD.left - PAD.right;
const CHART_H = HEIGHT - PAD.top - PAD.bottom;

const SEGMENTS = [
  { key: "success", label: "成功", color: "#67c23a" },
  { key: "waiting_human", label: "等待人工", color: "#e6a23c" },
  { key: "unknown", label: "未知", color: "#909399" },
  { key: "deferred", label: "推迟", color: "#409eff" },
  { key: "fail", label: "失败", color: "#f56c6c" },
  { key: "error", label: "错误", color: "#c45656" },
  { key: "canceled", label: "取消", color: "#c0c4cc" },
  { key: "interrupted", label: "中断", color: "#b1b3b8" },
] as const;

const maxTotal = computed(() => Math.max(1, ...props.points.map((point) => point.total)));
const barStep = computed(() =>
  props.points.length ? CHART_W / props.points.length : CHART_W,
);
const barWidth = computed(() => Math.max(3, Math.min(26, barStep.value * 0.62)));
const labelStep = computed(() =>
  Math.max(1, Math.ceil(props.points.length / 8)),
);

interface BarSegment {
  key: string;
  color: string;
  y: number;
  height: number;
}

interface BarView {
  date: string;
  x: number;
  center: number;
  segments: BarSegment[];
  total: number;
  title: string;
}

const bars = computed<BarView[]>(() =>
  props.points.map((point, index) => {
    const center = PAD.left + barStep.value * index + barStep.value / 2;
    let offset = 0;
    const segments: BarSegment[] = [];
    for (const segment of SEGMENTS) {
      const value = Number(point[segment.key] ?? 0);
      if (value <= 0) continue;
      const height = (value / maxTotal.value) * CHART_H;
      segments.push({
        key: segment.key,
        color: segment.color,
        y: PAD.top + CHART_H - offset - height,
        height,
      });
      offset += height;
    }
    const detail = SEGMENTS.map((segment) => {
      const value = Number(point[segment.key] ?? 0);
      return `${segment.label} ${value}`;
    }).join(" · ");
    return {
      date: point.date,
      x: center - barWidth.value / 2,
      center,
      segments,
      total: point.total,
      title: `${point.date} · 总计 ${point.total} · 成功率 ${point.success_rate}%\n${detail}`,
    };
  }),
);

const rateLine = computed(() => {
  const withData = props.points
    .map((point, index) => ({ point, index }))
    .filter(({ point }) => point.total > 0);
  if (!withData.length) return "";
  return withData
    .map(({ point, index }) => {
      const center = PAD.left + barStep.value * index + barStep.value / 2;
      const y = PAD.top + CHART_H * (1 - point.success_rate / 100);
      return `${center},${y}`;
    })
    .join(" ");
});

const rateDots = computed(() =>
  props.points
    .map((point, index) => ({ point, index }))
    .filter(({ point }) => point.total > 0)
    .map(({ point, index }) => ({
      x: PAD.left + barStep.value * index + barStep.value / 2,
      y: PAD.top + CHART_H * (1 - point.success_rate / 100),
      title: `${point.date} · 成功率 ${point.success_rate}%`,
    })),
);

const yTicks = computed(() =>
  [0, 0.5, 1].map((ratio) => ({
    y: PAD.top + CHART_H * (1 - ratio),
    label: Math.round(maxTotal.value * ratio),
  })),
);

const rateTicks = computed(() =>
  [0, 50, 100].map((value) => ({
    y: PAD.top + CHART_H * (1 - value / 100),
    label: `${value}%`,
  })),
);
</script>

<template>
  <div class="trend-chart">
    <div class="trend-legend">
      <span v-for="segment in SEGMENTS" :key="segment.key" class="legend-item">
        <i :style="{ background: segment.color }"></i>{{ segment.label }}
      </span>
      <span class="legend-item"><i class="rate-dot"></i>成功率</span>
    </div>
    <svg :viewBox="`0 0 ${WIDTH} ${HEIGHT}`" class="trend-svg" role="img">
      <line
        v-for="tick in yTicks"
        :key="`y-${tick.y}`"
        :x1="PAD.left"
        :x2="WIDTH - PAD.right"
        :y1="tick.y"
        :y2="tick.y"
        class="grid-line"
      />
      <text
        v-for="tick in yTicks"
        :key="`yt-${tick.y}`"
        :x="PAD.left - 8"
        :y="tick.y + 4"
        text-anchor="end"
        class="axis-label"
      >{{ tick.label }}</text>
      <text
        v-for="tick in rateTicks"
        :key="`rt-${tick.y}`"
        :x="WIDTH - PAD.right + 8"
        :y="tick.y + 4"
        text-anchor="start"
        class="axis-label rate-label"
      >{{ tick.label }}</text>
      <g v-for="bar in bars" :key="bar.date">
        <rect
          v-for="segment in bar.segments"
          :key="`${bar.date}-${segment.key}`"
          :x="bar.x"
          :y="segment.y"
          :width="barWidth"
          :height="segment.height"
          :fill="segment.color"
          rx="2"
        >
          <title>{{ bar.title }}</title>
        </rect>
      </g>
      <polyline v-if="rateLine" :points="rateLine" class="rate-line" />
      <circle
        v-for="dot in rateDots"
        :key="`dot-${dot.x}`"
        :cx="dot.x"
        :cy="dot.y"
        r="3"
        class="rate-point"
      >
        <title>{{ dot.title }}</title>
      </circle>
      <template v-for="(point, index) in points" :key="`x-${point.date}`">
        <text
          v-if="index % labelStep === 0 || index === points.length - 1"
          :x="PAD.left + barStep * index + barStep / 2"
          :y="HEIGHT - 10"
          text-anchor="middle"
          class="axis-label"
        >{{ point.date.slice(5) }}</text>
      </template>
    </svg>
  </div>
</template>

<style scoped>
.trend-chart {
  width: 100%;
}

.trend-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 8px;
  color: #7a8794;
  font-size: 11px;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.legend-item i {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 2px;
}

.legend-item .rate-dot {
  background: #409eff;
  border-radius: 50%;
}

.trend-svg {
  width: 100%;
  height: auto;
  overflow: visible;
}

.grid-line {
  stroke: #edf1f6;
  stroke-width: 1;
}

.axis-label {
  fill: #8a96a3;
  font-size: 10px;
}

.rate-label {
  fill: #409eff;
}

.rate-line {
  fill: none;
  stroke: #409eff;
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}

.rate-point {
  fill: #fff;
  stroke: #409eff;
  stroke-width: 2;
}
</style>
