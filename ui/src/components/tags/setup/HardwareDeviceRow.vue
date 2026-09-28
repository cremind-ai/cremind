<script setup lang="ts">
/**
 * One gateway, bridge or tag in the Settings → Tags hardware lists: a name,
 * a status chip (always with words, never colour alone), one line of plain
 * facts, an optional note, and its actions — the main one as a button, the
 * rest in a "More" menu.
 */
import { computed } from 'vue';
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { Pill, RowAction } from '../../../utils/tagsSetupFormat';

const props = withDefaults(defineProps<{
  icon: string;
  title: string;
  pill: Pill;
  extraPill?: Pill | null;
  meta?: string[];
  note?: { tone: 'warning' | 'danger' | 'info'; text: string } | null;
  actions?: RowAction[];
  busy?: string;
}>(), { extraPill: null, meta: () => [], note: null, actions: () => [], busy: '' });

const emit = defineEmits<{ (e: 'action', key: string): void }>();

const inline = computed(() => props.actions.filter((a) => a.inline));
const menu = computed(() => props.actions.filter((a) => !a.inline));
const facts = computed(() => props.meta.filter(Boolean).join(' · '));
</script>

<template>
  <li class="hw-row">
    <div class="hw-lead">
      <slot name="lead"><Icon :icon="icon" class="hw-icon" aria-hidden="true" /></slot>
    </div>
    <div class="hw-main">
      <div class="hw-title-line">
        <span class="hw-title">{{ title }}</span>
        <ElTag :type="pill.type" size="small" effect="plain">{{ pill.label }}</ElTag>
        <ElTag v-if="extraPill" :type="extraPill.type" size="small" effect="plain">{{ extraPill.label }}</ElTag>
      </div>
      <div v-if="facts" class="hw-meta">{{ facts }}</div>
      <p v-if="note" class="hw-note" :class="note.tone">
        <Icon :icon="note.tone === 'danger' ? 'mdi:alert-octagon-outline' : note.tone === 'warning' ? 'mdi:alert-outline' : 'mdi:information-outline'" aria-hidden="true" />
        <span>{{ note.text }}</span>
      </p>
    </div>
    <div v-if="actions.length" class="hw-actions">
      <ElButton
        v-for="a in inline"
        :key="a.key"
        size="small"
        :type="a.primary ? 'primary' : a.danger ? 'danger' : undefined"
        :loading="busy === a.key"
        :disabled="a.disabled"
        :title="a.disabled ? a.reason : undefined"
        :aria-label="a.disabled && a.reason ? `${a.label} (${a.reason})` : undefined"
        @click="emit('action', a.key)"
      >
        <Icon v-if="a.icon && busy !== a.key" :icon="a.icon" class="btn-icon" aria-hidden="true" /> {{ a.label }}
      </ElButton>
      <ElDropdown v-if="menu.length" trigger="click" @command="(k: string) => emit('action', k)">
        <ElButton size="small" :loading="menu.some((a) => a.key === busy)" :aria-label="`More actions for ${title}`">
          <Icon icon="mdi:dots-horizontal" aria-hidden="true" />
        </ElButton>
        <template #dropdown>
          <ElDropdownMenu>
            <ElDropdownItem
              v-for="a in menu"
              :key="a.key"
              :command="a.key"
              :disabled="a.disabled"
              :divided="a.danger"
              :class="{ 'hw-danger': a.danger }"
            >
              <Icon v-if="a.icon" :icon="a.icon" class="menu-icon" aria-hidden="true" /> {{ a.label }}
            </ElDropdownItem>
          </ElDropdownMenu>
        </template>
      </ElDropdown>
    </div>
  </li>
</template>

<style scoped>
.hw-row {
  display: flex; align-items: flex-start; gap: 12px; flex-wrap: wrap;
  padding: 10px 12px; border: 1px solid var(--border-color); border-radius: 10px; background: var(--surface-color);
}
.hw-lead { flex-shrink: 0; display: flex; align-items: center; justify-content: center; }
.hw-icon {
  width: 36px; height: 36px; padding: 7px; box-sizing: border-box; border-radius: 9px;
  background: var(--hover-bg); color: var(--primary-color);
}
.hw-main { flex: 1; min-width: 200px; display: flex; flex-direction: column; gap: 3px; }
.hw-title-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.hw-title { font-weight: 600; color: var(--text-primary); overflow-wrap: anywhere; }
.hw-meta { font-size: 0.82rem; color: var(--text-secondary); line-height: 1.45; }
.hw-note { display: flex; align-items: flex-start; gap: 6px; margin: 4px 0 0; font-size: 0.82rem; line-height: 1.45; color: var(--text-primary); }
.hw-note :deep(svg) { flex-shrink: 0; margin-top: 2px; }
.hw-note.warning :deep(svg) { color: var(--el-color-warning); }
.hw-note.danger :deep(svg) { color: var(--el-color-danger); }
.hw-note.info :deep(svg) { color: var(--primary-color); }
.hw-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.hw-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 4px; }
.menu-icon { margin-right: 6px; }
:global(.hw-danger) { color: var(--el-color-danger) !important; }
</style>
