<script setup lang="ts">
import { ref, computed, nextTick, onMounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import { ElBadge, ElButton, ElCard, ElEmpty, ElMessage, ElMessageBox, ElOption, ElSelect, ElTable, ElTableColumn, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import ChannelGroupList from '../components/channels/ChannelGroupList.vue';

import { useChannelsStore, MAIN_CHANNEL_TYPE } from '../stores/channels';
import type { ChannelRow, ChannelSenderRow } from '../services/channelApi';
import { formatTokens, formatTokensCompact, formatUsd } from '../utils/usageFormat';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const route = useRoute();
const channelsStore = useChannelsStore();

const loading = ref(false);
const senders = ref<Record<string, ChannelSenderRow[]>>({});
const expanded = ref<string | null>(null);
// Set by a `channel_group_request` notification's deep link; cleared a few
// seconds later so the row stops standing out once it has been found.
const highlightGroupId = ref<string | null>(null);
// Channel whose "add existing groups" dialog a deep link asked to open.
const pickerChannelId = ref<string | null>(null);
let highlightTimer: ReturnType<typeof setTimeout> | null = null;

const externalChannels = computed(() =>
  channelsStore.channels.filter((c) => c.channel_type !== MAIN_CHANNEL_TYPE),
);

async function loadAll() {
  loading.value = true;
  try {
    await Promise.all([channelsStore.loadCatalog(), channelsStore.loadChannels()]);
    // Eagerly, for every group-capable channel: the pending badge has to show
    // on a COLLAPSED card, and a group nobody can see is a group nobody
    // approves.
    await channelsStore.loadGroupsForAll();
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
  await expandChannel(channel.id);
}

async function expandChannel(channelId: string) {
  expanded.value = channelId;
  if (!senders.value[channelId]) {
    try {
      senders.value[channelId] = await channelsStore.fetchSenders(channelId);
    } catch (e) {
      ElMessage.error(e instanceof Error ? e.message : 'Failed to load senders');
    }
  }
}

/**
 * Land on the group a notification was about.
 *
 * Follows the pattern the skills page already uses for `?skillId=…&tour=1`:
 * read the query, act on it, then `replace` it away so a refresh (or a Back)
 * does not re-trigger the jump.
 */
async function applyDeepLink() {
  const channelId = String(route.query.channel || '');
  const groupId = String(route.query.group || '');
  // A notification with no group id means "you joined somewhere with many
  // rooms" (a Discord server): there is nothing to approve yet, so it opens the
  // picker instead of highlighting a row.
  const pick = String(route.query.pick || '') === '1';
  if (!channelId) return;
  await expandChannel(channelId);
  try {
    await channelsStore.loadGroups(channelId);
  } catch {
    // The card still opens; the list shows whatever it already had.
  }
  if (groupId) {
    await nextTick();
    const selector = `[data-group-id="${(window as any).CSS?.escape
      ? CSS.escape(groupId) : groupId}"]`;
    document.querySelector(selector)?.scrollIntoView({
      behavior: 'smooth', block: 'center',
    });
    highlightGroupId.value = groupId;
    if (highlightTimer) clearTimeout(highlightTimer);
    highlightTimer = setTimeout(() => { highlightGroupId.value = null; }, 4000);
  }
  if (pick) {
    pickerChannelId.value = channelId;
    await nextTick();
    // One-shot: the child opens on the rising edge, so this can be released
    // immediately and the operator can close and reopen the dialog by hand.
    pickerChannelId.value = null;
  }
  router.replace({ query: {} });
}

function openConversation(senderRow: ChannelSenderRow) {
  if (!senderRow.conversation_id) return;
  router.push({
    name: 'conversation',
    params: { profile: props.profile, conversationId: senderRow.conversation_id },
  });
}

async function setSenderAuth(
  channel: ChannelRow, senderRow: ChannelSenderRow, authenticated: boolean,
) {
  try {
    const updated = await channelsStore.setSenderAuthenticated(
      channel.id, senderRow.sender_id, authenticated,
    );
    const list = senders.value[channel.id];
    if (list) {
      const idx = list.findIndex((s) => s.sender_id === updated.sender_id);
      // The PATCH response carries no usage totals — keep the ones we loaded,
      // otherwise approving someone blanks their Usage cell until a refetch.
      if (idx >= 0) list[idx] = { ...updated, usage: list[idx].usage };
    }
    ElMessage.success(authenticated ? 'Subscriber approved' : 'Subscriber revoked');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to update subscriber');
  }
}

const clearing = ref<string | null>(null);
const deleting = ref<string | null>(null);

/** Tooltip detail behind the compact Usage cell. */
function usageTooltip(senderRow: ChannelSenderRow): string {
  const u = senderRow.usage;
  if (!u) return '';
  return [
    `Input: ${formatTokens(u.input_tokens)}`,
    `Cache read: ${formatTokens(u.cache_read_input_tokens)}`,
    `Cache write: ${formatTokens(u.cache_creation_input_tokens)}`,
    `Output: ${formatTokens(u.output_tokens)}`,
    `Requests: ${formatTokens(u.request_count)}`,
    `Cost: ${formatUsd(u.total_usd)}`,
  ].join(' · ');
}

async function clearSenderHistory(channel: ChannelRow, senderRow: ChannelSenderRow) {
  const who = senderRow.display_name || senderRow.sender_id;
  try {
    await ElMessageBox.confirm(
      `Permanently delete every message in ${who}'s conversation? `
      + 'They keep the same conversation — their next message continues in it — '
      + 'and the token usage totals shown here are kept.',
      'Clear history',
      { type: 'warning', confirmButtonText: 'Clear history', cancelButtonText: 'Cancel', confirmButtonClass: 'el-button--danger' },
    );
  } catch {
    return; // dismissed
  }
  clearing.value = senderRow.sender_id;
  try {
    const res = await channelsStore.clearSenderHistory(channel.id, senderRow.sender_id);
    ElMessage.success(
      res.cleared_messages > 0
        ? `Cleared ${res.cleared_messages} message${res.cleared_messages === 1 ? '' : 's'}`
        : 'No messages to clear',
    );
    senders.value[channel.id] = await channelsStore.fetchSenders(channel.id);
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to clear history');
  } finally {
    clearing.value = null;
  }
}

/** Per-client override of "confirm before messaging clients". */
async function setSenderConfirmation(
  channel: ChannelRow, senderRow: ChannelSenderRow,
  mode: 'required' | 'skip' | null,
) {
  const previous = senderRow.send_confirmation ?? null;
  const list = senders.value[channel.id];
  const idx = list ? list.findIndex((s) => s.sender_id === senderRow.sender_id) : -1;
  // Optimistic: the ElSelect has already moved, so show the new value and put it
  // back if the server refuses.
  if (idx >= 0 && list) list[idx] = { ...list[idx], send_confirmation: mode };
  try {
    const updated = await channelsStore.setSenderConfirmation(
      channel.id, senderRow.sender_id, mode,
    );
    // The PATCH response carries no usage totals — keep the ones we loaded.
    if (idx >= 0 && list) list[idx] = { ...updated, usage: list[idx].usage };
    ElMessage.success(
      mode === 'skip'
        ? 'Messages to this client will send directly'
        : mode === 'required'
          ? 'This client will always be confirmed'
          : 'This client now follows the profile setting',
    );
  } catch (e) {
    if (idx >= 0 && list) {
      list[idx] = { ...list[idx], send_confirmation: previous };
    }
    ElMessage.error(e instanceof Error ? e.message : 'Failed to update client');
  }
}

async function deleteSender(channel: ChannelRow, senderRow: ChannelSenderRow) {
  const who = senderRow.display_name || senderRow.sender_id;
  try {
    await ElMessageBox.confirm(
      `Completely delete ${who} from Cremind? This removes their conversation `
      + 'and every message in it, any automations they set up, their phone and '
      + 'contact details, and their access approval — if they write again they '
      + 'arrive as a brand-new client. Recorded usage and cost stay in your '
      + 'account totals but stop being attributed to anyone. '
      + 'This cannot be undone.',
      'Delete client',
      {
        type: 'error',
        confirmButtonText: 'Delete client',
        cancelButtonText: 'Cancel',
        confirmButtonClass: 'el-button--danger',
      },
    );
  } catch {
    return; // dismissed
  }
  deleting.value = senderRow.sender_id;
  try {
    const res = await channelsStore.deleteSender(channel.id, senderRow.sender_id);
    ElMessage.success(
      res.deleted_messages > 0
        ? `Deleted ${who} and ${res.deleted_messages} message${res.deleted_messages === 1 ? '' : 's'}`
        : `Deleted ${who}`,
    );
    senders.value[channel.id] = await channelsStore.fetchSenders(channel.id);
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to delete client');
  } finally {
    deleting.value = null;
  }
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

/** Deep-link to the profile-wide confirmation switch (highlights the group). */
function goConfirmSetting() {
  router.push({
    path: `/${props.profile}/settings/config`,
    query: { section: 'channels' },
  });
}

function goBack() {
  goBackToChat(router, props.profile);
}

function supportsGroups(channel: ChannelRow): boolean {
  return channelsStore.supportsGroupChats(channel.channel_type);
}

function pendingGroups(channel: ChannelRow): number {
  return channelsStore.pendingGroupCount(channel.id);
}

function groupsEnabled(channel: ChannelRow): boolean {
  return channelsStore.groupsEnabledByChannel[channel.id] === true;
}

onMounted(async () => {
  await loadAll();
  await applyDeepLink();
});

watch(() => route.query, () => { applyDeepLink(); });
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
            <ElBadge
              v-if="supportsGroups(channel) && pendingGroups(channel)"
              :value="pendingGroups(channel)"
              type="warning"
            >
              <span class="badge-anchor">{{ displayNameFor(channel) }}</span>
            </ElBadge>
            <template v-else>{{ displayNameFor(channel) }}</template>
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
            <template v-if="supportsGroups(channel)">
              · Group chats {{ groupsEnabled(channel) ? 'on' : 'off' }}
            </template>
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
        <p class="senders-hint">
          "Confirm before send" decides whether the agent asks you before
          messaging that client. The default for every client comes from
          <a href="#" @click.prevent="goConfirmSetting()">Settings → Config → Channels</a>;
          set a client to "Send directly" so automations can reach them without
          stopping to ask. Someone who has never messaged this channel is always
          confirmed.
        </p>
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
          <ElTableColumn prop="phone" label="Phone" width="150">
            <template #default="{ row }">
              <span v-if="row.phone">+{{ row.phone }}</span>
              <span v-else class="muted">—</span>
            </template>
          </ElTableColumn>
          <ElTableColumn prop="authenticated" label="Status" width="110">
            <template #default="{ row }">
              <ElTag
                :type="row.authenticated ? 'success' : 'info'"
                size="small" effect="plain"
              >
                {{ row.authenticated ? 'subscribed' : 'pending' }}
              </ElTag>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Confirm before send" width="170">
            <template #default="{ row }">
              <ElSelect
                :model-value="row.send_confirmation ?? ''"
                size="small"
                @update:model-value="(v: any) => setSenderConfirmation(
                  channel, row as ChannelSenderRow, v === '' ? null : v,
                )"
              >
                <ElOption label="Profile default" value="" />
                <ElOption label="Always ask" value="required" />
                <ElOption label="Send directly" value="skip" />
              </ElSelect>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Usage" width="150">
            <template #default="{ row }">
              <span v-if="row.usage" class="usage-cell" :title="usageTooltip(row as ChannelSenderRow)">
                {{ formatTokensCompact(row.usage.total_tokens) }} tok
                <span class="usage-cost">{{ formatUsd(row.usage.total_usd) }}</span>
              </span>
              <span v-else class="muted">—</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Subscription" width="130">
            <template #default="{ row }">
              <ElButton
                v-if="!row.authenticated"
                size="small" type="success" link
                @click="setSenderAuth(channel, row as ChannelSenderRow, true)"
              >Approve</ElButton>
              <ElButton
                v-else
                size="small" type="danger" link
                @click="setSenderAuth(channel, row as ChannelSenderRow, false)"
              >Revoke</ElButton>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Actions" min-width="250">
            <template #default="{ row }">
              <template v-if="row.conversation_id">
                <ElButton
                  size="small" link
                  @click="openConversation(row as ChannelSenderRow)"
                >Open</ElButton>
                <ElButton
                  size="small" type="danger" link
                  :loading="clearing === row.sender_id"
                  @click="clearSenderHistory(channel, row as ChannelSenderRow)"
                >Clear history</ElButton>
              </template>
              <!-- Deleting the client is offered even for someone with no
                   conversation yet: a pending or revoked contact still has a
                   sender record, and removing it is exactly how you undo an
                   unwanted first contact. -->
              <ElButton
                size="small" type="danger" link
                :loading="deleting === row.sender_id"
                @click="deleteSender(channel, row as ChannelSenderRow)"
              >Delete</ElButton>
            </template>
          </ElTableColumn>
        </ElTable>

        <ChannelGroupList
          v-if="supportsGroups(channel)"
          :profile="props.profile"
          :channel="channel"
          :highlight-group-id="highlightGroupId"
          :open-picker="pickerChannelId === channel.id"
        />
      </div>
    </ElCard>
  </div>
</template>

<style scoped>
/* Element Plus positions the badge against its slot; without a plain inline
   anchor it hangs off the flex row's full width instead of off the name. */
.badge-anchor {
  display: inline-block;
}

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
.senders-hint {
  margin: 0 0 10px;
  color: var(--text-tertiary);
  font-size: 0.85rem;
  line-height: 1.5;
}
.senders-hint a { color: var(--el-color-primary); text-decoration: none; }
.senders-hint a:hover { text-decoration: underline; }
.muted { color: var(--text-tertiary); font-size: 0.85rem; }
.usage-cell { font-size: 0.85rem; white-space: nowrap; cursor: default; }
.usage-cost { color: var(--text-secondary); margin-left: 6px; }
</style>
