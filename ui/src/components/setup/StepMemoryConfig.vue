<script setup lang="ts">
import { ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';

defineProps<{
  enabled: boolean;
}>();

const emit = defineEmits<{
  update: [enabled: boolean];
}>();
</script>

<template>
  <div class="step-memory">
    <h2>Memory <span class="optional">(optional)</span></h2>
    <p class="subtitle">
      Let the agent learn from your conversations and remember durable facts about you.
    </p>

    <div class="memory-card">
      <div class="memory-card-head">
        <Icon icon="mdi:brain" class="memory-icon" />
        <div>
          <div class="memory-title">Enable conversation memory</div>
          <div class="memory-sub">You can change this anytime in Settings → Memory.</div>
        </div>
        <ElSwitch
          :model-value="enabled"
          @update:model-value="emit('update', Boolean($event))"
        />
      </div>

      <ul class="memory-benefits">
        <li>
          <Icon icon="mdi:lightning-bolt-outline" />
          <span>
            <strong>Short-term memory</strong> summarizes each conversation — the agent
            recalls your habits, repeated commands, and past mistakes to avoid repeating them.
          </span>
        </li>
        <li>
          <Icon icon="mdi:account-heart-outline" />
          <span>
            <strong>Long-term memory</strong> remembers durable facts about you (name,
            preferences) across all of this profile's conversations.
          </span>
        </li>
      </ul>

      <div class="memory-cost">
        <Icon icon="mdi:alert-outline" />
        <span>
          Heads up: memory runs an extra background AI call (using your low-cost model
          group) roughly every 100k tokens of conversation, so it <strong>consumes
          additional tokens</strong>. It's off by default — enable it only if the benefit
          is worth the extra usage.
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.step-memory { max-width: 640px; }
.step-memory h2 { margin: 0 0 4px 0; }
.step-memory h2 .optional { font-weight: 400; font-size: 0.8em; color: var(--text-secondary); }
.subtitle { margin: 0 0 20px 0; color: var(--text-secondary); }

.memory-card {
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 18px;
  background: var(--surface-color);
}
.memory-card-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.memory-icon { font-size: 28px; color: var(--primary-color); flex-shrink: 0; }
.memory-title { font-weight: 600; color: var(--text-primary); }
.memory-sub { font-size: 0.8rem; color: var(--text-secondary); }
.memory-card-head :deep(.el-switch) { margin-left: auto; }

.memory-benefits {
  list-style: none;
  margin: 18px 0 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.memory-benefits li {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  font-size: 0.88rem;
  color: var(--text-secondary);
}
.memory-benefits li :deep(svg) { font-size: 18px; color: var(--primary-color); flex-shrink: 0; margin-top: 1px; }
.memory-benefits strong { color: var(--text-primary); }

.memory-cost {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-top: 18px;
  padding: 12px;
  border-radius: 8px;
  background: var(--hover-bg);
  border: 1px solid var(--border-color);
  font-size: 0.82rem;
  color: var(--text-secondary);
}
.memory-cost :deep(svg) { font-size: 18px; color: var(--warning-color, #f59e0b); flex-shrink: 0; margin-top: 1px; }
</style>
