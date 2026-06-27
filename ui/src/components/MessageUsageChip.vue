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
import { formatTokens, formatUsd, formatPercent, formatRatePerM } from '../utils/usageFormat';

const props = defineProps<{ message: ChatMessage }>();
const chat = useChatStore();
const usage = useUsageStore();
const settings = useSettingsStore();

const conversationId = computed(() => chat.activeConversationId);

// Hover (default) vs. click to open the explanation popover — a global, persisted
// preference. The :key on the popover forces a clean re-bind when the mode flips.
const hoverEnabled = computed(() => settings.usageChipHover);
const popoverTrigger = computed<'hover' | 'click'>(() => (hoverEnabled.value ? 'hover' : 'click'));

// Keep the (sometimes tall) popover fully inside the viewport. By default a
// top-placed popper anchors its bottom to the chip and grows upward, so for a
// chip mid-page the top can fall off-screen — unreachable even with an inner
// scrollbar. Enabling cross-axis preventOverflow with tether off lets Popper
// slide the box across the chip to stay on screen; the CSS max-height + scroll
// then makes every line reachable.
const popperOptions = {
  modifiers: [
    { name: 'preventOverflow', options: { altAxis: true, tether: false, padding: 8 } },
  ],
};

function ensureLoaded() {
  if (props.message.tokenUsage && conversationId.value) {
    usage.loadConversationUsage(conversationId.value); // coalesced + cached
  }
}
onMounted(ensureLoaded);
watch(() => props.message.id, ensureLoaded);

// A live turn completes with its server id surfaced as `backendId` and the
// usage cache freshly invalidated. Force a refetch so estimated cost + the
// worked formula appear immediately, not only after a page reload. Usage
// records are persisted before the `complete` event, so this fetch includes
// the just-finished turn. Only the one newly-completed bubble's backendId
// changes, so this is a single fetch per turn — not a storm.
watch(() => props.message.backendId, (id) => {
  if (id && conversationId.value) {
    usage.loadConversationUsage(conversationId.value, true);
  }
});

// The RequestUsage for this turn (cost + per-source), matched by message id.
// Usage records are keyed by the persisted (server) id; a live-streamed bubble
// keeps its optimistic client id and exposes the server id as `backendId`, so
// prefer that to match live turns (reloaded bubbles already carry the server id).
const request = computed(() => {
  const cid = conversationId.value;
  if (!cid) return null;
  const conv = usage.conversationUsage[cid];
  const mid = props.message.backendId ?? props.message.id;
  return conv?.requests.find(r => r.message_id === mid) ?? null;
});

const totalTokens = computed(() =>
  request.value?.total_tokens ?? props.message.tokenUsage?.totalTokens ?? 0);

const cachedTokens = computed(() => {
  if (request.value) return request.value.cache_read_input_tokens;
  const u = props.message.tokenUsage;
  return u ? u.cacheReadTokens : 0;
});

// Fresh (uncached) input alone — the cache-read tokens are tracked separately above.
const newInputTokens = computed(() => {
  if (request.value) return request.value.input_tokens;
  return props.message.tokenUsage?.inputTokens ?? 0;
});

const cacheWriteTokens = computed(() => {
  if (request.value) return request.value.cache_creation_input_tokens;
  return props.message.tokenUsage?.cacheCreationTokens ?? 0;
});

const outputTokens = computed(() => {
  if (request.value) return request.value.output_tokens;
  return props.message.tokenUsage?.outputTokens ?? 0;
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

const modelLabel = computed(() => request.value?.model ?? '');

// One worked cost term per token category, plugging in the model's per-1M rate.
// Only built when the backend exposed a single rate card for the turn (`rates`);
// a category line is dropped if its rate is unknown. Empty ⇒ template falls back
// to the symbolic formula. usd is derived the same way as the backend component
// (tokens / 1M × rate), so the terms sum to the frozen `cost` total.
interface CostTerm { label: string; tokens: number; rate: number; usd: number }
const isNum = (x: unknown): x is number => typeof x === 'number' && isFinite(x);
const costTerms = computed<CostTerm[]>(() => {
  const r = request.value?.rates;
  if (!r) return [];
  const terms: CostTerm[] = [];
  const push = (label: string, tokens: number, rate: number | null) => {
    if (!isNum(rate)) return;
    terms.push({ label, tokens, rate, usd: (tokens / 1_000_000) * rate });
  };
  push('new input', newInputTokens.value, r.input_per_1m);
  push('cached', cachedTokens.value, r.cache_read_per_1m);
  push('cache-write', cacheWriteTokens.value, r.cache_write_per_1m);
  push('output', outputTokens.value, r.output_per_1m);
  // If a category actually has tokens but no rate, we can't reproduce the total
  // exactly — drop to the symbolic formula rather than show a misleading sum.
  const missing =
    (cachedTokens.value > 0 && !isNum(r.cache_read_per_1m)) ||
    (cacheWriteTokens.value > 0 && !isNum(r.cache_write_per_1m)) ||
    (newInputTokens.value > 0 && !isNum(r.input_per_1m)) ||
    (outputTokens.value > 0 && !isNum(r.output_per_1m));
  return missing ? [] : terms;
});
</script>

<template>
  <div class="usage-chip-wrap">
    <ElPopover
      :key="popoverTrigger"
      :trigger="popoverTrigger"
      placement="top-start"
      :width="340"
      :show-after="hoverEnabled ? 120 : 0"
      :popper-options="popperOptions"
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
            <div class="up-formula">
              {{ formatTokens(totalTokens) }}
              = {{ formatTokens(newInputTokens) }} new input
              + {{ formatTokens(cachedTokens) }} cached
              + {{ formatTokens(cacheWriteTokens) }} cache-write
              + {{ formatTokens(outputTokens) }} output
            </div>
          </div>

          <div v-if="cost !== null" class="up-row">
            <dt><b class="cost">{{ formatUsd(cost) }}</b></dt>
            <dd>
              Estimated cost for this turn. Cached input is billed at a fraction of
              the normal rate, so the cost is usually far below what the raw token
              count suggests.
            </dd>
            <div v-if="costTerms.length" class="up-formula">
              <template v-for="(t, i) in costTerms" :key="t.label">
                <span :class="{ 'up-formula-cont': i > 0 }">{{ i === 0 ? 'cost = ' : '+ ' }}{{ formatTokens(t.tokens) }} × {{ formatRatePerM(t.rate) }} = {{ formatUsd(t.usd) }} {{ t.label }}</span><br />
              </template>
              <span class="up-formula-cont up-formula-note">= {{ formatUsd(cost) }} total — per-1M rates<template v-if="modelLabel"> for {{ modelLabel }}</template></span>
            </div>
            <div v-else class="up-formula">
              cost = (new-input × in-rate)<br />
              <span class="up-formula-cont">+ (cached × cache-rate)</span><br />
              <span class="up-formula-cont">+ (cache-write × write-rate)</span><br />
              <span class="up-formula-cont">+ (output × out-rate)</span><br />
              <span class="up-formula-note">rates are per 1M tokens; cached bills at a
              fraction (~10%) of the input rate</span>
            </div>
          </div>

          <div v-if="cachedTokens > 0" class="up-row">
            <dt><b class="cached">{{ formatPercent(cacheRate) }} cached</b></dt>
            <dd>
              Share of the <em>input</em> reused from the prompt cache instead of being
              reprocessed — higher is cheaper and faster. It dips right after the
              history is compacted, then climbs back as the cache warms up.
            </dd>
            <div class="up-formula">
              {{ formatPercent(cacheRate) }}
              = {{ formatTokens(cachedTokens) }} cached
              ÷ ({{ formatTokens(newInputTokens) }} new input
              + {{ formatTokens(cachedTokens) }} cached)
            </div>
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
.usage-popover .usage-pop {
  font-size: 0.78rem;
  color: var(--text-primary);
  /* The popover can be taller than the space above/below the chip (it opens on
     whichever side has more room). Cap it to the viewport and scroll the
     overflow so the whole box stays on screen and every line is reachable —
     without a cap the top can sit above the viewport, unreachable by scrolling. */
  max-height: min(70vh, 520px);
  overflow-y: auto;
  overflow-x: hidden;
  overscroll-behavior: contain;
  /* Keep the scrollbar from sitting on top of the text. */
  padding-right: 4px;
}
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

/* How each number is computed — muted monospace under the prose. */
.usage-popover .up-formula {
  margin-top: 3px;
  font-family: var(--el-font-family, monospace);
  font-size: 0.72rem;
  color: var(--text-tertiary, var(--text-secondary));
  line-height: 1.45;
}
.usage-popover .up-formula-cont { padding-left: 2.4em; }
.usage-popover .up-formula-note { font-style: italic; opacity: 0.85; }

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
