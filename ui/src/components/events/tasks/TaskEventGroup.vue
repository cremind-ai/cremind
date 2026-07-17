<script setup lang="ts">
import { computed, ref } from 'vue';
import TaskRunCard from './TaskRunCard.vue';
import type { BoardSubscription } from './boardTypes';
import type { EventRun } from '../../../services/eventRunsApi';

const props = defineProps<{
  sub: BoardSubscription | null;
  /** Terminal runs of this event in the current snapshot, newest first. */
  runs: EventRun[];
  now: number;
  /** When the whole board is filtered to one event, never collapse. */
  forceExpanded?: boolean;
}>();

const emit = defineEmits<{ (e: 'filter-event', key: string): void }>();

const expanded = ref(false);
const expandedEffective = computed(() => expanded.value || props.forceExpanded);
const visible = computed(() => (expandedEffective.value ? props.runs : props.runs.slice(0, 1)));
const extra = computed(() => Math.max(0, props.runs.length - 1));
</script>

<template>
  <div class="event-group">
    <TaskRunCard
      v-for="run in visible"
      :key="run.id"
      :run="run"
      :sub="sub"
      :now="now"
      @filter-event="(k) => emit('filter-event', k)"
    />
    <button
      v-if="!expandedEffective && extra > 0"
      type="button"
      class="eg-more"
      @click="expanded = true"
    >
      + {{ extra }} earlier {{ extra === 1 ? 'run' : 'runs' }}
    </button>
    <button
      v-else-if="expanded && !forceExpanded && extra > 0"
      type="button"
      class="eg-more"
      @click="expanded = false"
    >
      Collapse
    </button>
  </div>
</template>

<style scoped>
.event-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.eg-more {
  align-self: flex-start;
  margin-left: 6px;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 0.75rem;
  cursor: pointer;
  padding: 2px 4px;
}
.eg-more:hover {
  color: var(--primary-color);
  text-decoration: underline;
}
</style>
