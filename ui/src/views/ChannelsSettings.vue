<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElCard, ElMessage, ElMessageBox, ElSwitch, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';

import { useChannelsStore, MAIN_CHANNEL_TYPE } from '../stores/channels';
import type {
  ChannelCatalogEntry, ChannelRow, CreateChannelPayload,
} from '../services/channelApi';
import ChannelPairingDialog from '../components/channels/ChannelPairingDialog.vue';
import ChannelEditDialog from '../components/channels/ChannelEditDialog.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const channelsStore = useChannelsStore();

const loading = ref(false);
const showDialog = ref(false);
const editing = ref<ChannelRow | null>(null);
const dialogChannelType = ref<string>('');
const submitting = ref(false);

// Channel types not yet added.
const availableForAdd = computed<ChannelCatalogEntry[]>(() => {
  const taken = new Set(
    channelsStore.channels
      .filter((c) => c.channel_type !== MAIN_CHANNEL_TYPE)
      .map((c) => c.channel_type),
  );
  return Object.values(channelsStore.catalog).filter((e) => !taken.has(e.type));
});

const externalChannels = computed(() =>
  channelsStore.channels.filter((c) => c.channel_type !== MAIN_CHANNEL_TYPE),
);

// Pre-fill payload for the shared edit dialog when modifying an existing
// channel. ``null`` when creating a fresh entry.
const dialogInitial = computed<CreateChannelPayload | null>(() => {
  const ch = editing.value;
  if (!ch) return null;
  return {
    channel_type: ch.channel_type,
    mode: ch.mode,
    auth_mode: ch.auth_mode,
    response_mode: ch.response_mode,
    enabled: ch.enabled,
    config: { ...(ch.config || {}) },
  };
});

// Interactive-pairing dialog state. Shown after creating a channel whose
// chosen mode declares ``setup_kind`` (currently WhatsApp's ``qr`` and
// Telegram userbot's ``code``) and re-openable from a channel row's
// "Pair" button.
const pairingChannelId = ref<string>('');
const pairingChannelLabel = ref<string>('');
const pairingOpen = ref(false);

function modeNeedsInteractiveSetup(channelType: string, modeId: string): boolean {
  const entry = channelsStore.catalog[channelType];
  if (!entry) return false;
  const mode = entry.modes.find((m) => m.id === modeId);
  return Boolean(mode && mode.setup_kind);
}

function channelNeedsPairing(channel: ChannelRow): boolean {
  return modeNeedsInteractiveSetup(channel.channel_type, channel.mode);
}

function openPairingFor(channel: ChannelRow) {
  pairingChannelId.value = channel.id;
  pairingChannelLabel.value = displayNameFor(channel);
  pairingOpen.value = true;
}

function handlePaired() {
  // Refresh the channel rows so ``status`` updates reflect the now-paired
  // adapter. The dialog stays open with a "paired" success state until
  // the user clicks Done.
  channelsStore.loadChannels().catch(() => {});
}

async function loadAll() {
  loading.value = true;
  try {
    await Promise.all([channelsStore.loadCatalog(), channelsStore.loadChannels()]);
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load channels');
  } finally {
    loading.value = false;
  }
}

function openCreate(channelType: string) {
  if (!channelsStore.catalog[channelType]) return;
  editing.value = null;
  dialogChannelType.value = channelType;
  showDialog.value = true;
}

function openEdit(channel: ChannelRow) {
  editing.value = channel;
  dialogChannelType.value = channel.channel_type;
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
  editing.value = null;
}

async function handleDialogSubmit(payload: CreateChannelPayload) {
  submitting.value = true;
  try {
    if (editing.value) {
      await channelsStore.updateChannel(editing.value.id, {
        mode: payload.mode,
        auth_mode: payload.auth_mode,
        response_mode: payload.response_mode,
        config: payload.config,
      });
      ElMessage.success('Channel updated');
      closeDialog();
    } else {
      const created = await channelsStore.createChannel(payload);
      ElMessage.success('Channel registered');
      closeDialog();
      // Auto-open the pairing dialog when the chosen mode declares an
      // interactive setup_kind (WhatsApp QR scan, Telegram userbot code).
      if (modeNeedsInteractiveSetup(created.channel_type, created.mode)) {
        pairingChannelId.value = created.id;
        pairingChannelLabel.value =
          channelsStore.catalog[created.channel_type]?.display_name
          || created.channel_type;
        pairingOpen.value = true;
      }
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save channel');
  } finally {
    submitting.value = false;
  }
}

async function toggleEnabled(channel: ChannelRow, enabled: boolean) {
  try {
    await channelsStore.updateChannel(channel.id, { enabled });
    ElMessage.success(enabled ? 'Channel started' : 'Channel stopped');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to update channel');
  }
}

async function removeChannel(channel: ChannelRow) {
  try {
    await ElMessageBox.confirm(
      `Delete the ${displayNameFor(channel)} channel? All its conversations will also be removed.`,
      'Delete channel',
      { type: 'warning', confirmButtonText: 'Delete', cancelButtonText: 'Cancel' },
    );
  } catch { return; }
  try {
    await channelsStore.deleteChannel(channel.id);
    ElMessage.success('Channel deleted');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to delete channel');
  }
}

function displayNameFor(channel: ChannelRow): string {
  return channelsStore.catalog[channel.channel_type]?.display_name || channel.channel_type;
}

function iconFor(channel: ChannelRow): string {
  return channelsStore.catalog[channel.channel_type]?.icon || 'mdi:link-variant';
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

onMounted(loadAll);
</script>

<template>
  <div class="channels-page">
    <div class="channels-container">
      <div class="channels-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <div class="header-row">
          <div>
            <h1 class="channels-title">Channels</h1>
            <p class="channels-subtitle">
              Connect external messaging platforms · Profile <strong>{{ profile }}</strong>
            </p>
          </div>
        </div>
      </div>

      <div v-if="loading" class="loading">Loading…</div>
      <template v-else>
        <ElCard v-if="externalChannels.length === 0" shadow="never" class="empty-card">
          <p>No channels connected yet. Pick a platform below to get started.</p>
        </ElCard>

        <ElCard
          v-for="channel in externalChannels"
          :key="channel.id"
          shadow="never"
          class="channel-card"
        >
          <div class="channel-row">
            <div class="channel-icon">
              <Icon :icon="iconFor(channel)" />
            </div>
            <div class="channel-meta">
              <div class="channel-name">
                {{ displayNameFor(channel) }}
                <ElTag
                  v-if="channel.status === 'unlinked'"
                  type="danger" size="small" effect="plain"
                >unlinked</ElTag>
                <ElTag
                  v-else-if="channel.status === 'running'"
                  type="success" size="small" effect="plain"
                >running</ElTag>
                <ElTag
                  v-else-if="!channel.enabled"
                  type="info" size="small" effect="plain"
                >disabled</ElTag>
                <ElTag
                  v-else
                  type="warning" size="small" effect="plain"
                >stopped</ElTag>
              </div>
              <div class="channel-sub">
                <span>Mode: {{ channel.mode }}</span>
                <span>· Auth: {{ channel.auth_mode }}</span>
                <span>· Reply: {{ channel.response_mode }}</span>
              </div>
              <div v-if="channel.state?.last_error" class="channel-error">
                {{ channel.state.last_error }}
              </div>
            </div>
            <div class="channel-actions">
              <ElSwitch
                :model-value="channel.enabled"
                @update:model-value="(v) => toggleEnabled(channel, v as boolean)"
              />
              <ElButton
                v-if="channelNeedsPairing(channel)"
                size="small"
                @click="openPairingFor(channel)"
              >
                Pair
              </ElButton>
              <ElButton size="small" @click="openEdit(channel)">Edit</ElButton>
              <ElButton size="small" type="danger" plain @click="removeChannel(channel)">
                Delete
              </ElButton>
            </div>
          </div>
        </ElCard>

        <div v-if="availableForAdd.length > 0" class="add-section">
          <h3>Add a channel</h3>
          <div class="add-grid">
            <ElCard
              v-for="entry in availableForAdd"
              :key="entry.type"
              shadow="hover"
              class="add-card"
              :class="{ disabled: entry.implemented === false }"
              @click="entry.implemented !== false && openCreate(entry.type)"
            >
              <div class="add-card-content">
                <Icon :icon="entry.icon || 'mdi:link-variant'" class="add-icon" />
                <div>
                  <div class="add-name">
                    {{ entry.display_name }}
                    <ElTag
                      v-if="entry.implemented === false"
                      type="info" size="small" effect="plain"
                    >coming soon</ElTag>
                  </div>
                  <div class="add-modes">
                    {{ entry.modes.map((m) => m.label).join(' · ') }}
                  </div>
                </div>
              </div>
            </ElCard>
          </div>
        </div>
      </template>
    </div>

    <ChannelEditDialog
      v-model="showDialog"
      :catalog="channelsStore.catalog"
      :channel-type="dialogChannelType"
      :initial="dialogInitial"
      :editing="!!editing"
      :submitting="submitting"
      @submit="handleDialogSubmit"
    />

    <ChannelPairingDialog
      v-model="pairingOpen"
      :channel-id="pairingChannelId"
      :channel-label="pairingChannelLabel"
      @paired="handlePaired"
    />
  </div>
</template>

<style scoped>
.channels-page {
  width: 100%; height: 100%; overflow-y: auto;
  background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.channels-container { max-width: 880px; margin: 0 auto; }
.channels-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.channels-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.channels-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.loading { padding: 60px 0; text-align: center; color: var(--text-secondary); }

.empty-card { margin-bottom: 16px; color: var(--text-secondary); }
.channel-card { margin-bottom: 12px; }
.channel-row { display: flex; align-items: center; gap: 16px; }
.channel-icon {
  width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
  background: var(--hover-bg); border-radius: 10px; font-size: 22px;
  color: var(--primary-color); flex-shrink: 0;
}
.channel-meta { flex: 1; min-width: 0; }
.channel-name { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.channel-sub { font-size: 0.85rem; color: var(--text-secondary); margin-top: 2px; }
.channel-error { font-size: 0.85rem; color: var(--el-color-danger); margin-top: 4px; }
.channel-actions { display: flex; align-items: center; gap: 8px; }

.add-section { margin-top: 32px; }
.add-section h3 { font-size: 1rem; font-weight: 600; margin-bottom: 12px; }
.add-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.add-card { cursor: pointer; }
.add-card.disabled { cursor: not-allowed; opacity: 0.6; }
.add-card-content { display: flex; align-items: center; gap: 12px; }
.add-icon { font-size: 28px; color: var(--primary-color); flex-shrink: 0; }
.add-name { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.add-modes { font-size: 0.8rem; color: var(--text-secondary); margin-top: 2px; }
</style>
