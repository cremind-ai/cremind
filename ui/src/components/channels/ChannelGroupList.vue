<script setup lang="ts">
/**
 * The platform groups one channel's agent is in.
 *
 * Not the Group Chat page: that is Cremind's own rooms, where several profiles'
 * agents talk to each other. These are real groups on a real platform, full of
 * real people, and the agent stays deaf to each one until it is approved here.
 */
import { computed, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElEmpty, ElMessage, ElMessageBox, ElOption, ElSelect, ElSwitch,
  ElTag, ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';

import { useChannelsStore } from '../../stores/channels';
import ChannelGroupPicker from './ChannelGroupPicker.vue';
import type {
  ChannelGroup, ChannelGroupMember, ChannelRow,
} from '../../services/channelApi';

const props = defineProps<{
  profile: string;
  channel: ChannelRow;
  /** Set by a notification deep-link; highlights one row briefly. */
  highlightGroupId?: string | null;
  /** Set by a deep-link that means "choose from the groups you are in" —
   *  raised when the account joins somewhere with many rooms at once. */
  openPicker?: boolean;
}>();

const router = useRouter();
const channelsStore = useChannelsStore();

const expandedGroup = ref<string | null>(null);
const busy = ref<Record<string, boolean>>({});

const groups = computed<ChannelGroup[]>(() => {
  const rows = channelsStore.groupsByChannel[props.channel.id] || [];
  // Pending first: those are the ones asking for something. Within a status,
  // most recently active first.
  const rank = (g: ChannelGroup) =>
    g.status === 'pending' ? 0 : g.status === 'approved' ? 1 : 2;
  return [...rows].sort(
    (a, b) => rank(a) - rank(b)
      || (b.last_message_at || 0) - (a.last_message_at || 0),
  );
});

const enabled = computed(
  () => channelsStore.groupsEnabledByChannel[props.channel.id] === true,
);
const loading = computed(
  () => channelsStore.groupsLoading[props.channel.id] === true,
);

function statusType(status: string): 'warning' | 'success' | 'danger' | 'info' {
  if (status === 'pending') return 'warning';
  if (status === 'approved') return 'success';
  if (status === 'blocked') return 'danger';
  return 'info';
}

function lastActivity(group: ChannelGroup): string {
  if (!group.last_message_at) return 'no messages yet';
  return new Date(group.last_message_at).toLocaleString();
}

function memberName(member: ChannelGroupMember): string {
  return member.display_name || member.username || member.member_id;
}

async function withBusy(key: string, fn: () => Promise<unknown>) {
  busy.value = { ...busy.value, [key]: true };
  try {
    await fn();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Something went wrong');
  } finally {
    busy.value = { ...busy.value, [key]: false };
  }
}

function setStatus(group: ChannelGroup, status: 'approved' | 'blocked') {
  return withBusy(group.id, async () => {
    await channelsStore.setGroupStatus(props.channel.id, group.id, status);
    ElMessage.success(
      status === 'approved'
        ? `The agent can now take part in ${group.title || 'this group'}.`
        : `The agent will stay out of ${group.title || 'this group'}.`,
    );
  });
}

async function forget(group: ChannelGroup) {
  try {
    await ElMessageBox.confirm(
      `Forget "${group.title || group.platform_chat_id}"? Its conversation and `
      + 'every message in it are removed, and being added again will ask you to '
      + 'approve it afresh. This cannot be undone.',
      'Forget this group',
      { type: 'warning', confirmButtonText: 'Forget', cancelButtonText: 'Cancel' },
    );
  } catch {
    return;  // dismissed
  }
  await withBusy(group.id, async () => {
    await channelsStore.deleteGroup(props.channel.id, group.id);
    ElMessage.success('Group forgotten.');
  });
}

function refreshMembers(group: ChannelGroup) {
  return withBusy(group.id, async () => {
    const source = await channelsStore.refreshGroupRoster(
      props.channel.id, group.id,
    );
    if (source === 'unsupported') {
      ElMessage.info(
        'This platform will not list group members — the list fills in as '
        + 'people post.',
      );
    } else {
      ElMessage.success('Member list refreshed.');
    }
  });
}

function openConversation(group: ChannelGroup) {
  if (!group.conversation_id) return;
  router.push({
    name: 'conversation',
    params: { profile: props.profile, conversationId: group.conversation_id },
  });
}

function toggleMembers(group: ChannelGroup) {
  expandedGroup.value = expandedGroup.value === group.id ? null : group.id;
}

function setPolicyMode(group: ChannelGroup, mode: 'everyone' | 'selected') {
  return withBusy(group.id, () => channelsStore.setGroupSettings(
    props.channel.id, group.id,
    { member_policy: { ...group.settings.member_policy, mode } },
  ));
}

function setRespondMode(
  group: ChannelGroup, mode: 'mention_or_relevant' | 'mention_only',
) {
  return withBusy(group.id, () => channelsStore.setGroupSettings(
    props.channel.id, group.id, { respond_mode: mode },
  ));
}

/**
 * Flip whether the agent answers one member.
 *
 * Which list is edited depends on the mode, so the switch reads the same either
 * way: under "everyone" it adds to / removes from the deny list, under "only
 * selected" the allow list.
 */
function setMemberResponds(
  group: ChannelGroup, member: ChannelGroupMember, responds: boolean,
) {
  const policy = group.settings.member_policy;
  const ids = [member.member_id, ...(member.alt_ids || [])];
  const without = (list: string[]) => list.filter((id) => !ids.includes(id));
  const next = policy.mode === 'selected'
    ? {
      ...policy,
      allow: responds ? [...without(policy.allow), member.member_id] : without(policy.allow),
    }
    : {
      ...policy,
      deny: responds ? without(policy.deny) : [...without(policy.deny), member.member_id],
    };
  return withBusy(group.id, () => channelsStore.setGroupSettings(
    props.channel.id, group.id, { member_policy: next },
  ));
}

// Whether this platform can be asked which groups the account is in. When it
// cannot, waiting for somebody to post is genuinely the only route in, and the
// empty state says so rather than offering a button that would always fail.
const canList = computed(() => {
  const rows = channelsStore.groupsByChannel[props.channel.id] || [];
  return rows.length ? rows[0].capabilities.listing : true;
});

// In a template expression an apostrophe would end the attribute, so the two
// wordings live here.
const emptyDescription = computed(() => (canList.value
  ? 'No groups yet — add this account to a group, or pick one it is already in.'
  : "No groups yet — add this channel's account to a group and say something "
    + 'there.'));

const pickerOpen = ref(false);
watch(() => props.openPicker, (open) => { if (open) pickerOpen.value = true; },
  { immediate: true });

function goSettings() {
  router.push({ name: 'channels-settings', params: { profile: props.profile } });
}
</script>

<template>
  <div class="channel-groups">
    <div class="groups-head">
      <h4>Group chats</h4>
      <div class="groups-actions">
        <ElButton
          v-if="enabled && canList"
          size="small"
          @click="pickerOpen = true"
        >
          <Icon icon="mdi:playlist-plus" /> Add existing groups…
        </ElButton>
        <ElButton
          v-if="enabled"
          size="small"
          text
          :loading="loading"
          @click="channelsStore.loadGroups(props.channel.id)"
        >
          <Icon icon="mdi:refresh" />
        </ElButton>
      </div>
    </div>

    <p v-if="!enabled" class="groups-hint">
      Group chats are off for this channel — the agent ignores anything said in
      a group it is added to.
      <a href="#" @click.prevent="goSettings()">Turn them on in Settings</a>.
    </p>

    <template v-else>
      <p class="groups-hint">
        Every group this account is added to <em>from now on</em> lands here as
        <strong>pending</strong>, and the agent reads nothing in it until you
        approve it.
        <template v-if="canList">
          Groups it already belonged to were never announced — use
          <strong>Add existing groups</strong> to choose among those.
        </template>
      </p>

      <ElEmpty
        v-if="!groups.length"
        :image-size="60"
        :description="emptyDescription"
      />

      <div
        v-for="group in groups"
        :key="group.id"
        class="group-row"
        :class="{ highlight: group.id === props.highlightGroupId }"
        :data-group-id="group.id"
      >
        <div class="group-main">
          <div class="group-title">
            {{ group.title || group.platform_chat_id }}
            <ElTag :type="statusType(group.status)" size="small" effect="plain">
              {{ group.status }}
            </ElTag>
          </div>
          <div class="group-sub">
            <code>{{ group.platform_chat_id }}</code>
            · {{ group.member_count }} known
            · {{ lastActivity(group) }}
          </div>
        </div>

        <div class="group-actions">
          <ElButton
            v-if="group.status !== 'approved'"
            size="small" type="primary"
            :loading="busy[group.id]"
            @click="setStatus(group, 'approved')"
          >Approve</ElButton>
          <ElButton
            v-if="group.status !== 'blocked'"
            size="small"
            :loading="busy[group.id]"
            @click="setStatus(group, 'blocked')"
          >Block</ElButton>
          <ElButton
            size="small"
            :disabled="!group.conversation_id"
            @click="openConversation(group)"
          >Open</ElButton>
          <ElButton size="small" @click="toggleMembers(group)">
            {{ expandedGroup === group.id ? 'Hide members' : 'Members' }}
          </ElButton>
          <ElButton
            size="small" type="danger" text
            :loading="busy[group.id]"
            @click="forget(group)"
          >Forget</ElButton>
        </div>

        <div v-if="expandedGroup === group.id" class="group-members">
          <div class="member-controls">
            <label>
              Answers
              <ElSelect
                :model-value="group.settings.member_policy.mode"
                size="small"
                style="width: 170px"
                @update:model-value="(v: any) => setPolicyMode(group, v)"
              >
                <ElOption value="everyone" label="Everyone in the group" />
                <ElOption value="selected" label="Only selected people" />
              </ElSelect>
            </label>
            <label>
              Speaks
              <ElSelect
                :model-value="group.settings.respond_mode"
                size="small"
                style="width: 210px"
                @update:model-value="(v: any) => setRespondMode(group, v)"
              >
                <ElOption
                  value="mention_or_relevant"
                  label="When mentioned or relevant"
                />
                <ElOption value="mention_only" label="Only when mentioned" />
              </ElSelect>
            </label>
            <ElTooltip
              :disabled="group.capabilities.roster"
              content="This platform will not list group members — the list fills in as people post."
            >
              <span>
                <ElButton
                  size="small"
                  :disabled="!group.capabilities.roster"
                  :loading="busy[group.id]"
                  @click="refreshMembers(group)"
                >Refresh members</ElButton>
              </span>
            </ElTooltip>
          </div>

          <p v-if="!group.members.length" class="groups-hint">
            Nobody recorded yet. Members appear as they post.
          </p>
          <div
            v-for="member in group.members"
            :key="member.member_id"
            class="member-row"
          >
            <div class="member-name">
              {{ memberName(member) }}
              <ElTag v-if="member.is_bot" size="small" type="info" effect="plain">bot</ElTag>
              <ElTag v-if="member.role === 'admin'" size="small" effect="plain">admin</ElTag>
              <ElTag size="small" type="info" effect="plain">{{ member.source }}</ElTag>
            </div>
            <code class="member-id">{{ member.member_id }}</code>
            <ElSwitch
              :model-value="member.responds"
              size="small"
              @update:model-value="(v: any) => setMemberResponds(group, member, v === true)"
            />
          </div>
        </div>
      </div>
    </template>

    <ChannelGroupPicker
      v-if="enabled && canList"
      v-model="pickerOpen"
      :channel="props.channel"
    />
  </div>
</template>

<style scoped>
.channel-groups {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.groups-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.groups-head h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}
/* Pushes the actions to the right of the heading. */
.groups-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 4px;
}
.groups-hint {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}
.group-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px 12px;
  padding: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  margin-bottom: 8px;
  transition: background-color 0.4s ease, border-color 0.4s ease;
}
.group-row.highlight {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}
.group-sub {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.group-actions {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  flex-wrap: wrap;
}
.group-members {
  grid-column: 1 / -1;
  margin-top: 6px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.member-controls {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.member-controls label {
  display: flex;
  align-items: center;
  gap: 6px;
}
.member-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 12px;
  padding: 4px 0;
  font-size: 13px;
}
.member-name {
  display: flex;
  align-items: center;
  gap: 6px;
}
.member-id {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
</style>
