<script setup lang="ts">
/**
 * Where the agent may use your documents (`options.allow_in`). The web app
 * and CLI are on by default; messaging channels and group rooms are off,
 * because a channel may answer whoever writes to it and a room's answers are
 * read by other people. Each switch saves at once, and takes effect from the
 * next message (the tool list is fixed for a run).
 */
import { ElSwitch } from 'element-plus';
import type { DocumentsOptions } from '../../services/documentsApi';

type AllowIn = DocumentsOptions['allow_in'];

defineProps<{ allowIn: AllowIn; saving?: boolean }>();
const emit = defineEmits<{ save: [patch: Partial<AllowIn>] }>();

const ROWS: { key: keyof AllowIn; title: string; detail: string }[] = [
  {
    key: 'web_cli',
    title: 'Web app, CLI and your automations',
    detail: 'Conversations only you can see.',
  },
  {
    key: 'channels',
    title: 'Messaging channels',
    detail: 'Telegram, Zalo and other channels. Anyone the channel answers could ask about your files.',
  },
  {
    key: 'rooms',
    title: 'Group rooms',
    detail: 'Answers, and the passages they quote, are read by everyone in the room. The sources list there leaves out file paths and links.',
  },
];
</script>

<template>
  <div class="access">
    <div v-for="row in ROWS" :key="row.key" class="access-row">
      <div>
        <strong>{{ row.title }}</strong>
        <p class="hint">{{ row.detail }}</p>
      </div>
      <ElSwitch
        :model-value="allowIn[row.key]"
        :disabled="saving"
        :aria-label="row.title"
        @update:model-value="(v: string | number | boolean) => emit('save', { [row.key]: !!v })"
      />
    </div>
  </div>
</template>

<style scoped>
.access { display: flex; flex-direction: column; gap: 10px; }
.access-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.access-row strong { font-size: 0.875rem; color: var(--text-primary); font-weight: 600; }
.hint { margin: 2px 0 0; font-size: 0.8rem; color: var(--text-secondary); line-height: 1.45; }
</style>
