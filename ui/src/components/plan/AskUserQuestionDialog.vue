<script setup lang="ts">
import { computed, reactive, watch } from 'vue';
import { ElDialog, ElButton, ElInput } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useChatStore } from '../../stores/chat';
import type { PendingQuestion } from '../../stores/chat';

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const chatStore = useChatStore();
const pending = computed(() => chatStore.activePendingQuestion);

const open = computed({
  get: () => props.modelValue && !!pending.value,
  set: (v: boolean) => emit('update:modelValue', v),
});

interface Answer {
  selected: string[];
  freeText: string;
}
const answers = reactive<Record<string, Answer>>({});

// (Re)initialize the answer state whenever a new question set arrives.
watch(
  () => pending.value?.createdAt,
  () => {
    for (const k of Object.keys(answers)) delete answers[k];
    for (const q of pending.value?.questions ?? []) {
      answers[q.id] = { selected: [], freeText: '' };
    }
  },
  { immediate: true },
);

function toggleOption(q: PendingQuestion, label: string) {
  const a = answers[q.id];
  if (!a) return;
  if (q.multiSelect) {
    const i = a.selected.indexOf(label);
    if (i >= 0) a.selected.splice(i, 1);
    else a.selected.push(label);
  } else {
    a.selected = a.selected[0] === label ? [] : [label];
  }
}

function isSelected(q: PendingQuestion, label: string): boolean {
  return answers[q.id]?.selected.includes(label) ?? false;
}

const canSubmit = computed(() => {
  const qs = pending.value?.questions ?? [];
  if (!qs.length) return false;
  return qs.every((q) => {
    const a = answers[q.id];
    return !!a && (a.selected.length > 0 || a.freeText.trim().length > 0);
  });
});

function composeAnswerText(): string {
  const qs = pending.value?.questions ?? [];
  const lines = ['Here are my answers to your questions:', ''];
  qs.forEach((q, i) => {
    const a = answers[q.id];
    const parts: string[] = [];
    if (a?.selected.length) parts.push(a.selected.join(', '));
    if (a?.freeText.trim()) parts.push(a.freeText.trim());
    lines.push(`${i + 1}. ${q.question}`);
    lines.push(`   Answer: ${parts.join('; ') || '(no answer)'}`);
    lines.push('');
  });
  return lines.join('\n').trim();
}

async function submit() {
  if (!canSubmit.value) return;
  const text = composeAnswerText();
  // sendMessage clears the pending question optimistically → the dialog closes.
  await chatStore.sendMessage(text, { mode: 'plan' });
}

function dismiss() {
  emit('update:modelValue', false);
}
</script>

<template>
  <ElDialog
    v-model="open"
    title="A few questions before I plan"
    width="560px"
    :close-on-click-modal="false"
  >
    <div v-if="pending" class="q-body">
      <div v-for="q in pending.questions" :key="q.id" class="q-block">
        <div class="q-title">{{ q.question }}</div>
        <div v-if="q.description" class="q-desc">{{ q.description }}</div>
        <div class="q-options">
          <button
            v-for="opt in q.options"
            :key="opt.label"
            type="button"
            class="q-option"
            :class="{ selected: isSelected(q, opt.label) }"
            @click="toggleOption(q, opt.label)"
          >
            <Icon
              :icon="isSelected(q, opt.label) ? 'mdi:check-circle' : 'mdi:checkbox-blank-circle-outline'"
              class="q-option-icon"
            />
            <span class="q-option-text">
              <span class="q-option-label">{{ opt.label }}</span>
              <span v-if="opt.description" class="q-option-desc">{{ opt.description }}</span>
            </span>
          </button>
        </div>
        <ElInput
          v-if="q.allowFreeText"
          v-model="answers[q.id].freeText"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          :placeholder="q.options.length ? 'Other / more detail…' : 'Your answer…'"
          class="q-freetext"
        />
      </div>
    </div>
    <template #footer>
      <ElButton @click="dismiss">I'll type instead</ElButton>
      <ElButton type="primary" :disabled="!canSubmit" @click="submit">Send answers</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.q-body {
  max-height: 60vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.q-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.q-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.q-desc {
  font-size: 12px;
  color: var(--text-tertiary);
  line-height: 1.4;
}

.q-options {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.q-option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
  color: var(--text-primary);
  transition: all 0.15s ease;
}

.q-option:hover {
  border-color: var(--primary-color);
}

.q-option.selected {
  border-color: var(--primary-color);
  background: rgba(37, 99, 235, 0.08);
}

.q-option-icon {
  flex-shrink: 0;
  font-size: 16px;
  margin-top: 1px;
  color: var(--text-tertiary);
}

.q-option.selected .q-option-icon {
  color: var(--primary-color);
}

.q-option-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.q-option-label {
  font-size: 13px;
  font-weight: 500;
}

.q-option-desc {
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.35;
}

.q-freetext {
  margin-top: 2px;
}
</style>
