<script setup lang="ts">
import { computed } from 'vue';
import {
  ElButton,
  ElEmpty,
  ElMessage,
  ElMessageBox,
  ElTable,
  ElTableColumn,
  ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useEventRunsStore } from '../../stores/eventRuns';
import EventRunStatusTag from './EventRunStatusTag.vue';
import { formatTimestamp, formatTokensCompact, formatUsd } from '../../utils/usageFormat';
import { runDuration } from '../../utils/duration';
import type { EventRun, EventRunSourceKind } from '../../services/eventRunsApi';

const props = defineProps<{
  sourceKind: EventRunSourceKind;
  subscriptionId: string;
}>();

const store = useEventRunsStore();

const runs = computed(() => store.runsForSubscription(props.sourceKind, props.subscriptionId));
const exhausted = computed(() => store.isExhausted(props.sourceKind, props.subscriptionId));
const loadingMore = computed(() => store.isLoadingMore(props.sourceKind, props.subscriptionId));

function open(run: EventRun) {
  store.openRun(run.id);
}

async function confirmDelete(run: EventRun) {
  try {
    await ElMessageBox.confirm(
      'Delete this run and its conversation? Usage totals are kept in Usage & Cost.',
      'Delete run',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch {
    return;
  }
  try {
    await store.removeRun(run.id);
    ElMessage.success('Run deleted');
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

function loadOlder() {
  store.loadOlder(props.sourceKind, props.subscriptionId);
}
</script>

<template>
  <div class="run-history">
    <ElEmpty v-if="runs.length === 0" description="No runs yet." :image-size="60" />
    <template v-else>
      <ElTable :data="runs" size="small" row-key="id" @row-click="open" class="run-table">
        <ElTableColumn label="Fired" width="170">
          <template #default="{ row }">{{ formatTimestamp(row.created_at) }}</template>
        </ElTableColumn>
        <ElTableColumn label="Status" width="140">
          <template #default="{ row }">
            <EventRunStatusTag :status="row.status" />
            <ElTooltip
              v-if="row.status === 'pending' && row.pending_question"
              :content="row.pending_question"
              placement="top"
            >
              <span class="pending-q">{{ row.pending_question }}</span>
            </ElTooltip>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Trigger" min-width="180">
          <template #default="{ row }">
            <ElTooltip
              v-if="row.action"
              :content="row.action"
              placement="top"
              :show-after="300"
            >
              <span class="action-cell">{{ row.action || row.label }}</span>
            </ElTooltip>
            <span v-else class="action-cell">{{ row.label }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Tokens" width="90" align="right">
          <template #default="{ row }">{{ formatTokensCompact(row.usage.total_tokens) }}</template>
        </ElTableColumn>
        <ElTableColumn label="Cost" width="90" align="right">
          <template #default="{ row }">{{ formatUsd(row.usage.total_usd) }}</template>
        </ElTableColumn>
        <ElTableColumn label="Duration" width="90" align="right">
          <template #default="{ row }">{{ runDuration(row as EventRun) }}</template>
        </ElTableColumn>
        <ElTableColumn label="" width="120" align="right">
          <template #default="{ row }">
            <ElButton size="small" text @click.stop="open(row as EventRun)">
              <Icon icon="mdi:open-in-app" /> Open
            </ElButton>
            <ElButton size="small" text type="danger" @click.stop="confirmDelete(row as EventRun)">
              <Icon icon="mdi:delete-outline" />
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
      <div v-if="!exhausted" class="load-older">
        <ElButton size="small" text :loading="loadingMore" @click="loadOlder">
          Load older runs
        </ElButton>
      </div>
    </template>
  </div>
</template>

<style scoped>
.run-history {
  padding: 8px 16px 12px 48px;
}
.run-table {
  cursor: pointer;
}
.action-cell {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.pending-q {
  display: block;
  font-size: 11px;
  color: var(--el-color-warning);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 130px;
}
.load-older {
  text-align: center;
  padding-top: 6px;
}
</style>
