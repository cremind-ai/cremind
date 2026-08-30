<script setup lang="ts">
// Left column of the Group chat page: every room this profile can see, plus
// the admin-only "New group" affordance and its dialog.
//
// The rows follow the conversations panel — same hover and active states, same
// per-row action menu behind a kebab — because they are the same kind of list
// and a room being a different sort of thing is not a reason for it to be a
// different sort of row.
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  ElButton, ElDialog, ElInput, ElMessage, ElMessageBox, ElOption, ElPopover,
  ElSelect,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useGroupChatStore } from '../../stores/groupChat';
import { useSettingsStore } from '../../stores/settings';
import { listProfiles } from '../../services/configApi';
import type { GroupChat } from '../../services/groupChatApi';

const props = defineProps<{
  groups: GroupChat[];
  activeGroupId: string | null;
  loading?: boolean;
}>();

const emit = defineEmits<{ select: [groupId: string] }>();

const store = useGroupChatStore();
const settingsStore = useSettingsStore();
const route = useRoute();
const router = useRouter();

// One menu for the whole list, anchored to whichever kebab opened it — a
// popover per row would mount a teleported node per group.
const menuGroupId = ref<string | null>(null);
const menuTriggerEl = ref<HTMLElement | null>(null);

const openMenu = (groupId: string, el: HTMLElement) => {
  menuTriggerEl.value = el;
  menuGroupId.value = groupId;
};

const openSettingsFromMenu = () => {
  const groupId = menuGroupId.value;
  menuGroupId.value = null;
  if (!groupId) return;
  router.push({
    name: 'group-chat-settings',
    params: { profile: route.params.profile, groupId },
  });
};

const removeFromMenu = async () => {
  const groupId = menuGroupId.value;
  menuGroupId.value = null;
  if (!groupId) return;
  const group = props.groups.find((g) => g.id === groupId);
  try {
    await ElMessageBox.confirm(
      `Delete “${group?.name || groupId}”? Every member's hidden seat and the `
      + 'whole timeline go with it.',
      'Delete group',
      { type: 'warning', confirmButtonText: 'Delete', cancelButtonText: 'Cancel' },
    );
  } catch {
    return; // cancelled
  }
  try {
    // The store closes the room's stream and forgets its state; the view's own
    // watcher moves off a room that was open when it went.
    await store.deleteGroup(groupId);
    ElMessage.success('Group deleted');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to delete the group');
  }
};

const dialogOpen = ref(false);
const creating = ref(false);
const newName = ref('');
const newMembers = ref<string[]>([]);
const profiles = ref<string[]>([]);

// Every profile can hold a seat, ``admin`` included — its agent answers in a
// room exactly like any other member's.
const memberOptions = computed(() =>
  profiles.value.map((p) => ({ value: p, label: `${store.nameFor(p)} (${p})` })),
);

const openDialog = async () => {
  newName.value = '';
  // The profile creating the room is in it by default — uncheck to build a room
  // you only watch.
  newMembers.value = settingsStore.profileId ? [settingsStore.profileId] : [];
  dialogOpen.value = true;
  try {
    const { profiles: names } = await listProfiles(
      settingsStore.agentUrl, settingsStore.authToken,
    );
    profiles.value = names;
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load profiles');
  }
};

const submit = async () => {
  const name = newName.value.trim();
  if (!name) {
    ElMessage.warning('Give the group a name');
    return;
  }
  creating.value = true;
  try {
    const group = await store.createGroup({ name, members: [...newMembers.value] });
    dialogOpen.value = false;
    ElMessage.success('Group created');
    emit('select', group.id);
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to create the group');
  } finally {
    creating.value = false;
  }
};

const previewFor = (group: GroupChat): string => {
  const last = group.last_message;
  if (!last) return 'No messages yet';
  return `${last.sender_name}: ${last.content}`;
};

const memberSummary = (group: GroupChat): string =>
  group.members.map((p) => store.nameFor(p)).join(', ') || 'No members yet';

defineExpose({ openDialog });
</script>

<template>
  <aside class="group-list">
    <div class="list-header">
      <span class="list-title">Groups</span>
      <button
        v-if="store.isAdmin"
        type="button"
        class="new-btn"
        title="Create a group"
        @click="openDialog"
      >
        <Icon icon="mdi:plus" />
      </button>
    </div>

    <div v-if="props.loading && props.groups.length === 0" class="list-empty">Loading…</div>
    <div v-else-if="props.groups.length === 0" class="list-empty">No groups yet</div>

    <!-- The menu is anchored to a row; scrolling the list would leave it
         floating over whatever scrolled into that spot. -->
    <ul v-else class="list-body" @scroll="menuGroupId = null">
      <li
        v-for="group in props.groups"
        :key="group.id"
        class="list-item"
        :class="{ active: group.id === props.activeGroupId }"
        @click="emit('select', group.id)"
      >
        <div class="item-text">
          <div class="item-name">{{ group.name }}</div>
          <div class="item-members">{{ memberSummary(group) }}</div>
          <div class="item-preview">{{ previewFor(group) }}</div>
        </div>
        <!-- Both actions behind it are the admin's, so a member sees no kebab
             rather than an empty menu. -->
        <button
          v-if="store.isAdmin"
          type="button"
          class="item-menu-btn"
          :class="{ 'is-open': menuGroupId === group.id }"
          title="Group actions"
          @click.stop="openMenu(group.id, $event.currentTarget as HTMLElement)"
        >
          <Icon icon="mdi:dots-vertical" />
        </button>
      </li>
    </ul>

    <!-- Shared row-action menu. -->
    <ElPopover
      :visible="menuGroupId !== null"
      :virtual-ref="menuTriggerEl ?? undefined"
      virtual-triggering
      placement="bottom-end"
      :width="170"
      popper-class="group-menu-popover"
      @update:visible="(v) => { if (!v) menuGroupId = null; }"
    >
      <div class="group-menu" role="menu">
        <button
          type="button"
          role="menuitem"
          class="group-menu-item"
          @click="openSettingsFromMenu"
        >
          <Icon icon="mdi:cog-outline" class="group-menu-icon" />
          <span>Settings…</span>
        </button>
        <button
          type="button"
          role="menuitem"
          class="group-menu-item danger"
          @click="removeFromMenu"
        >
          <Icon icon="mdi:delete-outline" class="group-menu-icon" />
          <span>Delete</span>
        </button>
      </div>
    </ElPopover>

    <ElDialog v-model="dialogOpen" title="New group" width="440px">
      <div class="form-row">
        <label class="form-label">Name</label>
        <ElInput v-model="newName" placeholder="Ops" maxlength="128" />
      </div>
      <div class="form-row">
        <label class="form-label">Members</label>
        <ElSelect
          v-model="newMembers"
          multiple
          filterable
          class="form-control"
          placeholder="Pick the profiles that share this room"
        >
          <ElOption
            v-for="opt in memberOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </ElSelect>
        <p class="form-hint">
          Each member gets its own hidden seat in the room and decides for
          itself whether a message was addressed to it.
        </p>
      </div>
      <template #footer>
        <ElButton @click="dialogOpen = false">Cancel</ElButton>
        <ElButton type="primary" :loading="creating" @click="submit">Create</ElButton>
      </template>
    </ElDialog>
  </aside>
</template>

<style scoped>
.group-list {
  width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--surface-color);
  border-right: 1px solid var(--border-color);
  box-sizing: border-box;
  overflow: hidden;
}

.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border-color);
  flex-shrink: 0;
}

.list-title {
  font-size: 0.8rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-tertiary);
}

.new-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  font-size: 16px;
  background: transparent;
  border: none;
  border-radius: 4px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.new-btn:hover {
  color: var(--primary-color);
  background: var(--hover-bg);
}

.list-empty {
  padding: 24px 14px;
  font-size: 0.85rem;
  color: var(--text-tertiary);
  text-align: center;
}

.list-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  margin: 0;
  padding: 6px;
  list-style: none;
}

.list-item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 8px 12px;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.list-item:hover {
  background: var(--hover-bg);
}

.list-item.active {
  background: var(--surface-hover);
  border-color: var(--border-color);
}

.item-text {
  flex: 1;
  min-width: 0;
}

/* Laid out always, revealed on hover — appearing on hover would shift the row's
   text sideways every time the pointer crossed it. */
.item-menu-btn {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease, background 0.15s ease, color 0.15s ease;
}

.list-item:hover .item-menu-btn,
.list-item:focus-within .item-menu-btn,
.item-menu-btn.is-open {
  opacity: 1;
  pointer-events: auto;
}

.item-menu-btn:hover {
  color: var(--primary-color);
  background: var(--hover-bg);
}

.item-name {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-members,
.item-preview {
  font-size: 0.75rem;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-preview {
  margin-top: 2px;
  color: var(--text-secondary);
}

.form-row {
  margin-bottom: 16px;
}

.form-label {
  display: block;
  margin-bottom: 6px;
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-secondary);
}

.form-control {
  width: 100%;
}

.form-hint {
  margin: 6px 0 0 0;
  font-size: 0.75rem;
  line-height: 1.45;
  color: var(--text-tertiary);
}
</style>

<!-- Non-scoped: the row-action ElPopover content is teleported to <body>, so a
     scoped style would not reach the menu rows. Its own class rather than the
     conversations panel's, because that panel's styles ship in a different
     route chunk and a room list would then look right only after visiting the
     chat page first. -->
<style>
.group-menu-popover.el-popover.el-popper {
  padding: 6px;
  min-width: 140px;
}

.group-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.group-menu .group-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  background: transparent;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
  color: var(--text-primary);
  font-size: 0.875rem;
  transition: background 0.15s ease;
}

.group-menu .group-menu-item:hover {
  background: var(--hover-bg);
}

.group-menu .group-menu-item .group-menu-icon {
  font-size: 18px;
  color: var(--text-secondary);
  flex-shrink: 0;
}

.group-menu .group-menu-item.danger,
.group-menu .group-menu-item.danger .group-menu-icon {
  color: var(--danger-color, #e74c3c);
}
</style>
