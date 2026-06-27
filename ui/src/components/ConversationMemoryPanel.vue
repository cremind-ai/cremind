<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElDialog, ElButton, ElProgress, ElEmpty, ElTooltip } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  fetchConversationMemory,
  triggerConversationMemory,
  type ConversationMemory,
} from '../services/conversationMemory';

const props = defineProps<{
  modelValue: boolean;
  conversationId: string | null;
}>();

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void;
}>();

const settings = useSettingsStore();
const router = useRouter();
const route = useRoute();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
});

// Close the panel and jump to the Memory section of Settings → Config.
function openMemorySettings(): void {
  const profile = String(route.params.profile || settings.profileId || '');
  if (!profile) return;
  visible.value = false;
  router.push({
    name: 'user-config-settings',
    params: { profile },
    query: { section: 'memory' },
  });
}

const memory = ref<ConversationMemory | null>(null);
const loading = ref(false);
const triggering = ref(false);
const error = ref<string | null>(null);

const progressPercent = computed(() => {
  const p = memory.value?.token_progress;
  if (!p || !p.threshold) return 0;
  return Math.min(100, Math.round((p.current / p.threshold) * 100));
});

const tokensRemaining = computed(() => {
  const p = memory.value?.token_progress;
  if (!p) return 0;
  return Math.max(0, p.threshold - p.current);
});

// The threshold as a percentage of the model's context window (e.g. 85%).
const thresholdPercentOfWindow = computed(() => {
  const p = memory.value?.token_progress;
  if (!p || !p.context_window) return 0;
  return Math.round((p.threshold / p.context_window) * 100);
});

async function load(): Promise<void> {
  if (!props.conversationId) return;
  loading.value = true;
  error.value = null;
  try {
    memory.value = await fetchConversationMemory(
      settings.agentUrl, settings.authToken, props.conversationId,
    );
  } catch (e: any) {
    error.value = e?.message || 'Failed to load memory';
  } finally {
    loading.value = false;
  }
}

// Fold now (synchronous server-side), then refresh to show the result.
async function handleTrigger(): Promise<void> {
  if (!props.conversationId || triggering.value) return;
  triggering.value = true;
  error.value = null;
  try {
    await triggerConversationMemory(
      settings.agentUrl, settings.authToken, props.conversationId,
    );
    await load();
  } catch (e: any) {
    error.value = e?.message || 'Failed to update memory';
  } finally {
    triggering.value = false;
  }
}

function formatTime(ts: number): string {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return '';
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) load();
  },
);
</script>

<template>
  <ElDialog
    v-model="visible"
    width="640px"
    :close-on-click-modal="true"
    class="memory-dialog"
  >
    <template #header="{ titleId, titleClass }">
      <div class="memory-dialog-header">
        <span :id="titleId" :class="titleClass">Conversation Memory</span>
        <ElTooltip content="Open Memory settings">
          <button
            type="button"
            class="memory-settings-btn"
            aria-label="Open Memory settings"
            @click="openMemorySettings"
          >
            <Icon icon="mdi:cog-outline" />
          </button>
        </ElTooltip>
      </div>
    </template>

    <div v-if="loading" class="memory-loading">
      <Icon icon="mdi:loading" class="spin" /> Loading memory…
    </div>

    <div v-else-if="error" class="memory-error">
      <Icon icon="mdi:alert-circle-outline" /> {{ error }}
    </div>

    <div v-else-if="memory" class="memory-body">
      <!-- Disabled hint -->
      <div v-if="!memory.enabled" class="memory-notice">
        <Icon icon="mdi:information-outline" />
        <span>
          Long-term memory is <strong>off</strong> for this profile. Enable it in
          <em>Settings → Memory</em> (requires Compaction). The running summary
          below is still kept for context. Use “Update now” to fold immediately.
        </span>
      </div>

      <!-- Token progress toward the next compaction fold -->
      <div class="memory-progress">
        <div class="memory-progress-head">
          <span class="memory-progress-label">
            Next compaction
            <ElTooltip
              content="How much of the model's context window the latest turn used. Once it reaches the threshold, the oldest turns fold into a running summary so the prompt stays within the window."
            >
              <Icon icon="mdi:help-circle-outline" class="hint-icon" />
            </ElTooltip>
          </span>
          <span class="memory-progress-value">
            {{ memory.token_progress.current.toLocaleString() }} /
            {{ memory.token_progress.threshold.toLocaleString() }} tokens
          </span>
        </div>
        <ElProgress
          :percentage="progressPercent"
          :stroke-width="10"
          :status="progressPercent >= 100 ? 'success' : ''"
        />
        <div class="memory-progress-foot">
          <span v-if="progressPercent >= 100">Threshold reached — the oldest turns will fold into the summary on the next turn.</span>
          <span v-else>≈ {{ tokensRemaining.toLocaleString() }} more tokens until the next fold.</span>
        </div>
        <!-- How the number is computed, so the mechanism is transparent. -->
        <p class="memory-progress-formula">
          <strong>How it's measured:</strong> the model's actual reported context size
          for the latest turn — the real prompt it processed (system prompt + tools +
          history + this turn's reasoning and tool results). Compaction is suggested
          once that reaches
          <strong>{{ thresholdPercentOfWindow }}%</strong> of this model's
          <strong>{{ memory.token_progress.context_window.toLocaleString() }}</strong>-token
          context window, leaving headroom for the response. It tracks the current
          turn, so it can rise or fall between turns.
        </p>
      </div>

      <!-- Short-term: this conversation's running summary -->
      <section class="memory-section">
        <h4>
          <Icon icon="mdi:lightning-bolt-outline" />
          Short-term memory
          <ElTooltip content="The running summary of this conversation — older turns folded into a compact summary the agent keeps for continuity.">
            <Icon icon="mdi:help-circle-outline" class="hint-icon" />
          </ElTooltip>
        </h4>
        <div v-if="memory.summary" class="memory-item">
          <div class="memory-item-content">{{ memory.summary }}</div>
          <div v-if="memory.last_compacted_at" class="memory-item-meta">
            Last folded {{ formatTime(memory.last_compacted_at) }}
          </div>
        </div>
        <ElEmpty v-else description="No summary yet — the conversation is still short" :image-size="60" />
      </section>

      <!-- Long-term: this profile -->
      <section class="memory-section">
        <h4>
          <Icon icon="mdi:brain" />
          Long-term memory
          <ElTooltip content="Durable facts about you (name, preferences). Shared across this profile's conversations.">
            <Icon icon="mdi:help-circle-outline" class="hint-icon" />
          </ElTooltip>
          <span class="count">{{ memory.long_term.length }}</span>
        </h4>
        <ul v-if="memory.long_term.length" class="memory-list">
          <li v-for="item in memory.long_term" :key="item.id" class="memory-item">
            <div class="memory-item-content">{{ item.content }}</div>
            <div class="memory-item-meta">{{ formatTime(item.created_at) }} · {{ item.token_count }} tok</div>
          </li>
        </ul>
        <ElEmpty v-else description="No long-term memory yet" :image-size="60" />
      </section>
    </div>

    <template #footer>
      <div class="memory-footer">
        <span v-if="triggering" class="extracting">
          <Icon icon="mdi:loading" class="spin" /> Folding…
        </span>
        <ElButton @click="load()" :disabled="loading || triggering">
          <Icon icon="mdi:refresh" /> Refresh
        </ElButton>
        <ElButton type="primary" @click="handleTrigger" :disabled="triggering || !conversationId">
          <Icon icon="mdi:cog-sync-outline" /> Update now
        </ElButton>
      </div>
    </template>
  </ElDialog>
</template>

<style scoped>
.memory-dialog-header {
  display: flex;
  align-items: center;
  gap: 10px;
}
.memory-settings-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  background: none;
  border: 1px solid transparent;
  border-radius: 6px;
  color: var(--text-secondary);
  cursor: pointer;
}
.memory-settings-btn:hover {
  color: var(--primary-color);
  border-color: var(--border-color);
  background: var(--hover-bg);
}
.memory-settings-btn :deep(svg) { font-size: 17px; }

.memory-loading,
.memory-error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px;
  color: var(--text-secondary);
  justify-content: center;
}
.memory-error { color: var(--danger-color, #ef4444); }

.memory-body {
  display: flex;
  flex-direction: column;
  gap: 18px;
  max-height: 60vh;
  overflow-y: auto;
}

.memory-notice {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  background: var(--hover-bg);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  font-size: 0.82rem;
  color: var(--text-secondary);
}

.memory-progress-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 4px;
}
.memory-progress-label { font-weight: 600; color: var(--text-primary); display: inline-flex; align-items: center; gap: 4px; }
.memory-progress-value { font-size: 0.8rem; color: var(--text-secondary); }
.memory-progress-foot { margin-top: 4px; font-size: 0.78rem; color: var(--text-tertiary, var(--text-secondary)); }
.memory-progress-formula {
  margin: 8px 0 0 0;
  padding: 8px 10px;
  background: var(--hover-bg);
  border-radius: 8px;
  font-size: 0.76rem;
  line-height: 1.5;
  color: var(--text-tertiary, var(--text-secondary));
}
.memory-progress-formula strong { color: var(--text-secondary); font-weight: 600; }
.memory-progress-formula em { font-style: italic; }

.memory-section h4 {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 8px 0;
  font-size: 0.95rem;
  color: var(--text-primary);
}
.memory-section h4 .count {
  margin-left: auto;
  font-size: 0.75rem;
  color: var(--text-secondary);
  background: var(--hover-bg);
  border-radius: 999px;
  padding: 1px 8px;
}
.hint-icon { color: var(--text-tertiary, var(--text-secondary)); font-size: 14px; }

.memory-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.memory-item {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--surface-color);
}
.memory-item-content { font-size: 0.85rem; color: var(--text-primary); white-space: pre-wrap; word-break: break-word; }
.memory-item-meta { margin-top: 6px; font-size: 0.72rem; color: var(--text-tertiary, var(--text-secondary)); }

.memory-footer { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
.memory-footer .extracting { margin-right: auto; display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 0.82rem; }

.spin { animation: memory-spin 1s linear infinite; }
@keyframes memory-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }
</style>
