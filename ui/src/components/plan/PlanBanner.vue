<script setup lang="ts">
import { computed } from 'vue';
import { ElButton } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useChatStore } from '../../stores/chat';

const emit = defineEmits<{
  review: [];
  answer: [];
  accept: [];
  cancel: [];
}>();

const chatStore = useChatStore();
const plan = computed(() => chatStore.activePendingPlan);
const question = computed(() => chatStore.activePendingQuestion);
const questionCount = computed(() => question.value?.questions.length ?? 0);
</script>

<template>
  <Transition name="plan-banner-fade">
    <div v-if="plan" class="plan-banner">
      <Icon icon="mdi:clipboard-check-outline" class="plan-banner-icon" />
      <span class="plan-banner-text">
        Plan ready: <strong>{{ plan.filename }}</strong> — review before executing.
      </span>
      <div class="plan-banner-actions">
        <ElButton size="small" @click="emit('review')">Review</ElButton>
        <ElButton size="small" @click="emit('cancel')">Cancel</ElButton>
        <ElButton size="small" type="primary" @click="emit('accept')">Accept</ElButton>
      </div>
    </div>
    <div v-else-if="question" class="plan-banner">
      <Icon icon="mdi:help-circle-outline" class="plan-banner-icon" />
      <span class="plan-banner-text">
        The agent has {{ questionCount }}
        question{{ questionCount === 1 ? '' : 's' }} for you.
      </span>
      <div class="plan-banner-actions">
        <ElButton size="small" type="primary" @click="emit('answer')">Answer</ElButton>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.plan-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 16px 8px;
  padding: 10px 14px;
  background: var(--surface-color);
  border: 1px solid var(--warning-color);
  border-radius: 10px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.plan-banner-icon {
  flex-shrink: 0;
  font-size: 18px;
  color: var(--warning-color);
}

.plan-banner-text {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: var(--text-primary);
}

.plan-banner-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.plan-banner-fade-enter-active,
.plan-banner-fade-leave-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}

.plan-banner-fade-enter-from,
.plan-banner-fade-leave-to {
  opacity: 0;
  transform: translateY(6px);
}
</style>
