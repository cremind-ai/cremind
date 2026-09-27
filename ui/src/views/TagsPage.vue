<script setup lang="ts">
/**
 * Tags — the Cremind Tag e-paper screens this profile owns (GET /api/tags):
 * live status per tag, what is on its screen versus what is on its way, the
 * actions a profile may take on its own tags, and each tag's delivery history
 * with a detail drawer (`?delivery=<id>` opens it).
 *
 * Refreshed by a plain 15 s poll while the page is visible — deliberately not
 * a new SSE stream (each origin gets ~6 HTTP/1.1 connections).
 *
 * CLI counterpart: `cremind tags list/show/history/display/clear/…`.
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElButton, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import { goBackToChat } from '../utils/backToChat';
import { useTagsStore } from '../stores/tags';
import { useSettingsStore } from '../stores/settings';
import { useNow } from '../composables/useNow';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import type { TagDelivery, TagDevice } from '../services/tagsApi';
import TagDeviceCard from '../components/tags/TagDeviceCard.vue';
import TagDisplayDialog from '../components/tags/TagDisplayDialog.vue';
import TagDeliveryDrawer from '../components/tags/TagDeliveryDrawer.vue';
import { deviceTitle } from '../utils/tagsFormat';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const route = useRoute();
const store = useTagsStore();
const settingsStore = useSettingsStore();
const { now } = useNow();

const loading = ref(false);
const loadError = ref('');
const icons = ref<string[]>([]);
const displayOpen = ref(false);
const displayDevice = ref<TagDevice | null>(null);
const drawerId = ref<number | null>(null);
const drawerDeviceId = ref<string | null>(null);
const cardRefs: Record<string, InstanceType<typeof TagDeviceCard> | null> = {};

store.reset(props.profile);

const overview = computed(() => store.overview);
const devices = computed(() => overview.value?.devices ?? []);
const counts = computed(() => overview.value?.counts);
const isAdmin = computed(() => props.profile === 'admin' || !!store.settings?.is_admin);

const drawerTagName = computed(() => {
  const dev = devices.value.find((x) => x.id === drawerDeviceId.value);
  return dev ? deviceTitle(dev) : '';
});

async function refresh(initial = false) {
  if (!settingsStore.authToken) return;
  if (initial) loading.value = true;
  try {
    await store.loadOverview();
    loadError.value = '';
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Failed to load your tags';
  } finally {
    loading.value = false;
  }
}

const poll = useVisiblePoll(() => refresh(), 15_000);

async function loadIcons() {
  try {
    const s = await store.loadSettings();
    icons.value = s.icons;
  } catch {
    icons.value = [];
  }
}

function openDisplay(device: TagDevice) {
  displayDevice.value = device;
  displayOpen.value = true;
}

function onSent() {
  poll.trigger();
}

function openDelivery(d: TagDelivery) {
  drawerId.value = d.id;
  drawerDeviceId.value = d.device_id;
  if (String(route.query.delivery || '') !== String(d.id)) {
    router.replace({ query: { ...route.query, delivery: String(d.id) } });
  }
}

function closeDelivery() {
  drawerId.value = null;
  drawerDeviceId.value = null;
  if (route.query.delivery) {
    const { delivery: _drop, ...rest } = route.query;
    router.replace({ query: rest });
  }
}

function onDeliveryChanged(d: TagDelivery) {
  cardRefs[d.device_id]?.upsertDelivery(d);
  poll.trigger();
}

/** `?delivery=<id>` (a link from a notification or the CLI docs) opens the drawer. */
function applyDeepLink() {
  const raw = String(route.query.delivery || '');
  const id = raw ? Number(raw) : NaN;
  if (Number.isFinite(id) && id > 0) {
    if (drawerId.value !== id) {
      drawerId.value = id;
      drawerDeviceId.value = null;
    }
  } else if (drawerId.value != null) {
    drawerId.value = null;
  }
}

function setCardRef(id: string, el: unknown) {
  cardRefs[id] = (el as InstanceType<typeof TagDeviceCard> | null) ?? null;
}

function goSettings() {
  router.push(`/${props.profile}/settings/tags`);
}

function goHardware() {
  router.push(`/${props.profile}/settings/tags/hardware`);
}

onMounted(async () => {
  applyDeepLink();
  await Promise.all([refresh(true), loadIcons()]);
});

watch(() => route.query.delivery, applyDeepLink);
// A hard reload may render before the profile's token is active.
watch(() => settingsStore.authToken, (t, prev) => {
  if (t && !prev) { void refresh(true); void loadIcons(); }
});

function manualRefresh() {
  refresh().then(() => { if (!loadError.value) ElMessage.success('Up to date'); });
}
</script>

<template>
  <div class="tags-page">
    <div class="tags-container">
      <button class="back-btn" @click="goBackToChat(router, props.profile)">
        <Icon icon="mdi:arrow-left" />
        Back to Chat
      </button>
      <div class="page-header">
        <div>
          <h1 class="page-title">Tags</h1>
          <p class="page-subtitle">
            The Cremind Tag e-paper screens this profile owns, and what Cremind sends them.
          </p>
        </div>
        <div class="header-actions">
          <ElButton aria-label="Refresh now" @click="manualRefresh">
            <Icon icon="mdi:refresh" />
          </ElButton>
          <ElButton type="primary" @click="goSettings">
            <Icon icon="mdi:cog-outline" class="btn-icon" /> Settings
          </ElButton>
        </div>
      </div>

      <div v-if="loading && !overview" class="loading">Loading…</div>
      <div v-else-if="loadError && !overview" class="callout callout-danger" role="alert">
        <Icon icon="mdi:alert-circle-outline" class="callout-icon danger" />
        <span>{{ loadError }}</span>
      </div>
      <template v-else-if="overview">
        <div v-if="!overview.enabled" class="callout callout-info" role="status">
          <Icon icon="mdi:information-outline" class="callout-icon info" />
          <span class="callout-text">
            Tags are off for this profile, so Cremind is not turning your activity into cards.
            A note you display on a tag yourself still goes out.
          </span>
          <ElButton size="small" @click="goSettings">Turn on in Settings</ElButton>
        </div>

        <div class="stats">
          <div class="stat">
            <span class="stat-value">{{ counts?.devices ?? 0 }}</span>
            <span class="stat-label">Tags</span>
          </div>
          <div class="stat">
            <span class="stat-value" :class="{ accent: (counts?.active_deliveries ?? 0) > 0 }">{{ counts?.active_deliveries ?? 0 }}</span>
            <span class="stat-label">Updates on their way</span>
          </div>
          <div class="stat">
            <span class="stat-value" :class="{ warn: (counts?.needs_input ?? 0) > 0 }">{{ counts?.needs_input ?? 0 }}</span>
            <span class="stat-label">Waiting for your input</span>
          </div>
          <div class="stat">
            <span class="stat-value" :class="{ danger: (counts?.failed_24h ?? 0) > 0 }">{{ counts?.failed_24h ?? 0 }}</span>
            <span class="stat-label">Failed (24 h)</span>
          </div>
        </div>

        <div v-if="devices.length === 0" class="empty">
          <Icon icon="mdi:tablet-dashboard" class="empty-icon" />
          <h2 class="empty-title">No tags yet</h2>
          <p class="empty-text">A tag shows up here once it is yours. Getting there takes three steps:</p>
          <ol class="empty-steps">
            <li>
              <strong>Connect a companion.</strong> The companion is the small app on the computer the
              Cremind Tag gateway is plugged into. An admin registers it under
              Settings → Tags → Hardware and gives it its hardware credential with
              <code>cremind-tag connect</code>.
            </li>
            <li>
              <strong>An admin claims a tag for you.</strong> Tags are shared hardware: only the admin
              profile decides which profile owns which tag.
            </li>
            <li>
              <strong>Let the companion fetch your cards.</strong> Create a content credential for your
              profile in Settings → Tags and give it to the companion the same way.
            </li>
          </ol>
          <div class="empty-actions">
            <ElButton type="primary" @click="goSettings">Open Tags settings</ElButton>
            <ElButton v-if="isAdmin" @click="goHardware">Open Hardware</ElButton>
          </div>
        </div>

        <TagDeviceCard
          v-for="device in devices"
          :key="device.id"
          :ref="(el) => setCardRef(device.id, el)"
          :device="device"
          :pending="device.pending_count ?? 0"
          :now="now"
          :is-admin="isAdmin"
          @display="openDisplay"
          @open-delivery="openDelivery"
          @changed="poll.trigger()"
        />
        <p v-if="loadError" class="stale-note">Could not refresh: {{ loadError }}</p>
      </template>
    </div>

    <TagDisplayDialog v-model="displayOpen" :device="displayDevice" :icons="icons" @sent="onSent" />
    <TagDeliveryDrawer
      :delivery-id="drawerId"
      :tag-name="drawerTagName"
      @close="closeDelivery"
      @changed="onDeliveryChanged"
    />
  </div>
</template>

<style scoped>
.tags-page {
  width: 100%; height: 100%; overflow-y: auto; box-sizing: border-box;
  padding: 24px; background: var(--bg-color); color: var(--text-primary);
}
.tags-container { max-width: 980px; margin: 0 auto; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.page-header {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
  margin-bottom: 18px; flex-wrap: wrap;
}
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px; color: var(--text-primary); }
.page-subtitle { font-size: 0.875rem; color: var(--text-secondary); margin: 0; }
.header-actions { display: flex; gap: 8px; }
.header-actions .el-button + .el-button { margin-left: 0; }
.btn-icon { margin-right: 6px; }
.loading { padding: 60px 0; text-align: center; color: var(--text-secondary); }

.callout {
  display: flex; align-items: center; gap: 10px; margin-bottom: 16px;
  padding: 10px 12px; border-radius: 8px; font-size: 0.875rem; line-height: 1.45;
  color: var(--text-primary); flex-wrap: wrap;
}
.callout-text { flex: 1; min-width: 220px; }
.callout-info {
  border: 1px solid color-mix(in srgb, var(--primary-color) 45%, transparent);
  background: color-mix(in srgb, var(--primary-color) 10%, var(--surface-color));
}
.callout-danger {
  border: 1px solid color-mix(in srgb, var(--el-color-danger) 55%, transparent);
  background: color-mix(in srgb, var(--el-color-danger) 12%, var(--surface-color));
}
.callout-icon { font-size: 1.15rem; flex-shrink: 0; }
.callout-icon.info { color: var(--primary-color); }
.callout-icon.danger { color: var(--el-color-danger); }

.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px; margin-bottom: 18px;
}
.stat {
  display: flex; flex-direction: column; gap: 2px; padding: 12px 14px;
  border: 1px solid var(--border-color); border-radius: 10px; background: var(--surface-color);
}
.stat-value { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.stat-value.accent { color: var(--primary-color); }
.stat-value.warn { color: var(--el-color-warning); }
.stat-value.danger { color: var(--el-color-danger); }
.stat-label { font-size: 0.8rem; color: var(--text-secondary); }

.empty {
  border: 1px dashed var(--border-color); border-radius: 12px; background: var(--surface-color);
  padding: 28px 28px 24px; text-align: left; color: var(--text-primary);
}
.empty-icon { font-size: 36px; color: var(--primary-color); }
.empty-title { font-size: 1.1rem; margin: 8px 0 4px; }
.empty-text { margin: 0 0 8px; color: var(--text-secondary); font-size: 0.9rem; }
.empty-steps { margin: 0 0 16px; padding-left: 20px; color: var(--text-secondary); font-size: 0.875rem; line-height: 1.6; }
.empty-steps li + li { margin-top: 6px; }
.empty-steps strong { color: var(--text-primary); }
.empty-steps code {
  background: var(--hover-bg); color: var(--text-primary);
  padding: 1px 6px; border-radius: 4px; font-size: 0.85em;
}
.empty-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.empty-actions .el-button + .el-button { margin-left: 0; }
.stale-note { font-size: 0.8rem; color: var(--el-color-danger); }
</style>
