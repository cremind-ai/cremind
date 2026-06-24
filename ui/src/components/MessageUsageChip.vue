<script setup lang="ts">
// Compact per-request token + estimated-cost chip shown under each assistant
// turn. Collapsed: "1,240 tok · $0.0034 · 38% cached". Expands to the per
// sub-agent/tool breakdown for THIS request. Token counts render immediately
// from the message; cost + per-source detail come from the (cached, coalesced)
// conversation usage fetch and match this message by id.
import { computed, onMounted, ref, watch } from 'vue';
import { Icon } from '@iconify/vue';
import type { ChatMessage } from '../stores/chat';
import { useChatStore } from '../stores/chat';
import { useUsageStore } from '../stores/usage';
import { formatTokens, formatUsd, formatPercent } from '../utils/usageFormat';

const props = defineProps<{ message: ChatMessage }>();
const chat = useChatStore();
const usage = useUsageStore();
const expanded = ref(false);

const conversationId = computed(() => chat.activeConversationId);

function ensureLoaded() {
  if (props.message.tokenUsage && conversationId.value) {
    usage.loadConversationUsage(conversationId.value); // coalesced + cached
  }
}
onMounted(ensureLoaded);
watch(() => props.message.id, ensureLoaded);

// The RequestUsage for this turn (cost + per-source), matched by message id.
const request = computed(() => {
  const cid = conversationId.value;
  if (!cid) return null;
  const conv = usage.conversationUsage[cid];
  return conv?.requests.find(r => r.message_id === props.message.id) ?? null;
});

const totalTokens = computed(() =>
  request.value?.total_tokens ?? props.message.tokenUsage?.totalTokens ?? 0);

const cachedTokens = computed(() => {
  if (request.value) return request.value.cache_read_input_tokens;
  const u = props.message.tokenUsage;
  return u ? u.cacheReadTokens : 0;
});

const inputTokens = computed(() => {
  if (request.value) return request.value.input_tokens + request.value.cache_read_input_tokens;
  const u = props.message.tokenUsage;
  return u ? u.inputTokens + u.cacheReadTokens : 0;
});

const cacheRate = computed(() => {
  const denom = inputTokens.value;
  return denom ? cachedTokens.value / denom : 0;
});

const cost = computed(() => request.value?.estimated_cost_usd ?? null);
const bySource = computed(() => request.value?.by_source ?? []);
const canExpand = computed(() => bySource.value.length > 0);
</script>

<template>
  <div class="usage-chip-wrap">
    <button type="button" class="usage-chip" :class="{ clickable: canExpand }" @click="canExpand && (expanded = !expanded)">
      <span>{{ formatTokens(totalTokens) }} tok</span>
      <span v-if="cost !== null" class="sep">·</span>
      <span v-if="cost !== null" class="cost">{{ formatUsd(cost) }}</span>
      <span v-if="cachedTokens > 0" class="sep">·</span>
      <span v-if="cachedTokens > 0" class="cached">{{ formatPercent(cacheRate) }} cached</span>
      <Icon v-if="canExpand" :icon="expanded ? 'mdi:chevron-up' : 'mdi:chevron-down'" class="chip-caret" />
    </button>

    <div v-if="expanded && canExpand" class="usage-breakdown">
      <div class="bd-head">
        <span>Source</span><span class="num">Tokens</span><span class="num">Cost</span>
      </div>
      <div v-for="s in bySource" :key="s.source" class="bd-row">
        <span class="bd-src">
          {{ s.display_name }}
          <span class="bd-type" :class="`t-${s.source_type}`">{{ s.source_type }}</span>
        </span>
        <span class="num">{{ formatTokens(s.total_tokens) }}</span>
        <span class="num">{{ formatUsd(s.estimated_cost_usd) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.usage-chip-wrap { margin-top: 6px; }
.usage-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--el-font-family, monospace);
  font-size: 0.72rem;
  color: var(--text-tertiary, var(--text-secondary));
  background: none;
  border: none;
  padding: 0;
  cursor: default;
}
.usage-chip.clickable { cursor: pointer; }
.usage-chip.clickable:hover { color: var(--text-secondary); }
.usage-chip .sep { opacity: 0.5; }
.usage-chip .cost { color: var(--primary-color); }
.usage-chip .cached { color: var(--success-color, #10b981); }
.chip-caret { font-size: 14px; }

.usage-breakdown {
  margin-top: 6px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-color);
  padding: 6px 8px;
  max-width: 460px;
  font-size: 0.74rem;
}
.bd-head, .bd-row {
  display: grid;
  grid-template-columns: 1fr 80px 80px;
  gap: 8px;
  align-items: center;
  padding: 3px 2px;
}
.bd-head { color: var(--text-tertiary, var(--text-secondary)); border-bottom: 1px solid var(--border-color); margin-bottom: 2px; }
.bd-row { color: var(--text-primary); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.bd-src { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.bd-type {
  font-size: 0.62rem;
  text-transform: uppercase;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--hover-bg);
  color: var(--text-secondary);
}
.t-reasoning { color: var(--primary-color); }
.t-subagent { color: var(--danger-color, #ef4444); }
.t-tool { color: var(--warning-color, #f59e0b); }
</style>
