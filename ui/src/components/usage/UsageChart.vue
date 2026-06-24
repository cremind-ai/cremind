<script setup lang="ts">
// Thin reactive wrapper around ECharts (tree-shaken core import — only the
// chart types/components we use are registered, keeping the bundle small and
// CSP-safe in both the Electron and web builds). The parent passes a fully
// built `option`; colors come from the option (derived from CSS vars by the
// parent), so a theme switch is just a new option, no re-init.
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, PieChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  TitleComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

echarts.use([
  BarChart, LineChart, PieChart,
  GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, TitleComponent,
  CanvasRenderer,
]);

const props = withDefaults(defineProps<{
  option: Record<string, any>;
  height?: string;
}>(), { height: '280px' });

const el = ref<HTMLDivElement | null>(null);
const chart = shallowRef<echarts.ECharts | null>(null);
let observer: ResizeObserver | null = null;

function render() {
  if (!chart.value) return;
  // notMerge:true so removed series/axes don't linger across filter changes.
  chart.value.setOption(props.option, true);
}

onMounted(() => {
  if (!el.value) return;
  chart.value = echarts.init(el.value);
  render();
  observer = new ResizeObserver(() => chart.value?.resize());
  observer.observe(el.value);
});

watch(() => props.option, render, { deep: true });

onBeforeUnmount(() => {
  observer?.disconnect();
  observer = null;
  chart.value?.dispose();
  chart.value = null;
});
</script>

<template>
  <div ref="el" class="usage-chart" :style="{ height }"></div>
</template>

<style scoped>
.usage-chart {
  width: 100%;
}
</style>
