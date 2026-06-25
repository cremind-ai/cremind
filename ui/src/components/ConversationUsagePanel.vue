<script setup lang="ts">
// Conversation-level token usage + estimated cost. Cumulative totals up to now,
// a per-source rollup (reasoning agent vs. each sub-agent/tool), and a
// per-request table whose rows expand to the per-tool breakdown for that turn.
// Modal cloned from ConversationMemoryPanel.
import { computed, ref, watch } from 'vue';
import {
  ElDialog, ElButton, ElTable, ElTableColumn, ElTag, ElEmpty,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useUsageStore } from '../stores/usage';
import { formatTokens, formatUsd, formatPercent, formatTimestamp } from '../utils/usageFormat';
import type { ConversationUsage, RequestUsage } from '../services/usageApi';

const props = defineProps<{
  modelValue: boolean;
  conversationId: string | null;
}>();
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>();

const usage = useUsageStore();
const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
});

const data = ref<ConversationUsage | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);

async function load() {
  if (!props.conversationId) return;
  loading.value = true;
  error.value = null;
  try {
    data.value = await usage.loadConversationUsage(props.conversationId, true);
    if (!data.value) error.value = 'Failed to load usage';
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (open) => { if (open) load(); });

const tagType = (t: string) =>
  t === 'reasoning' ? 'primary' : t === 'subagent' ? 'danger' : t === 'intrinsic' ? 'success' : 'warning';

// Requests are sortable from the column headers (When / Model / Tokens / Est.
// cost), defaulting to newest-first — which also fixes the unordered rows the
// backend returns (it groups by message_id, i.e. UUID order). ``model`` can be
// null, so it needs an explicit comparator; the numeric columns sort natively.
const modelSort = (a: RequestUsage, b: RequestUsage) =>
  (a.model || '').localeCompare(b.model || '');
</script>

<template>
  <ElDialog v-model="visible" width="720px" :close-on-click-modal="true" class="usage-dialog">
    <template #header="{ titleId, titleClass }">
      <span :id="titleId" :class="titleClass">
        <Icon icon="mdi:chart-box-outline" /> Conversation usage &amp; cost
      </span>
    </template>

    <div v-if="loading" class="usage-loading"><Icon icon="mdi:loading" class="spin" /> Loading usage…</div>
    <div v-else-if="error" class="usage-error"><Icon icon="mdi:alert-circle-outline" /> {{ error }}</div>

    <div v-else-if="data && data.requests.length" class="usage-body">
      <!-- Cumulative totals -->
      <div class="totals-row">
        <div class="total-cell">
          <div class="total-label">Total tokens</div>
          <div class="total-value">{{ formatTokens(data.totals.total_tokens) }}</div>
        </div>
        <div class="total-cell">
          <div class="total-label">Estimated cost</div>
          <div class="total-value cost">{{ formatUsd(data.totals.estimated_cost_usd) }}</div>
        </div>
        <div class="total-cell">
          <div class="total-label">Cache hit rate</div>
          <div class="total-value cached">{{ formatPercent(data.cache_hit_rate) }}</div>
        </div>
        <div class="total-cell">
          <div class="total-label">Requests</div>
          <div class="total-value">{{ data.request_count }}</div>
        </div>
      </div>

      <!-- Per-source rollup -->
      <section class="usage-section">
        <h4><Icon icon="mdi:account-network-outline" /> By source <span class="hint">reasoning agent &amp; sub-agents / tools</span></h4>
        <ElTable :data="data.by_source" size="small">
          <ElTableColumn label="Source" min-width="180">
            <template #default="{ row }">
              <span class="src-name">{{ row.display_name }}</span>
              <ElTag size="small" :type="tagType(row.source_type)" effect="light">{{ row.source_type }}</ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Calls" width="70" align="right" prop="request_count" />
          <ElTableColumn label="Tokens" width="110" align="right">
            <template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Est. cost" width="100" align="right">
            <template #default="{ row }">{{ formatUsd(row.estimated_cost_usd) }}</template>
          </ElTableColumn>
        </ElTable>
      </section>

      <!-- Per-request table; sort from the column headers, newest-first by
           default. Each row expands to its per-source breakdown. -->
      <section class="usage-section">
        <h4><Icon icon="mdi:format-list-numbered" /> Requests <span class="hint">click a column header to sort</span></h4>
        <ElTable
          :data="data.requests"
          size="small"
          row-key="message_id"
          :default-sort="{ prop: 'created_at', order: 'descending' }"
        >
          <ElTableColumn type="expand">
            <template #default="{ row }">
              <div class="req-detail">
                <div v-for="s in row.by_source" :key="s.source" class="req-src-row">
                  <span class="src-name">{{ s.display_name }}</span>
                  <ElTag size="small" :type="tagType(s.source_type)" effect="plain">{{ s.source_type }}</ElTag>
                  <span class="grow"></span>
                  <span class="muted">{{ formatTokens(s.input_tokens + s.cache_read_input_tokens) }} in / {{ formatTokens(s.output_tokens) }} out</span>
                  <span class="cost">{{ formatUsd(s.estimated_cost_usd) }}</span>
                </div>
              </div>
            </template>
          </ElTableColumn>
          <ElTableColumn label="When" width="170" prop="created_at" sortable>
            <template #default="{ row }">{{ formatTimestamp(row.created_at) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Model" min-width="140" prop="model" sortable :sort-method="modelSort" show-overflow-tooltip>
            <template #default="{ row }">{{ row.model || '—' }}</template>
          </ElTableColumn>
          <ElTableColumn label="Tokens" width="110" prop="total_tokens" sortable align="right">
            <template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template>
          </ElTableColumn>
          <ElTableColumn label="Est. cost" width="110" prop="estimated_cost_usd" sortable align="right">
            <template #default="{ row }">{{ formatUsd(row.estimated_cost_usd) }}</template>
          </ElTableColumn>
        </ElTable>
      </section>
    </div>

    <ElEmpty v-else-if="data" description="No usage recorded for this conversation yet" :image-size="70" />

    <template #footer>
      <ElButton @click="load" :disabled="loading"><Icon icon="mdi:refresh" /> Refresh</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.usage-loading, .usage-error { display: flex; align-items: center; gap: 8px; padding: 24px; color: var(--text-secondary); justify-content: center; }
.usage-error { color: var(--danger-color, #ef4444); }
.usage-body { display: flex; flex-direction: column; gap: 18px; max-height: 64vh; overflow-y: auto; }

.totals-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.total-cell { border: 1px solid var(--border-color); border-radius: 10px; padding: 12px 14px; background: var(--surface-color); }
.total-label { font-size: 0.74rem; color: var(--text-secondary); }
.total-value { font-size: 1.25rem; font-weight: 700; color: var(--text-primary); margin-top: 2px; }
.total-value.cost { color: var(--primary-color); }
.total-value.cached { color: var(--success-color, #10b981); }

.usage-section h4 { display: flex; align-items: center; gap: 6px; margin: 0 0 8px; font-size: 0.95rem; color: var(--text-primary); }
.usage-section h4 .hint { font-weight: 400; font-size: 0.78rem; color: var(--text-tertiary, var(--text-secondary)); }
.src-name { margin-right: 8px; color: var(--text-primary); }

.req-detail { display: flex; flex-direction: column; gap: 4px; padding: 4px 10px; }
.req-src-row { display: flex; align-items: center; gap: 8px; font-size: 0.78rem; }
.req-src-row .grow { flex: 1; }
.req-src-row .muted { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.req-src-row .cost { color: var(--primary-color); min-width: 64px; text-align: right; font-variant-numeric: tabular-nums; }

.spin { animation: usage-spin 1s linear infinite; }
@keyframes usage-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }
</style>
