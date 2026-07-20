<script setup lang="ts">
/**
 * Kebab "more actions" menu for an event rule, shown on rule cards and run cards.
 * Emits an intent (`select`) that TasksBoard maps to the extracted dialogs / APIs;
 * `open-change` lets a host card pin its hover bar while the teleported menu is open.
 */
import { computed } from 'vue';
import { ElDropdown, ElDropdownItem, ElDropdownMenu } from 'element-plus';
import { Icon } from '@iconify/vue';
import type { BoardSubscription, RuleAction } from './boardTypes';

const props = defineProps<{
  sub: BoardSubscription;
  listenerRunning?: boolean;
}>();

const emit = defineEmits<{
  (e: 'select', action: RuleAction): void;
  (e: 'open-change', open: boolean): void;
}>();

const isSchedule = computed(() => props.sub.kind === 'schedule');
// Edit + pause/resume only make sense for a live (active|paused) schedule.
const scheduleActionable = computed(
  () =>
    props.sub.kind === 'schedule' &&
    (props.sub.scheduleStatus === 'active' || props.sub.scheduleStatus === 'paused'),
);
// Whether the rule is currently paused, per kind: schedule uses its status;
// skill/file carry a persisted `paused` flag.
const isPaused = computed(() => {
  if (props.sub.kind === 'schedule') return props.sub.scheduleStatus === 'paused';
  return props.sub.paused;
});

function onCommand(cmd: RuleAction) {
  emit('select', cmd);
}
</script>

<template>
  <ElDropdown
    trigger="click"
    popper-class="rule-menu-popper"
    @command="onCommand"
    @visible-change="(v: boolean) => emit('open-change', v)"
  >
    <button type="button" class="rule-kebab" title="Event actions" @click.stop>
      <Icon icon="mdi:dots-vertical" />
    </button>
    <template #dropdown>
      <ElDropdownMenu>
        <!-- skill_event -->
        <template v-if="sub.kind === 'skill_event'">
          <ElDropdownItem command="edit">
            <Icon icon="mdi:pencil-outline" /> Edit event
          </ElDropdownItem>
          <ElDropdownItem command="simulate">
            <Icon icon="mdi:flask-outline" /> Simulate
          </ElDropdownItem>
          <ElDropdownItem command="toggle-pause">
            <Icon :icon="isPaused ? 'mdi:play' : 'mdi:pause'" />
            {{ isPaused ? 'Resume' : 'Pause' }}
          </ElDropdownItem>
          <ElDropdownItem v-if="!listenerRunning" command="start-listener">
            <Icon icon="mdi:play" /> Start listener
          </ElDropdownItem>
          <ElDropdownItem command="open-conversation">
            <Icon icon="mdi:forum-outline" /> Open conversation
          </ElDropdownItem>
          <ElDropdownItem command="delete" divided>
            <span class="rule-menu-danger"><Icon icon="mdi:delete-outline" /> Delete event</span>
          </ElDropdownItem>
        </template>

        <!-- file_watcher -->
        <template v-else-if="sub.kind === 'file_watcher'">
          <ElDropdownItem command="edit">
            <Icon icon="mdi:pencil-outline" /> Edit event
          </ElDropdownItem>
          <ElDropdownItem command="toggle-pause">
            <Icon :icon="isPaused ? 'mdi:play' : 'mdi:pause'" />
            {{ isPaused ? 'Resume' : 'Pause' }}
          </ElDropdownItem>
          <ElDropdownItem command="open-conversation">
            <Icon icon="mdi:forum-outline" /> Open conversation
          </ElDropdownItem>
          <ElDropdownItem command="delete" divided>
            <span class="rule-menu-danger"><Icon icon="mdi:delete-outline" /> Delete event</span>
          </ElDropdownItem>
        </template>

        <!-- schedule -->
        <template v-else-if="isSchedule">
          <ElDropdownItem v-if="scheduleActionable" command="edit">
            <Icon icon="mdi:pencil-outline" /> Edit event
          </ElDropdownItem>
          <ElDropdownItem v-if="scheduleActionable" command="toggle-pause">
            <Icon :icon="isPaused ? 'mdi:play' : 'mdi:pause'" />
            {{ isPaused ? 'Resume' : 'Pause' }}
          </ElDropdownItem>
          <ElDropdownItem command="open-conversation">
            <Icon icon="mdi:forum-outline" /> Open conversation
          </ElDropdownItem>
          <ElDropdownItem command="delete" divided>
            <span class="rule-menu-danger"><Icon icon="mdi:delete-outline" /> Delete event</span>
          </ElDropdownItem>
        </template>
      </ElDropdownMenu>
    </template>
  </ElDropdown>
</template>

<style scoped>
.rule-kebab {
  border: 1px solid var(--border-color);
  background: var(--bg-color);
  color: var(--text-secondary);
  border-radius: 5px;
  padding: 2px 6px;
  font-size: 0.75rem;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
}
.rule-kebab:hover {
  color: var(--primary-color);
  border-color: var(--primary-color);
}
</style>

<!-- Menu items are teleported to <body>, so the danger color must be un-scoped. -->
<style>
.rule-menu-popper .rule-menu-danger {
  color: var(--danger-color, #f56c6c);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
</style>
