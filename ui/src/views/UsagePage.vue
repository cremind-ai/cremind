<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElCard, ElSelect, ElOption, ElTable, ElTableColumn, ElTag,
  ElEmpty, ElAlert, ElSkeleton, ElRadioGroup, ElRadioButton, ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { useUsageStore } from '../stores/usage';
import UsageChart from '../components/usage/UsageChart.vue';
import UsageStatTile from '../components/usage/UsageStatTile.vue';
import { formatTokens, formatTokensCompact, formatUsd, formatPercent, formatTimestamp } from '../utils/usageFormat';
import { goBackToChat } from '../utils/backToChat';
import type { UsageGroupSlice, UsageTimePoint } from '../services/usageApi';

const props = defineProps<{ profile: string }>();
const settings = useSettingsStore();
const usage = useUsageStore();
const router = useRouter();

function goBack() {
  goBackToChat(router, props.profile);
}

const isAdmin = computed(() => (settings.profileId || props.profile) === 'admin');

// ── filters ──
const rangeKey = ref<'7d' | '30d' | '90d' | 'all'>('30d');
const granularity = ref<'day' | 'week'>('day');
const scopeAll = ref(false); // admin only: span all profiles

const RANGE_DAYS: Record<string, number | null> = { '7d': 7, '30d': 30, '90d': 90, all: null };

function startMs(): number | null {
  const days = RANGE_DAYS[rangeKey.value];
  if (days == null) return null;
  return Date.now() - days * 86_400_000;
}

async function reload() {
  await usage.loadSummary({
    start: startMs(),
    end: null,
    profile: isAdmin.value && scopeAll.value ? null : props.profile,
  });
}

onMounted(reload);
watch([rangeKey, scopeAll], reload);

const summary = computed(() => usage.summary);

// ── chart palette (read CSS vars; recomputed on theme switch) ──
function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
const palette = computed(() => {
  // Reference theme so the palette recomputes when it flips.
  void settings.theme;
  return {
    text: cssVar('--text-primary', '#334155'),
    textSecondary: cssVar('--text-secondary', '#64748b'),
    border: cssVar('--border-color', '#e2e8f0'),
    surface: cssVar('--surface-color', '#ffffff'),
    primary: cssVar('--primary-color', '#2563eb'),
    success: cssVar('--success-color', '#10b981'),
    warning: cssVar('--warning-color', '#f59e0b'),
    danger: cssVar('--danger-color', '#ef4444'),
    // Distinct categorical colors for pies / multi-series.
    series: ['#2563eb', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444', '#06b6d4', '#ec4899', '#84cc16'],
  };
});

// ── week re-bucketing (client-side) ──
function isoWeekKey(iso: string): string {
  const d = new Date(iso + 'T00:00:00Z');
  const day = (d.getUTCDay() + 6) % 7; // Mon=0
  d.setUTCDate(d.getUTCDate() - day);
  return d.toISOString().slice(0, 10);
}

const displaySeries = computed<UsageTimePoint[]>(() => {
  const pts = summary.value?.series ?? [];
  if (granularity.value === 'day') return pts;
  const byWeek = new Map<string, UsageTimePoint>();
  for (const p of pts) {
    const key = isoWeekKey(p.bucket);
    const acc = byWeek.get(key);
    if (!acc) {
      byWeek.set(key, { ...p, bucket: key });
    } else {
      acc.input_tokens += p.input_tokens;
      acc.cache_read_input_tokens += p.cache_read_input_tokens;
      acc.cache_creation_input_tokens += p.cache_creation_input_tokens;
      acc.output_tokens += p.output_tokens;
      acc.total_tokens += p.total_tokens;
      acc.total_usd += p.total_usd;
      acc.request_count += p.request_count;
    }
  }
  return [...byWeek.values()].sort((a, b) => a.bucket.localeCompare(b.bucket));
});

// ── chart options ──
const timeSeriesOption = computed(() => {
  const c = palette.value;
  const pts = displaySeries.value;
  const dates = pts.map(p => p.bucket);
  const axisCommon = { axisLabel: { color: c.textSecondary }, axisLine: { lineStyle: { color: c.border } } };
  const stackArea = (name: string, color: string, data: number[]) => ({
    name, type: 'line', stack: 'tok', smooth: true, showSymbol: false,
    areaStyle: { opacity: 0.55 }, lineStyle: { width: 1 }, itemStyle: { color },
    emphasis: { focus: 'series' }, data,
  });
  return {
    tooltip: {
      trigger: 'axis',
      backgroundColor: c.surface, borderColor: c.border,
      textStyle: { color: c.text },
    },
    legend: { textStyle: { color: c.textSecondary }, top: 0 },
    grid: { left: 8, right: 8, top: 36, bottom: 8, containLabel: true },
    xAxis: { type: 'category', boundaryGap: false, data: dates, ...axisCommon },
    yAxis: [
      { type: 'value', name: 'Tokens', nameTextStyle: { color: c.textSecondary },
        axisLabel: { color: c.textSecondary, formatter: (v: number) => formatTokensCompact(v) },
        splitLine: { lineStyle: { color: c.border, opacity: 0.4 } } },
      { type: 'value', name: 'Cost', nameTextStyle: { color: c.textSecondary },
        axisLabel: { color: c.textSecondary, formatter: (v: number) => `$${v}` },
        splitLine: { show: false } },
    ],
    series: [
      stackArea('Uncached input', c.primary, pts.map(p => p.input_tokens)),
      stackArea('Cache read', c.success, pts.map(p => p.cache_read_input_tokens)),
      stackArea('Cache write', c.warning, pts.map(p => p.cache_creation_input_tokens)),
      stackArea('Output', c.series[3], pts.map(p => p.output_tokens)),
      {
        name: 'Est. cost', type: 'line', yAxisIndex: 1, smooth: true, showSymbol: false,
        lineStyle: { width: 2, type: 'dashed' }, itemStyle: { color: c.danger },
        data: pts.map(p => Number(p.total_usd.toFixed(4))),
      },
    ],
  };
});

function donutOption(slices: UsageGroupSlice[]) {
  const c = palette.value;
  return {
    tooltip: {
      trigger: 'item', backgroundColor: c.surface, borderColor: c.border, textStyle: { color: c.text },
      formatter: (p: any) => {
        const s = slices[p.dataIndex];
        return `${p.name}<br/>${formatTokens(s.total_tokens)} tok · ${formatUsd(s.estimated_cost_usd)}`;
      },
    },
    legend: { type: 'scroll', orient: 'vertical', right: 0, top: 'middle', textStyle: { color: c.textSecondary } },
    color: c.series,
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['32%', '50%'],
      avoidLabelOverlap: true, label: { show: false },
      data: slices.map(s => ({ name: s.display_name, value: s.total_tokens })),
    }],
  };
}
const byModelOption = computed(() => donutOption(summary.value?.by_model ?? []));
const byProviderOption = computed(() => donutOption(summary.value?.by_provider ?? []));

const bySourceOption = computed(() => {
  const c = palette.value;
  const slices = [...(summary.value?.by_source ?? [])].slice(0, 12).reverse();
  const typeColor: Record<string, string> = {
    reasoning: c.primary, tool: c.warning, subagent: c.series[3], intrinsic: c.success, aggregate: c.textSecondary,
  };
  return {
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: c.surface, borderColor: c.border, textStyle: { color: c.text },
      formatter: (ps: any) => {
        const s = slices[ps[0].dataIndex];
        return `${s.display_name} (${s.source_type})<br/>${formatTokens(s.total_tokens)} tok · ${formatUsd(s.estimated_cost_usd)}`;
      },
    },
    grid: { left: 8, right: 16, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: 'value', axisLabel: { color: c.textSecondary, formatter: (v: number) => formatTokensCompact(v) }, splitLine: { lineStyle: { color: c.border, opacity: 0.4 } } },
    yAxis: { type: 'category', data: slices.map(s => s.display_name), axisLabel: { color: c.textSecondary }, axisLine: { lineStyle: { color: c.border } } },
    series: [{
      type: 'bar', barWidth: '58%',
      data: slices.map(s => ({ value: s.total_tokens, itemStyle: { color: typeColor[s.source_type ?? ''] || c.primary, borderRadius: [0, 4, 4, 0] } })),
    }],
  };
});

function openConversation(id: string) {
  router.push({ name: 'conversation', params: { profile: props.profile, conversationId: id } });
}

const hasData = computed(() => (summary.value?.request_count ?? 0) > 0 || (summary.value?.totals.total_tokens ?? 0) > 0);
</script>

<template>
  <div class="usage-page">
    <header class="usage-header">
      <div class="usage-title">
        <h1>
          <button class="icon-button" @click="goBack" title="Back to conversation">
            <Icon icon="mdi:arrow-left" />
          </button>
          <Icon icon="mdi:chart-box-outline" /> Usage &amp; Cost
        </h1>
        <p class="usage-subtitle">Token usage and estimated pricing across the agent and its sub-agents.</p>
      </div>
      <div class="usage-filters">
        <ElRadioGroup v-model="granularity" size="small">
          <ElRadioButton label="day">Daily</ElRadioButton>
          <ElRadioButton label="week">Weekly</ElRadioButton>
        </ElRadioGroup>
        <ElSelect v-model="rangeKey" size="small" style="width: 130px">
          <ElOption label="Last 7 days" value="7d" />
          <ElOption label="Last 30 days" value="30d" />
          <ElOption label="Last 90 days" value="90d" />
          <ElOption label="All time" value="all" />
        </ElSelect>
        <ElTooltip v-if="isAdmin" content="Span every profile (admin)">
          <ElRadioGroup v-model="scopeAll" size="small">
            <ElRadioButton :label="false">This profile</ElRadioButton>
            <ElRadioButton :label="true">All profiles</ElRadioButton>
          </ElRadioGroup>
        </ElTooltip>
      </div>
    </header>

    <ElAlert v-if="usage.summaryError" :title="usage.summaryError" type="error" show-icon :closable="false" class="usage-alert" />

    <ElSkeleton v-if="usage.summaryLoading && !summary" :rows="6" animated class="usage-skeleton" />

    <template v-else-if="summary && hasData">
      <ElAlert
        v-if="summary.has_unpriced"
        type="info" show-icon :closable="false" class="usage-alert"
        title="Some historical usage has no price estimate (the model used couldn't be determined). Token counts are exact; those rows contribute $0 to cost totals."
      />

      <!-- Stat tiles -->
      <section class="stat-grid">
        <UsageStatTile icon="mdi:counter" label="Total tokens" :value="formatTokens(summary.totals.total_tokens)"
          :sub="`${formatTokens(summary.totals.input_tokens + summary.totals.cache_read_input_tokens + summary.totals.cache_creation_input_tokens)} in · ${formatTokens(summary.totals.output_tokens)} out`" />
        <UsageStatTile icon="mdi:cash-multiple" label="Estimated cost" :value="formatUsd(summary.totals.estimated_cost_usd)" accent="danger"
          :sub="`output ${formatUsd(summary.totals.output_usd)} · cache writes ${formatUsd(summary.cache_write_usd)}`" />
        <UsageStatTile icon="mdi:cached" label="Cache hit rate" :value="formatPercent(summary.cache_hit_rate)" accent="success"
          :sub="`saved on reads ${formatUsd(summary.cache_read_usd)}`" />
        <UsageStatTile icon="mdi:message-text-outline" label="Requests" :value="formatTokens(summary.request_count)" accent="warning"
          :sub="`across ${formatTokens(summary.conversation_count)} conversations`" />
      </section>

      <!-- Time series -->
      <ElCard class="usage-card" shadow="never">
        <template #header><span class="card-title">Usage over time</span></template>
        <UsageChart :option="timeSeriesOption" height="320px" />
      </ElCard>

      <!-- Breakdowns grid -->
      <section class="breakdown-grid">
        <ElCard class="usage-card" shadow="never">
          <template #header><span class="card-title">By model</span></template>
          <UsageChart v-if="summary.by_model.length" :option="byModelOption" height="240px" />
          <ElEmpty v-else description="No model data" :image-size="60" />
        </ElCard>

        <ElCard class="usage-card" shadow="never">
          <template #header><span class="card-title">By provider</span></template>
          <UsageChart v-if="summary.by_provider.length" :option="byProviderOption" height="240px" />
          <ElEmpty v-else description="No provider data" :image-size="60" />
        </ElCard>
      </section>

      <!-- By source (reasoning agent vs each sub-agent / tool) -->
      <ElCard class="usage-card" shadow="never">
        <template #header>
          <span class="card-title">By source <span class="card-hint">— reasoning agent vs. each sub-agent / tool</span></span>
        </template>
        <UsageChart v-if="summary.by_source.length" :option="bySourceOption" height="300px" />
        <ElTable v-if="summary.by_source.length" :data="summary.by_source" size="small" class="usage-table">
          <ElTableColumn label="Source" min-width="160">
            <template #default="{ row }">
              <span class="src-name">{{ row.display_name }}</span>
              <ElTag size="small" :type="row.source_type === 'reasoning' ? 'primary' : row.source_type === 'subagent' ? 'danger' : 'warning'" effect="light">{{ row.source_type }}</ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Tokens" width="110" align="right">
            <template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Calls" width="80" align="right" prop="request_count" />
          <ElTableColumn label="Est. cost" width="110" align="right">
            <template #default="{ row }">{{ formatUsd(row.estimated_cost_usd) }}</template>
          </ElTableColumn>
        </ElTable>
      </ElCard>

      <!-- Top conversations -->
      <ElCard class="usage-card" shadow="never">
        <template #header><span class="card-title">Top conversations</span></template>
        <ElTable :data="summary.top_conversations" size="small" class="usage-table" @row-click="(r: any) => openConversation(r.conversation_id)">
          <ElTableColumn label="Conversation" min-width="220" show-overflow-tooltip prop="title" />
          <ElTableColumn label="Requests" width="100" align="right" prop="request_count" />
          <ElTableColumn label="Tokens" width="120" align="right">
            <template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Est. cost" width="110" align="right">
            <template #default="{ row }">{{ formatUsd(row.total_usd) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Last active" width="180">
            <template #default="{ row }">{{ formatTimestamp(row.last_active_at) }}</template>
          </ElTableColumn>
        </ElTable>
        <ElEmpty v-if="!summary.top_conversations.length" description="No conversations yet" :image-size="60" />
      </ElCard>
    </template>

    <ElEmpty v-else-if="summary && !hasData" description="No usage recorded yet. Start a conversation to see token usage and cost here." />
  </div>
</template>

<style scoped>
.usage-page {
  padding: 22px 26px;
  max-width: 1200px;
  margin: 0 auto;
  overflow-y: auto;
  height: 100%;
}
.usage-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}
.usage-title h1 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 1.5rem;
  color: var(--text-primary);
}
.icon-button {
  width: 32px;
  height: 32px;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  transition: background 0.2s ease;
}
.icon-button:hover {
  background: var(--hover-bg);
  color: var(--primary-color);
}
.usage-subtitle { margin: 4px 0 0; color: var(--text-secondary); font-size: 0.85rem; }
.usage-filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.usage-alert { margin-bottom: 16px; }
.usage-skeleton { padding: 20px; }

.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 18px;
}
.breakdown-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
.usage-card { margin-bottom: 16px; border-radius: 12px; border-color: var(--border-color); background: var(--surface-color); }
.card-title { font-weight: 600; color: var(--text-primary); }
.card-hint { font-weight: 400; color: var(--text-tertiary, var(--text-secondary)); font-size: 0.82rem; }
.usage-table { margin-top: 12px; width: 100%; }
.usage-table :deep(.el-table__row) { cursor: default; }
.src-name { margin-right: 8px; color: var(--text-primary); }
</style>
