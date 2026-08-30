<script setup lang="ts">
// Admin-only configuration for one group room. Card layout mirrors
// ChannelsSettings so the two operator-facing pages read the same way.
//
// The settings blob is replaced whole on every save (the backend normalises it
// strictly and answers 400 on anything malformed), so each card sends the full
// blob it built from the current form — never a partial patch.
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElCard, ElInput, ElInputNumber, ElMessage,
  ElMessageBox, ElOption, ElSelect, ElSwitch,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useGroupChatStore } from '../stores/groupChat';
import { useSettingsStore } from '../stores/settings';
import { listProfiles } from '../services/configApi';
import type { GroupChat, GroupSettings } from '../services/groupChatApi';

const props = defineProps<{ profile: string; groupId: string }>();

const router = useRouter();
const store = useGroupChatStore();
const settingsStore = useSettingsStore();

const loading = ref(false);
const savingGeneral = ref(false);
const savingMembers = ref(false);
const profiles = ref<string[]>([]);

const group = computed<GroupChat | null>(
  () => store.groups.find((g) => g.id === props.groupId) ?? null,
);

const form = ref({
  name: '',
  web_sender_name: '',
  max_agent_hops: 6,
  max_agent_posts_per_minute: 30,
  smart_routing: true,
});
const members = ref<string[]>([]);

// Every profile can hold a seat, ``admin`` included — its agent answers in a
// room exactly like any other member's.
const memberOptions = computed(() =>
  profiles.value.map((p) => ({ value: p, label: `${store.nameFor(p)} (${p})` })),
);

/** Copy the stored group into the local form. Deep for the parts we edit in
 *  place, so an abandoned edit never leaks into the store's copy. */
function hydrate(row: GroupChat) {
  form.value = {
    name: row.name,
    web_sender_name: row.settings.web_sender_name,
    max_agent_hops: row.settings.max_agent_hops,
    max_agent_posts_per_minute: row.settings.max_agent_posts_per_minute,
    // A blob stored before this knob existed has no key, and the server reads
    // that as on — so anything but an explicit `false` is on here too.
    smart_routing: row.settings.smart_routing !== false,
  };
  members.value = [...row.members];
}

function buildSettings(): GroupSettings {
  return {
    max_agent_hops: form.value.max_agent_hops,
    max_agent_posts_per_minute: form.value.max_agent_posts_per_minute,
    web_sender_name: form.value.web_sender_name.trim(),
    // Carried by every save: the server rebuilds the blob from its defaults and
    // only overrides what it is sent, so leaving this out would quietly switch
    // routing back on the next time anything else here was edited.
    smart_routing: form.value.smart_routing,
  };
}

async function loadAll() {
  loading.value = true;
  try {
    await Promise.all([
      store.loadGroups(),
      store.loadAgentNames(),
      listProfiles(settingsStore.agentUrl, settingsStore.authToken)
        .then(({ profiles: names }) => { profiles.value = names; })
        .catch(() => { profiles.value = []; }),
    ]);
    const row = group.value;
    if (row) hydrate(row);
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load the group');
  } finally {
    loading.value = false;
  }
}

async function saveGeneral() {
  savingGeneral.value = true;
  try {
    const updated = await store.updateGroup(props.groupId, {
      name: form.value.name.trim(),
      settings: buildSettings(),
    });
    hydrate(updated);
    ElMessage.success('Group updated');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save');
  } finally {
    savingGeneral.value = false;
  }
}

async function saveMembers() {
  savingMembers.value = true;
  try {
    const updated = await store.updateGroup(props.groupId, {
      members: [...members.value],
    });
    hydrate(updated);
    ElMessage.success('Members updated');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save members');
  } finally {
    savingMembers.value = false;
  }
}

// ── danger zone ──
async function removeGroup() {
  try {
    await ElMessageBox.confirm(
      `Delete "${form.value.name}"? Every member's hidden seat and the whole`
      + ' timeline go with it.',
      'Delete group',
      { type: 'warning', confirmButtonText: 'Delete', cancelButtonText: 'Cancel' },
    );
  } catch { return; }
  try {
    await store.deleteGroup(props.groupId);
    ElMessage.success('Group deleted');
    router.replace({ name: 'group-chat', params: { profile: props.profile } });
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to delete the group');
  }
}

function goBack() {
  router.push({
    name: 'group-chat-room',
    params: { profile: props.profile, groupId: props.groupId },
  });
}

onMounted(loadAll);
</script>

<template>
  <div class="group-settings-page">
    <div class="settings-container">
      <div class="settings-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to the room
        </button>
        <h1 class="settings-title">{{ form.name || 'Group chat' }}</h1>
        <p class="settings-subtitle">
          Members, people the agents recognise, and the chats this room mirrors.
        </p>
      </div>

      <div v-if="loading" class="loading">Loading…</div>
      <div v-else-if="!group" class="loading">This group no longer exists.</div>
      <template v-else>
        <ElCard shadow="never" class="section-card">
          <template #header><span class="section-title">General</span></template>
          <div class="field">
            <label class="field-label">Name</label>
            <ElInput v-model="form.name" maxlength="128" />
          </div>
          <div class="field">
            <label class="field-label">Web sender name</label>
            <ElInput v-model="form.web_sender_name" />
            <p class="field-hint">
              The name your posts from this page carry in the room.
            </p>
          </div>
          <div class="field">
            <label class="field-label">Max agent hops</label>
            <ElInputNumber v-model="form.max_agent_hops" :min="0" :max="100" />
            <p class="field-hint">
              How far an agent-to-agent chain may run before further posts are
              delivered silently. A human message resets the count.
            </p>
          </div>
          <div class="field">
            <label class="field-label">Max agent posts per minute</label>
            <ElInputNumber
              v-model="form.max_agent_posts_per_minute" :min="0" :max="600"
            />
          </div>
          <div class="field field-inline">
            <ElSwitch v-model="form.smart_routing" />
            <div>
              <label class="field-label">Route messages with the low-cost model</label>
              <p class="field-hint">
                A cheap model reads each post and starts a turn only for the
                agents it looks addressed to — every member still receives every
                message, and each agent still decides for itself whether to
                answer. Turn it off and every member works on every post. If the
                router cannot run, the whole room is woken.
              </p>
            </div>
          </div>
          <div class="section-actions">
            <ElButton type="primary" :loading="savingGeneral" @click="saveGeneral">
              Save
            </ElButton>
          </div>
        </ElCard>

        <ElCard shadow="never" class="section-card">
          <template #header><span class="section-title">Members</span></template>
          <p class="field-hint">
            Each member profile gets its own hidden seat in this room and runs a
            full turn of its own for every message posted here.
          </p>
          <ElSelect
            v-model="members"
            multiple
            filterable
            class="full-width"
            placeholder="Pick the profiles that share this room"
          >
            <ElOption
              v-for="opt in memberOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </ElSelect>
          <div class="section-actions">
            <ElButton type="primary" :loading="savingMembers" @click="saveMembers">
              Save members
            </ElButton>
          </div>
        </ElCard>

        <ElCard shadow="never" class="section-card danger-card">
          <template #header><span class="section-title">Danger zone</span></template>
          <div class="danger-row">
            <div>
              <div class="danger-title">Delete this group</div>
              <p class="field-hint">
                Removes the room, its timeline, and every member's hidden seat.
              </p>
            </div>
            <ElButton type="danger" plain @click="removeGroup">Delete group</ElButton>
          </div>
        </ElCard>
      </template>
    </div>

  </div>
</template>

<style scoped>
.group-settings-page {
  width: 100%; height: 100%; overflow-y: auto;
  background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.settings-container { max-width: 880px; margin: 0 auto; }
.settings-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.settings-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.settings-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.loading { padding: 40px 0; text-align: center; color: var(--text-secondary); }

.section-card { margin-bottom: 16px; }
.section-title { font-weight: 600; }
.section-header-row {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.section-actions { margin-top: 16px; display: flex; justify-content: flex-end; }

.field { margin-bottom: 16px; }
.field-inline { display: flex; align-items: flex-start; gap: 12px; }
.field-label {
  display: block; margin-bottom: 6px;
  font-size: 0.82rem; font-weight: 600; color: var(--text-secondary);
}
.field-hint {
  margin: 6px 0 0 0; font-size: 0.78rem; line-height: 1.5; color: var(--text-tertiary);
}
.full-width { width: 100%; }

.danger-card { border-color: var(--el-color-danger); }
.danger-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.danger-title { font-weight: 600; color: var(--text-primary); }

</style>
