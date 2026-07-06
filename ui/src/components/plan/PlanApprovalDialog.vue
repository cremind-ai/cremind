<script setup lang="ts">
import { computed } from 'vue';
import { ElDialog, ElButton } from 'element-plus';
import { useChatStore } from '../../stores/chat';
import { useSettingsStore } from '../../stores/settings';
import { createChatMarked } from '../../utils/markdown';

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{
  'update:modelValue': [value: boolean];
  accept: [];
  cancel: [];
}>();

const chatStore = useChatStore();
const settingsStore = useSettingsStore();

const plan = computed(() => chatStore.activePendingPlan);

const open = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
});

const resolveApiUrl = (href: string): string => {
  if (!href) return href;
  const base = settingsStore.agentUrl.replace(/\/$/, '');
  if (href.startsWith('/api/')) return base + href;
  if (!href.startsWith('http://') && !href.startsWith('https://')) {
    return `${base}/api/files/open?path=${encodeURIComponent(href)}`;
  }
  return href;
};

const marked = createChatMarked(resolveApiUrl);
const renderedPlan = computed(() =>
  plan.value?.markdown ? (marked.parse(plan.value.markdown) as string) : '',
);

function onAccept() {
  emit('accept');
}
function onCancel() {
  emit('cancel');
}
</script>

<template>
  <ElDialog
    v-model="open"
    :title="plan?.title || plan?.filename || 'Proposed plan'"
    width="720px"
    :close-on-click-modal="false"
  >
    <div class="plan-dialog-body markdown-body" v-html="renderedPlan"></div>
    <template #footer>
      <ElButton @click="onCancel">Cancel</ElButton>
      <ElButton type="primary" @click="onAccept">Accept &amp; execute</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.plan-dialog-body {
  max-height: 65vh;
  overflow-y: auto;
  font-size: 14px;
  line-height: 1.6;
  color: var(--text-primary);
}
</style>
