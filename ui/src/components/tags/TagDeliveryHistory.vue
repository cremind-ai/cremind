<script setup lang="ts">
/**
 * One tag's delivery history, newest first, in pages of 20 ("Load older" asks
 * for the page before the oldest row shown). When `refreshKey` changes (the
 * page's poll saw the tag move) the newest page is fetched again and merged
 * in, so rows already paged in stay. A row opens the detail drawer; any
 * delivery that has not finished can be cancelled — also after the companion
 * fetched it (Cremind then sends it a `resolved` job to drop the card).
 */
import { onMounted, ref, watch } from 'vue';
import {
  ElButton, ElEmpty, ElMessage, ElMessageBox, ElTable, ElTableColumn, ElTag, ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import { TagsApiError, type TagDelivery } from '../../services/tagsApi';
import { formatTimestamp } from '../../utils/usageFormat';
import { formatRelativeTime } from '../../utils/relativeTime';
import {
  cancelPrompt, cancelledMessage, cardIconGlyph, kindLabel, stageLabel, stagePillType,
} from '../../utils/tagsFormat';

const PAGE = 20;

const props = defineProps<{ deviceId: string; refreshKey: string | number; now: number }>();
const emit = defineEmits<{ (e: 'open', delivery: TagDelivery): void; (e: 'changed'): void }>();

const store = useTagsStore();
const rows = ref<TagDelivery[]>([]);
const nextBefore = ref<number | null>(null);
const loading = ref(false);
const loadingMore = ref(false);
const cancelling = ref<number | null>(null);

function merge(fresh: TagDelivery[]) {
  const byId = new Map(rows.value.map((r) => [r.id, r]));
  for (const d of fresh) byId.set(d.id, d);
  rows.value = [...byId.values()].sort((a, b) => b.id - a.id);
}

async function loadHead(initial = false) {
  if (initial) loading.value = true;
  try {
    const page = await store.deliveries({ device: props.deviceId, limit: PAGE });
    if (initial) {
      rows.value = page.deliveries;
      nextBefore.value = page.next_before;
    } else {
      // A refresh only adds or updates the newest rows; the "older" cursor
      // still points below the oldest row shown.
      merge(page.deliveries);
    }
  } catch (e) {
    if (initial) ElMessage.error(e instanceof Error ? e.message : 'Failed to load the history');
  } finally {
    loading.value = false;
  }
}

async function loadOlder() {
  if (nextBefore.value == null) return;
  loadingMore.value = true;
  try {
    const page = await store.deliveries({ device: props.deviceId, limit: PAGE, before: nextBefore.value });
    merge(page.deliveries);
    nextBefore.value = page.next_before;
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load older deliveries');
  } finally {
    loadingMore.value = false;
  }
}

async function cancel(row: TagDelivery) {
  try {
    await ElMessageBox.confirm(cancelPrompt(row), 'Cancel delivery',
      { type: 'warning', confirmButtonText: 'Cancel delivery', cancelButtonText: 'Keep it' });
  } catch { return; }
  cancelling.value = row.id;
  try {
    const res = await store.cancelDelivery(row.id);
    merge([res.delivery]);
    ElMessage.success(cancelledMessage(res));
    emit('changed');
  } catch (e) {
    if (e instanceof TagsApiError && e.code === 'already_terminal') {
      if (e.body?.delivery) merge([e.body.delivery as TagDelivery]);
      ElMessage.info(e.message);
    } else {
      ElMessage.error(e instanceof Error ? e.message : 'Failed to cancel');
    }
  } finally {
    cancelling.value = null;
  }
}

/** Let the parent update a row it changed elsewhere (the drawer's cancel). */
function upsert(d: TagDelivery) {
  if (d.device_id === props.deviceId) merge([d]);
}
defineExpose({ upsert, reload: () => loadHead(false) });

onMounted(() => loadHead(true));
watch(() => props.refreshKey, () => loadHead(false));
</script>

<template>
  <div class="delivery-history">
    <div v-if="loading" class="muted pad">Loading…</div>
    <ElEmpty v-else-if="rows.length === 0" description="Nothing has been sent to this tag yet." :image-size="56" />
    <template v-else>
      <ElTable :data="rows" size="small" row-key="id" class="history-table" @row-click="(r: TagDelivery) => emit('open', r)">
        <ElTableColumn label="Sent" width="150">
          <template #default="{ row }">
            <div :title="formatTimestamp(row.created_at)">{{ formatRelativeTime(row.created_at, now) }}</div>
            <div class="muted small">#{{ row.id }}</div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Card" min-width="220">
          <template #default="{ row }">
            <div class="card-cell">
              <Icon :icon="cardIconGlyph(row.card?.icon)" class="card-icon" />
              <div class="card-text">
                <div class="card-title">{{ row.card?.title || kindLabel(row.kind) }}</div>
                <div class="muted small">{{ kindLabel(row.kind) }}</div>
              </div>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Stage" width="190">
          <template #default="{ row }">
            <ElTag :type="stagePillType(row.stage)" size="small" effect="plain">{{ stageLabel(row.stage) }}</ElTag>
            <ElTooltip v-if="row.detail" :content="row.detail" placement="top" :show-after="200">
              <div class="detail-line">{{ row.detail }}</div>
            </ElTooltip>
          </template>
        </ElTableColumn>
        <ElTableColumn label="Rev" width="64" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.revision ?? '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="" width="150" align="right">
          <template #default="{ row }">
            <ElButton size="small" text @click.stop="emit('open', row as TagDelivery)">
              <Icon icon="mdi:text-box-search-outline" class="btn-icon" /> Details
            </ElButton>
            <ElButton
              v-if="!row.terminal"
              size="small" text type="danger"
              :loading="cancelling === row.id"
              :aria-label="`Cancel delivery ${row.id}`"
              @click.stop="cancel(row as TagDelivery)"
            >
              <Icon icon="mdi:close-circle-outline" />
            </ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
      <div v-if="nextBefore != null" class="load-older">
        <ElButton size="small" text :loading="loadingMore" @click="loadOlder">Load older</ElButton>
      </div>
    </template>
  </div>
</template>

<style scoped>
.delivery-history { padding-top: 4px; }
.history-table { cursor: pointer; }
.card-cell { display: flex; align-items: flex-start; gap: 8px; min-width: 0; }
.card-icon { flex-shrink: 0; font-size: 18px; color: var(--text-secondary); margin-top: 2px; }
.card-text { min-width: 0; }
.card-title {
  color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.detail-line {
  font-size: 0.75rem; color: var(--text-tertiary); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 170px;
}
.muted { color: var(--text-tertiary); }
.small { font-size: 0.75rem; }
.pad { padding: 16px 0; text-align: center; }
.num { font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.btn-icon { margin-right: 4px; }
.load-older { text-align: center; padding-top: 6px; }
</style>
