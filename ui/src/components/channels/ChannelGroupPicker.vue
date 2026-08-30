<script setup lang="ts">
/**
 * Choosing among the groups the account is ALREADY in.
 *
 * The rest of this feature is built on being told: somebody adds the account to
 * a group, a notification appears, the operator approves it. That story has a
 * hole in it, and it is the common case — an account is usually in a dozen
 * groups long before Cremind is pointed at it, nobody "added" it to those, and
 * so nothing will ever announce them. Waiting for someone to happen to post in
 * one is not a way to configure software.
 *
 * Picking here IS approving. The operator is looking at their own group list
 * and saying which ones the agent may work in; asking them to approve the same
 * group again on the page behind this dialog would be ceremony, not consent.
 */
import { computed, ref, watch } from 'vue';
import {
  ElButton, ElCheckbox, ElDialog, ElEmpty, ElMessage, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';

import { useChannelsStore } from '../../stores/channels';
import type { AvailableChannelGroup, ChannelRow } from '../../services/channelApi';

const props = defineProps<{ modelValue: boolean; channel: ChannelRow }>();
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>();

const channelsStore = useChannelsStore();

const loading = ref(false);
const saving = ref(false);
const supported = ref(true);
const rows = ref<AvailableChannelGroup[]>([]);
const picked = ref<Set<string>>(new Set());
const error = ref('');

const open = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
});

/** Groups already known to Cremind can't be picked — they have a row already. */
const selectable = computed(() => rows.value.filter((g) => !g.tracked));

const allSelected = computed(
  () => selectable.value.length > 0
    && selectable.value.every((g) => picked.value.has(g.platform_chat_id)),
);

async function load() {
  loading.value = true;
  error.value = '';
  picked.value = new Set();
  try {
    const result = await channelsStore.loadAvailableGroups(props.channel.id);
    supported.value = result.supported;
    rows.value = result.groups;
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Could not list groups';
    rows.value = [];
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (isOpen) => { if (isOpen) load(); });

function toggle(group: AvailableChannelGroup) {
  if (group.tracked) return;
  const next = new Set(picked.value);
  if (next.has(group.platform_chat_id)) next.delete(group.platform_chat_id);
  else next.add(group.platform_chat_id);
  picked.value = next;
}

function toggleAll() {
  picked.value = allSelected.value
    ? new Set()
    : new Set(selectable.value.map((g) => g.platform_chat_id));
}

async function confirm() {
  const picks = rows.value.filter((g) => picked.value.has(g.platform_chat_id));
  if (!picks.length) return;
  saving.value = true;
  try {
    const added = await channelsStore.addGroups(
      props.channel.id,
      picks.map((g) => ({
        platform_chat_id: g.platform_chat_id,
        title: g.title || undefined,
        chat_type: g.chat_type,
      })),
    );
    ElMessage.success(
      added === 1
        ? 'The agent can now take part in that group.'
        : `The agent can now take part in ${added} groups.`,
    );
    open.value = false;
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Could not enable the groups');
  } finally {
    saving.value = false;
  }
}

function statusLabel(group: AvailableChannelGroup): string {
  if (!group.tracked) return '';
  if (group.tracked.status === 'approved') return 'already on';
  if (group.tracked.status === 'blocked') return 'blocked';
  return 'waiting for approval';
}
</script>

<template>
  <ElDialog
    v-model="open"
    title="Add existing groups"
    width="560px"
    :close-on-click-modal="!saving"
  >
    <p class="picker-intro">
      Groups this account already belongs to. Nobody added the agent to these,
      so they were never announced — pick the ones it may take part in.
    </p>

    <div v-if="loading" class="picker-state">
      <Icon icon="mdi:loading" class="spin" /> Asking the platform…
    </div>

    <div v-else-if="error" class="picker-state error">{{ error }}</div>

    <ElEmpty
      v-else-if="!supported"
      description="This platform won't list the groups an account is in. Add the
        account to a group and say something there — it will appear on this page
        as pending."
    />

    <ElEmpty
      v-else-if="!rows.length"
      description="This account isn't in any groups yet."
    />

    <div v-else class="picker-list">
      <label class="picker-row select-all">
        <ElCheckbox
          :model-value="allSelected"
          :disabled="!selectable.length"
          @change="toggleAll"
        />
        <span class="picker-name">
          Select all ({{ selectable.length }} available)
        </span>
      </label>

      <label
        v-for="group in rows"
        :key="group.platform_chat_id"
        class="picker-row"
        :class="{ 'is-tracked': !!group.tracked }"
      >
        <ElCheckbox
          :model-value="picked.has(group.platform_chat_id)"
          :disabled="!!group.tracked"
          @change="toggle(group)"
        />
        <span class="picker-name">{{ group.title || group.platform_chat_id }}</span>
        <span v-if="group.member_count" class="picker-meta">
          {{ group.member_count }} members
        </span>
        <ElTag v-if="group.tracked" size="small" type="info">
          {{ statusLabel(group) }}
        </ElTag>
      </label>
    </div>

    <template #footer>
      <ElButton :disabled="saving" @click="open = false">Cancel</ElButton>
      <ElButton
        type="primary"
        :loading="saving"
        :disabled="!picked.size"
        @click="confirm"
      >
        Enable {{ picked.size || '' }}
      </ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.picker-intro {
  margin: 0 0 12px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  line-height: 1.5;
}
.picker-state {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 20px 0;
  color: var(--text-secondary);
  font-size: 0.88rem;
}
.picker-state.error { color: var(--danger-color, #f56c6c); }
.spin { animation: picker-spin 1s linear infinite; }
@keyframes picker-spin { to { transform: rotate(360deg); } }

.picker-list {
  max-height: 340px;
  overflow-y: auto;
  border: 1px solid var(--border-color, #e4e7ed);
  border-radius: 8px;
}
.picker-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--border-color-light, #f0f2f5);
}
.picker-row:last-child { border-bottom: none; }
.picker-row.is-tracked { cursor: default; opacity: 0.65; }
.picker-row.select-all { background: var(--bg-secondary, #fafafa); }
.picker-name {
  flex: 1;
  font-size: 0.9rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.picker-meta {
  font-size: 0.78rem;
  color: var(--text-secondary);
}
</style>
