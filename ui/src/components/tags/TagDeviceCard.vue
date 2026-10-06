<script setup lang="ts">
/**
 * One tag the profile owns: status, battery, last contact, what is on the
 * screen versus what is waiting for it, the two stored previews side by side,
 * the actions a profile may take on its own tag (display a note, clear,
 * refresh, identify, rename), and its delivery history on demand.
 */
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  ElButton, ElCard, ElMessage, ElMessageBox, ElTag, ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import type { TagDelivery, TagDevice } from '../../services/tagsApi';
import TagPreviewImage from './TagPreviewImage.vue';
import TagDeliveryHistory from './TagDeliveryHistory.vue';
import { formatRelativeTime } from '../../utils/relativeTime';
import { formatTimestamp } from '../../utils/usageFormat';
import {
  BATTERY_LOW_MV, batteryIcon, deviceStatusPill, deviceTitle, formatBattery, formatRssi, logicalSize, panelLabel,
} from '../../utils/tagsFormat';

const props = defineProps<{ device: TagDevice; pending: number; now: number; isAdmin?: boolean }>();
const emit = defineEmits<{
  (e: 'display', device: TagDevice): void;
  (e: 'open-delivery', delivery: TagDelivery): void;
  (e: 'changed'): void;
}>();

const store = useTagsStore();
const router = useRouter();
const route = useRoute();
const historyOpen = ref(false);
const busy = ref<string | null>(null);
const history = ref<InstanceType<typeof TagDeliveryHistory> | null>(null);

const d = computed(() => props.device);
const status = computed(() => deviceStatusPill(d.value.status));
const title = computed(() => deviceTitle(d.value));
/** The clear after a change of owner failed or expired 3 times. */
const clearFailed = computed(() => d.value.status === 'clear_failed');
/** Its bridge refused the assignment (full) and it was detached from it. */
const assignFailed = computed(() => d.value.status === 'assign_failed');
function openHardware() {
  router.push({ path: `/${route.params.profile}/settings/tags/hardware`, query: { tag: d.value.id } });
}
const lowBattery = computed(() => d.value.battery_mv != null && d.value.battery_mv < BATTERY_LOW_MV);
/** Each preview is at least as wide as the screen at one CSS pixel per tag
 *  pixel (+ its frame), so two that do not fit side by side wrap instead of
 *  shrinking; never wider than the card, whose preview then scrolls. */
const previewsStyle = computed(() => {
  const s = logicalSize(d.value);
  return s ? { '--preview-min': `min(${s.width + 2}px, 100%)` } : {};
});
const behind = computed(() => d.value.desired_revision > d.value.displayed_revision);
// What the history should re-read on: anything the poll sees move on this tag.
const historyKey = computed(() => `${d.value.updated_at}:${props.pending}:${d.value.desired_revision}:${d.value.displayed_revision}`);

const pendingText = computed(() => {
  const n = props.pending;
  if (!n) return 'Nothing waiting';
  return n === 1 ? '1 update waiting for this tag' : `${n} updates waiting for this tag`;
});

const screenText = computed(() => {
  const shown = d.value.displayed_revision;
  const composed = d.value.desired_revision;
  if (!shown && !composed) return 'Nothing shown yet';
  if (!shown) return `Nothing shown yet · revision ${composed} on its way`;
  if (behind.value) return `Showing revision ${shown} · revision ${composed} on its way`;
  return `Up to date · revision ${shown}`;
});

async function run(kind: 'refresh' | 'identify') {
  busy.value = kind;
  try {
    if (kind === 'refresh') {
      await store.refresh(d.value.id);
      ElMessage.success('Refresh requested — the tag redraws at its next wake-up');
    } else {
      await store.identify(d.value.id);
      ElMessage.success('Identify requested — the tag flashes at its next wake-up');
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : `Failed to ${kind}`);
  } finally {
    busy.value = null;
  }
}

async function clearScreen() {
  try {
    await ElMessageBox.confirm(
      `Blank the screen of ${title.value}? Every card waiting for it is cancelled; new cards appear again as they arrive.`,
      'Clear tag',
      { type: 'warning', confirmButtonText: 'Clear screen', cancelButtonText: 'Cancel', confirmButtonClass: 'el-button--danger' },
    );
  } catch { return; }
  busy.value = 'clear';
  try {
    await store.clear(d.value.id);
    ElMessage.success('Clear queued');
    emit('changed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to clear the tag');
  } finally {
    busy.value = null;
  }
}

async function rename() {
  let value: string;
  try {
    const res = await ElMessageBox.prompt('A name for this tag, as you will see it in Cremind.', 'Rename tag', {
      inputValue: d.value.name,
      inputPlaceholder: d.value.hw_id,
      confirmButtonText: 'Rename',
      cancelButtonText: 'Cancel',
      inputValidator: (v: string) => {
        const n = (v ?? '').trim().length;
        return (n >= 1 && n <= 128) || 'Enter 1 to 128 characters';
      },
    });
    value = (res as { value: string }).value ?? '';
  } catch { return; }
  busy.value = 'rename';
  try {
    await store.renameDevice(d.value.id, value.trim());
    ElMessage.success('Tag renamed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to rename');
  } finally {
    busy.value = null;
  }
}

defineExpose({ upsertDelivery: (x: TagDelivery) => history.value?.upsert(x) });
</script>

<template>
  <ElCard shadow="never" class="tag-card" :data-tag-id="d.id">
    <div class="tag-head">
      <div class="tag-icon"><Icon icon="mdi:tablet-dashboard" /></div>
      <div class="tag-meta">
        <div class="tag-name">
          <span class="tag-title">{{ title }}</span>
          <ElTag :type="status.type" size="small" effect="plain">{{ status.label }}</ElTag>
          <ElTag v-if="d.companion_online === false" type="warning" size="small" effect="plain">companion offline</ElTag>
        </div>
        <div class="tag-sub">
          <span class="mono">{{ d.hw_id }}</span>
          <span v-if="panelLabel(d)">· {{ panelLabel(d) }}</span>
          <span v-if="d.companion_name">· via {{ d.companion_name }}</span>
          <span v-if="d.fw">· fw {{ d.fw }}</span>
        </div>
      </div>
      <div class="tag-actions">
        <ElButton type="primary" size="small" :disabled="d.clear_required || clearFailed" @click="emit('display', d)">
          <Icon icon="mdi:note-edit-outline" class="btn-icon" /> Display note
        </ElButton>
        <ElButton size="small" :loading="busy === 'clear'" @click="clearScreen">
          <Icon icon="mdi:eraser" class="btn-icon" /> Clear
        </ElButton>
        <ElTooltip content="Redraw the screen at the next wake-up" placement="top" :show-after="300">
          <ElButton size="small" :loading="busy === 'refresh'" aria-label="Refresh" @click="run('refresh')">
            <Icon icon="mdi:refresh" />
          </ElButton>
        </ElTooltip>
        <ElTooltip content="Flash the tag so you can find it" placement="top" :show-after="300">
          <ElButton size="small" :loading="busy === 'identify'" aria-label="Identify" @click="run('identify')">
            <Icon icon="mdi:target" />
          </ElButton>
        </ElTooltip>
        <ElTooltip content="Rename" placement="top" :show-after="300">
          <ElButton size="small" :loading="busy === 'rename'" aria-label="Rename" @click="rename">
            <Icon icon="mdi:pencil-outline" />
          </ElButton>
        </ElTooltip>
      </div>
    </div>

    <div v-if="assignFailed" class="callout callout-danger" role="alert">
      <Icon icon="mdi:alert-octagon-outline" class="callout-icon danger" />
      <span class="callout-text">
        The tag's bridge could not take it (its table is full), so the tag is not connected to any
        bridge. Cards wait until an admin assigns it to another bridge or releases it.
      </span>
      <ElButton v-if="isAdmin" size="small" @click="openHardware">Open Hardware</ElButton>
    </div>
    <div v-else-if="clearFailed" class="callout callout-danger" role="alert">
      <Icon icon="mdi:alert-octagon-outline" class="callout-icon danger" />
      <span class="callout-text">
        The tag could not clear its screen after a change of owner — three tries failed or ran
        out of time. Nothing can be shown on it until an admin claims or releases it again.
      </span>
      <ElButton v-if="isAdmin" size="small" @click="openHardware">Open Hardware</ElButton>
    </div>
    <div v-else-if="d.clear_required" class="callout callout-warning" role="status">
      <Icon icon="mdi:progress-clock" class="callout-icon" />
      <span>
        Clearing the screen after a change of owner. Notes can be sent once the tag
        confirms it is blank.
      </span>
    </div>

    <div class="facts">
      <div class="fact">
        <span class="fact-label">Battery</span>
        <span class="fact-value" :class="{ danger: lowBattery }">
          <Icon :icon="batteryIcon(d.battery_mv)" class="fact-icon" />
          {{ formatBattery(d.battery_mv) }}<template v-if="lowBattery"> · low</template>
        </span>
      </div>
      <div class="fact">
        <span class="fact-label">Signal</span>
        <span class="fact-value">{{ formatRssi(d.rssi) }}</span>
      </div>
      <div class="fact">
        <span class="fact-label">Last contact</span>
        <span class="fact-value" :title="d.last_contact_at ? formatTimestamp(d.last_contact_at) : ''">
          {{ d.last_contact_at ? formatRelativeTime(d.last_contact_at, now) : 'never' }}
        </span>
      </div>
      <div class="fact">
        <span class="fact-label">Screen</span>
        <span class="fact-value">{{ screenText }}</span>
      </div>
      <div class="fact">
        <span class="fact-label">Pending</span>
        <span class="fact-value" :class="{ accent: pending > 0 }">{{ pendingText }}</span>
      </div>
    </div>

    <div class="previews" :style="previewsStyle">
      <TagPreviewImage
        :device-id="d.id" kind="displayed" label="On the tag now" :epoch="d.epoch" :name="title"
        :revision="d.previews?.displayed ?? null" :width="d.width" :height="d.height" :rotation="d.rotation"
      />
      <TagPreviewImage
        :device-id="d.id" kind="desired" label="Next screen" :epoch="d.epoch" :name="title"
        :revision="d.previews?.desired ?? null" :width="d.width" :height="d.height" :rotation="d.rotation"
      />
    </div>

    <button
      type="button"
      class="history-toggle"
      :aria-expanded="historyOpen"
      @click="historyOpen = !historyOpen"
    >
      <Icon :icon="historyOpen ? 'mdi:chevron-down' : 'mdi:chevron-right'" />
      Delivery history
    </button>
    <TagDeliveryHistory
      v-if="historyOpen"
      ref="history"
      :device-id="d.id"
      :refresh-key="historyKey"
      :now="now"
      @open="(x: TagDelivery) => emit('open-delivery', x)"
      @changed="emit('changed')"
    />
  </ElCard>
</template>

<style scoped>
.tag-card { margin-bottom: 14px; color: var(--text-primary); }
.tag-head { display: flex; align-items: flex-start; gap: 14px; flex-wrap: wrap; }
.tag-icon {
  width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
  background: var(--hover-bg); border-radius: 10px; font-size: 22px;
  color: var(--primary-color); flex-shrink: 0;
}
.tag-meta { flex: 1; min-width: 200px; }
.tag-name { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-weight: 600; }
.tag-title { font-size: 1rem; }
.tag-sub { font-size: 0.82rem; color: var(--text-secondary); margin-top: 2px; display: flex; flex-wrap: wrap; gap: 4px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.8rem; }
.tag-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.tag-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 4px; }

.callout {
  display: flex; align-items: flex-start; gap: 8px; margin-top: 12px;
  padding: 10px 12px; border-radius: 8px; font-size: 0.85rem; line-height: 1.45;
  color: var(--text-primary);
}
.callout-warning {
  border: 1px solid color-mix(in srgb, var(--el-color-warning) 55%, transparent);
  background: color-mix(in srgb, var(--el-color-warning) 12%, var(--surface-color));
}
.callout-danger {
  border: 1px solid color-mix(in srgb, var(--el-color-danger) 55%, transparent);
  background: color-mix(in srgb, var(--el-color-danger) 12%, var(--surface-color));
  align-items: center; flex-wrap: wrap;
}
.callout-text { flex: 1; min-width: 220px; }
.callout-icon { flex-shrink: 0; font-size: 1.1rem; margin-top: 1px; color: var(--el-color-warning); }
.callout-icon.danger { color: var(--el-color-danger); }

.facts {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 10px 16px; margin-top: 14px;
}
.fact { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.fact-label { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase; color: var(--text-tertiary); }
.fact-value { font-size: 0.88rem; color: var(--text-primary); display: flex; align-items: center; gap: 4px; }
.fact-value.danger { color: var(--el-color-danger); }
.fact-value.accent { color: var(--primary-color); font-weight: 600; }
.fact-icon { font-size: 1rem; }

.previews { display: flex; gap: 14px; margin-top: 14px; flex-wrap: wrap; }
.previews > * { min-width: var(--preview-min, 180px); }

.history-toggle {
  display: flex; align-items: center; gap: 4px; margin-top: 12px; padding: 4px 0;
  background: none; border: none; cursor: pointer;
  color: var(--text-secondary); font-size: 0.85rem; font-weight: 600;
}
.history-toggle:hover { color: var(--primary-color); }
</style>
