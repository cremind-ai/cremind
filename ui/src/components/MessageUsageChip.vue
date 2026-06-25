<script setup lang="ts">
// Compact per-request token + estimated-cost chip shown under each assistant
// turn: "1,240 tok · $0.0034 · 38% cached". Opening it reveals an explanation of
// each metric plus the per sub-agent/tool breakdown for THIS request. The popover
// opens on hover by default; users can flip it to click-to-open via the in-popover
// switch (persisted globally). Token counts render immediately from the message;
// cost + per-source detail come from the (cached, coalesced) conversation usage
// fetch and match this message by id.
import { computed, onMounted, watch } from 'vue';
import { Icon } from '@iconify/vue';
import { ElPopover, ElSwitch } from 'element-plus';
import type { ChatMessage } from '../stores/chat';
import { useChatStore } from '../stores/chat';
import { useUsageStore } from '../stores/usage';
import { useSettingsStore } from '../stores/settings';
import { formatTokens, formatUsd, formatPercent } from '../utils/usageFormat';

const props = defineProps<{ message: ChatMessage }>();
const chat = useChatStore();
const usage = useUsageStore();
const settings = useSettingsStore();

const conversationId = computed(() => chat.activeConversationId);

// Hover (default) vs. click to open the explanation popover — a global, persisted
// preference. The :key on the popover forces a clean re-bind when the mode flips.
const hoverEnabled = computed(() => settings.usageChipHover);
const popoverTrigger = computed<'hover' | 'click'>(() => (hoverEnabled.value ? 'hover' : 'click'));

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
const hasBreakdown = computed(() => bySource.value.length > 0);
</script>

<template>
  <div class="usage-chip-wrap">
    <ElPopover
      :key="popoverTrigger"
      :trigger="popoverTrigger"
      placement="top-start"
      :width="340"
      :show-after="hoverEnabled ? 120 : 0"
      popper-class="usage-popover"
    >
      <template #reference>
        <button type="button" class="usage-chip">
          <span>{{ formatTokens(totalTokens) }} tok</span>
          <span v-if="cost !== null" class="sep">·</span>
          <span v-if="cost !== null" class="cost">{{ formatUsd(cost) }}</span>
          <span v-if="cachedTokens > 0" class="sep">·</span>
          <span v-if="cachedTokens > 0" class="cached">{{ formatPercent(cacheRate) }} cached</span>
          <Icon icon="mdi:information-outline" class="chip-info" />
        </button>
      </template>

      <div class="usage-pop">
        <div class="up-title">What these numbers mean</div>

        <dl class="up-defs">
          <div class="up-row">
            <dt><b>{{ formatTokens(totalTokens) }} tok</b></dt>
            <dd>
              Total tokens for <em>this turn only</em> — your message plus the system
              prompt, tools and conversation history (input) and the reply (output),
              summed across the model and any tools or sub-agents it used.
            </dd>
          </div>

          <div v-if="cost !== null" class="up-row">
            <dt><b class="cost">{{ formatUsd(cost) }}</b></dt>
            <dd>
              Estimated cost for this turn. Cached input is billed at a fraction of
              the normal rate, so the cost is usually far below what the raw token
              count suggests.
            </dd>
          </div>

          <div v-if="cachedTokens > 0" class="up-row">
            <dt><b class="cached">{{ formatPercent(cacheRate) }} cached</b></dt>
            <dd>
              Share of the <em>input</em> reused from the prompt cache instead of being
              reprocessed — higher is cheaper and faster. It dips right after the
              history is compacted, then climbs back as the cache warms up.
            </dd>
          </div>
        </dl>

        <div v-if="hasBreakdown" class="up-breakdown">
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

        <label class="up-hover-toggle">
          <ElSwitch
            :model-value="hoverEnabled"
            size="small"
            @update:model-value="(v: string | number | boolean) => settings.setUsageChipHover(!!v)"
          />
          <span class="up-hover-label">Show on hover</span>
          <span class="up-hover-hint">{{ hoverEnabled ? 'on — hover to open' : 'off — click to open' }}</span>
        </label>
      </div>
    </ElPopover>
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
  cursor: pointer;
}
.usage-chip:hover { color: var(--text-secondary); }
.usage-chip .sep { opacity: 0.5; }
.usage-chip .cost { color: var(--primary-color); }
.usage-chip .cached { color: var(--success-color, #10b981); }
.chip-info { font-size: 13px; opacity: 0.6; }
.usage-chip:hover .chip-info { opacity: 1; }
</style>

<!-- Non-scoped: the popover content is teleported to <body>, so style it via the
     popper-class instead of relying on scoped data attributes. -->
<style>
.usage-popover.el-popover.el-popper { padding: 12px 14px; }
.usage-popover .usage-pop { font-size: 0.78rem; color: var(--text-primary); }
.usage-popover .up-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--text-primary);
}
.usage-popover .up-defs { margin: 0; display: flex; flex-direction: column; gap: 8px; }
.usage-popover .up-row { display: flex; flex-direction: column; gap: 2px; }
.usage-popover .up-row dt { margin: 0; font-family: var(--el-font-family, monospace); }
.usage-popover .up-row dd {
  margin: 0;
  color: var(--text-secondary);
  line-height: 1.4;
}
.usage-popover .up-row .cost { color: var(--primary-color); }
.usage-popover .up-row .cached { color: var(--success-color, #10b981); }

.usage-popover .up-breakdown {
  margin-top: 10px;
  border-top: 1px solid var(--border-color);
  padding-top: 8px;
  font-size: 0.74rem;
}
.usage-popover .bd-head, .usage-popover .bd-row {
  display: grid;
  grid-template-columns: 1fr 70px 70px;
  gap: 8px;
  align-items: center;
  padding: 3px 2px;
}
.usage-popover .bd-head { color: var(--text-tertiary, var(--text-secondary)); }
.usage-popover .bd-row { color: var(--text-primary); }
.usage-popover .num { text-align: right; font-variant-numeric: tabular-nums; }
.usage-popover .bd-src { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.usage-popover .bd-type {
  font-size: 0.62rem;
  text-transform: uppercase;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--hover-bg);
  color: var(--text-secondary);
}
.usage-popover .t-reasoning { color: var(--primary-color); }
.usage-popover .t-subagent { color: var(--danger-color, #ef4444); }
.usage-popover .t-tool { color: var(--warning-color, #f59e0b); }

.usage-popover .up-hover-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid var(--border-color);
  cursor: pointer;
}
.usage-popover .up-hover-label { color: var(--text-primary); }
.usage-popover .up-hover-hint { margin-left: auto; font-size: 0.7rem; color: var(--text-tertiary, var(--text-secondary)); }
</style>
