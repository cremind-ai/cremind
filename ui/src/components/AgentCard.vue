<script setup lang="ts">
import { ref, computed } from 'vue';
import type { AgentCard } from '@a2a-js/sdk';
import { ElButton, ElTooltip } from 'element-plus';
import { Icon } from '@iconify/vue';

const props = withDefaults(defineProps<{
  agentCard: AgentCard | null;
  isConnected: boolean;
  compact?: boolean;
}>(), {
  compact: false,
});

const emit = defineEmits<{
  connect: [];
  disconnect: [];
}>();

const isExpanded = ref(false);

const supportsStreaming = computed(() => {
  return props.agentCard?.capabilities?.streaming || false;
});

const handleConnectionToggle = () => {
  if (props.isConnected) {
    emit('disconnect');
  } else {
    emit('connect');
  }
};

const toggleExpand = () => {
  if (props.compact) return;
  isExpanded.value = !isExpanded.value;
};
</script>

<template>
  <div class="agent-card" :class="{ 'disconnected': !isConnected, 'expanded': isExpanded, 'compact': compact }">
    <!-- Compact Header (Always Visible) -->
    <ElTooltip
      :content="`${agentCard?.name || 'Agent'} — ${isConnected ? 'Connected' : 'Disconnected'}`"
      placement="right"
      :show-after="300"
      :disabled="!compact"
    >
      <div class="agent-header" @click="toggleExpand">
        <div class="agent-header-top">
          <div class="agent-avatar">
            <img src="/agent-avatar.png" alt="Agent" class="agent-avatar-img" />
            <span v-if="compact" class="avatar-status-dot" :class="{ 'connected': isConnected }"></span>
          </div>
          <div class="agent-brief" v-if="!compact">
            <h3 class="agent-name">{{ agentCard?.name || 'Agent' }}</h3>
            <div class="agent-status-row">
              <span class="status-dot" :class="{ 'connected': isConnected }"></span>
              <span class="status-text">{{ isConnected ? 'Connected' : 'Disconnected' }}</span>
            </div>
          </div>
          <div v-if="!compact && $slots['header-action']" class="agent-header-action" @click.stop>
            <slot name="header-action" />
          </div>
        </div>
        <div v-if="!compact" class="agent-header-bottom">
          <Icon icon="mdi:chevron-down" class="expand-icon" />
        </div>
        <div v-else-if="$slots['header-action']" class="agent-header-bottom" @click.stop>
          <slot name="header-action" />
        </div>
      </div>
    </ElTooltip>

    <!-- Expanded Details (Shown when expanded) -->
    <transition name="expand">
      <div v-if="isExpanded && !compact" class="agent-details">
        <p v-if="agentCard?.description" class="agent-description">{{ agentCard.description }}</p>

        <div class="agent-meta">
          <div v-if="agentCard?.version" class="meta-item">
            <Icon icon="mdi:tag" class="meta-icon" />
            <span class="meta-label">Version:</span>
            <span class="meta-value">{{ agentCard.version }}</span>
          </div>
          <div v-if="supportsStreaming" class="meta-item">
            <Icon icon="mdi:flash" class="meta-icon" />
            <span class="meta-label">Streaming:</span>
            <span class="meta-value">Enabled</span>
          </div>
        </div>

        <ElButton
          :type="isConnected ? 'danger' : 'primary'"
          size="small"
          @click.stop="handleConnectionToggle"
          class="connect-button"
        >
          {{ isConnected ? 'Disconnect' : 'Connect' }}
        </ElButton>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.agent-card {
  background: var(--surface-color);
  border-bottom: 1px solid var(--border-color);
  transition: all 0.2s ease;
}

.agent-card.disconnected {
  border-left: 3px solid var(--danger-color);
}

.agent-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 16px;
  cursor: pointer;
  transition: all 0.2s ease;
}

.agent-header:hover {
  background: var(--hover-bg);
}

.agent-header-top {
  display: flex;
  align-items: center;
  gap: 12px;
}

.agent-header-action {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

.agent-header-bottom {
  display: flex;
  align-items: center;
  justify-content: center;
}

.agent-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--primary-color);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
  position: relative;
}

.agent-avatar-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
  border-radius: 50%;
}

.avatar-status-dot {
  position: absolute;
  bottom: -1px;
  right: -1px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--danger-color);
  border: 1.5px solid var(--surface-color);
  z-index: 1;
}

.avatar-status-dot.connected {
  background: var(--success-color);
}

.agent-card.compact .agent-header {
  align-items: center;
  padding: 12px 0;
  cursor: default;
  gap: 8px;
}

.agent-card.compact .agent-header-top {
  justify-content: center;
}

.agent-card.compact .agent-header:hover {
  background: transparent;
}

.agent-brief {
  flex: 1;
  min-width: 0;
}

.agent-name {
  margin: 0 0 2px 0;
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-status-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--danger-color);
  flex-shrink: 0;
}

.status-dot.connected {
  background: var(--success-color);
}

.status-text {
  font-size: 0.75rem;
  color: var(--text-secondary);
}

.expand-icon {
  font-size: 18px;
  color: var(--text-tertiary);
  transition: transform 0.2s ease;
  flex-shrink: 0;
}

.agent-card.expanded .expand-icon {
  transform: rotate(180deg);
}

.agent-details {
  padding: 0 16px 12px 16px;
  border-top: 1px solid var(--border-color);
}

.agent-description {
  margin: 12px 0;
  font-size: 0.8rem;
  color: var(--text-secondary);
  line-height: 1.5;
}

.agent-meta {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}

.meta-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.75rem;
}

.meta-icon {
  font-size: 14px;
  color: var(--text-tertiary);
}

.meta-label {
  color: var(--text-tertiary);
}

.meta-value {
  color: var(--text-primary);
  font-weight: 500;
}

.connect-button {
  width: 100%;
}

/* Expand transition */
.expand-enter-active,
.expand-leave-active {
  transition: all 0.3s ease;
  overflow: hidden;
}

.expand-enter-from,
.expand-leave-to {
  opacity: 0;
  max-height: 0;
}

.expand-enter-to,
.expand-leave-from {
  opacity: 1;
  max-height: 500px;
}

/* Override Element Plus button styles */
.agent-card :deep(.el-button) {
  border-radius: 6px;
  font-weight: 500;
}

.agent-card :deep(.el-button--primary) {
  background: var(--primary-color);
  border-color: var(--primary-color);
}

.agent-card :deep(.el-button--primary:hover) {
  background: var(--primary-light);
  border-color: var(--primary-light);
}

.agent-card :deep(.el-button--danger) {
  background: var(--danger-color);
  border-color: var(--danger-color);
}

.agent-card :deep(.el-button--danger:hover) {
  opacity: 0.9;
}
</style>
