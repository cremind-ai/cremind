<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import { ElButton, ElCard, ElEmpty, ElMessage, ElTable, ElTableColumn, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useChannelsStore, MAIN_CHANNEL_TYPE } from '../stores/channels';
import type { ChannelRow, ChannelSenderRow } from '../services/channelApi';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const channelsStore = useChannelsStore();

const loading = ref(false);
const senders = ref<Record<string, ChannelSenderRow[]>>({});
const expanded = ref<string | null>(null);

const externalChannels = computed(() =>
  channelsStore.channels.filter((c) => c.channel_type !== MAIN_CHANNEL_TYPE),
);

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

async function toggleExpanded(channel: ChannelRow) {
  if (expanded.value === channel.id) {
    expanded.value = null;
    return;
  }
  expanded.value = channel.id;
  if (!senders.value[channel.id]) {
    try {
      senders.value[channel.id] = await channelsStore.fetchSenders(channel.id);
    } catch (e) {
      ElMessage.error(e instanceof Error ? e.message : 'Failed to load senders');
    }
  }
}

function openConversation(senderRow: ChannelSenderRow) {
  if (!senderRow.conversation_id) return;
  router.push({
    name: 'conversation',
    params: { profile: props.profile, conversationId: senderRow.conversation_id },
  });
}

function displayNameFor(channel: ChannelRow): string {
  return channelsStore.catalog[channel.channel_type]?.display_name || channel.channel_type;
}

function iconFor(channel: ChannelRow): string {
  return channelsStore.catalog[channel.channel_type]?.icon || 'mdi:link-variant';
}

function goSettings() {
  router.push(`/${props.profile}/settings/channels`);
}

function goBack() {
  goBackToChat(router, props.profile);
}

onMounted(loadAll);
</script>

<template>
  <div class="channels-mgmt">
    <button class="back-btn" @click="goBack">
      <Icon icon="mdi:arrow-left" />
      Back to Chat
    </button>
    <div class="channels-mgmt-header">
      <div>
        <h1>Channels</h1>
        <p>Live status and per-sender authentication for connected platforms.</p>
      </div>
      <ElButton type="primary" @click="goSettings">
        <Icon icon="mdi:cog-outline" style="margin-right: 6px" />
        Settings
      </ElButton>
    </div>

    <div v-if="loading" class="loading">Loading…</div>
    <ElEmpty
      v-else-if="externalChannels.length === 0"
      description="No external channels connected. Open Settings → Channels to add one."
    />

    <ElCard
      v-for="channel in externalChannels"
      :key="channel.id"
      shadow="never"
      class="channel-card"
    >
      <div class="channel-row" @click="toggleExpanded(channel)">
        <div class="channel-icon"><Icon :icon="iconFor(channel)" /></div>
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
            <ElTag v-else type="warning" size="small" effect="plain">stopped</ElTag>
          </div>
          <div class="channel-sub">
            Mode {{ channel.mode }} · Auth {{ channel.auth_mode }} · Reply {{ channel.response_mode }}
          </div>
          <div v-if="channel.state?.last_error" class="channel-error">
            {{ channel.state.last_error }}
          </div>
        </div>
        <Icon
          :icon="expanded === channel.id ? 'mdi:chevron-up' : 'mdi:chevron-down'"
          class="chevron"
        />
      </div>

      <div v-if="expanded === channel.id" class="senders">
        <ElTable
          :data="senders[channel.id] || []"
          empty-text="No senders yet"
          size="small"
          stripe
        >
          <ElTableColumn prop="display_name" label="Name" min-width="160">
            <template #default="{ row }">
              {{ row.display_name || row.sender_id }}
            </template>
          </ElTableColumn>
          <ElTableColumn prop="sender_id" label="ID" min-width="160" />
          <ElTableColumn prop="authenticated" label="Auth" width="110">
            <template #default="{ row }">
              <ElTag
                :type="row.authenticated ? 'success' : 'info'"
                size="small" effect="plain"
              >
                {{ row.authenticated ? 'authenticated' : 'pending' }}
              </ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Conversation" width="140">
            <template #default="{ row }">
              <ElButton
                v-if="row.conversation_id"
                size="small" link
                @click="openConversation(row as ChannelSenderRow)"
              >Open</ElButton>
              <span v-else class="muted">—</span>
            </template>
          </ElTableColumn>
        </ElTable>
      </div>
    </ElCard>
  </div>
</template>

<style scoped>
.channels-mgmt { padding: 24px; max-width: 980px; margin: 0 auto; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.channels-mgmt-header {
  display: flex; align-items: flex-start; justify-content: space-between;
  margin-bottom: 16px;
}
.channels-mgmt-header h1 { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px 0; }
.channels-mgmt-header p { font-size: 0.875rem; color: var(--text-secondary); margin: 0; }
.loading { padding: 60px 0; text-align: center; color: var(--text-secondary); }
.channel-card { margin-bottom: 12px; }
.channel-row { display: flex; align-items: center; gap: 16px; cursor: pointer; }
.channel-icon {
  width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
  background: var(--hover-bg); border-radius: 10px; font-size: 22px;
  color: var(--primary-color); flex-shrink: 0;
}
.channel-meta { flex: 1; min-width: 0; }
.channel-name { font-weight: 600; display: flex; align-items: center; gap: 8px; }
.channel-sub { font-size: 0.85rem; color: var(--text-secondary); margin-top: 2px; }
.channel-error { font-size: 0.85rem; color: var(--el-color-danger); margin-top: 4px; }
.chevron { font-size: 20px; color: var(--text-tertiary); }
.senders { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border-color); }
.muted { color: var(--text-tertiary); font-size: 0.85rem; }
</style>
