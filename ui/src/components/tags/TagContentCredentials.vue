<script setup lang="ts">
/**
 * This profile's content credentials: what a companion uses to fetch this
 * profile's cards (and only this profile's, only for tags it owns on that
 * companion). Create and revoke act at once — they are not part of the
 * settings form's save bar.
 */
import { computed, onMounted, ref } from 'vue';
import {
  ElButton, ElCard, ElDialog, ElInput, ElMessage, ElMessageBox, ElOption, ElSelect,
  ElTable, ElTableColumn, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import type { TagCredential, TagSecretPayload } from '../../services/tagsApi';
import TagSecretDialog from './TagSecretDialog.vue';
import { formatRelativeTime } from '../../utils/relativeTime';
import { formatTimestamp } from '../../utils/usageFormat';

const props = defineProps<{ now: number }>();

const store = useTagsStore();
const loading = ref(false);
const createOpen = ref(false);
const companionId = ref('');
const label = ref('');
const creating = ref(false);
const secret = ref<TagSecretPayload | null>(null);
const secretOpen = ref(false);
const revoking = ref<string | null>(null);

const companions = computed(() => store.companions);
const companionName = (id: string) => companions.value.find((c) => c.id === id)?.name || id.slice(0, 8);
const rows = computed(() => [...store.credentials].sort((a, b) => Number(a.revoked) - Number(b.revoked) || b.created_at - a.created_at));

async function load() {
  loading.value = true;
  try {
    await Promise.all([store.loadCredentials(), store.loadCompanions()]);
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load credentials');
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  companionId.value = companions.value.length === 1 ? companions.value[0].id : '';
  label.value = '';
  createOpen.value = true;
}

async function create() {
  if (!companionId.value) return;
  creating.value = true;
  try {
    secret.value = await store.createCredential(companionId.value, label.value);
    createOpen.value = false;
    secretOpen.value = true;
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to create the credential');
  } finally {
    creating.value = false;
  }
}

async function revoke(cred: TagCredential) {
  try {
    await ElMessageBox.confirm(
      `Revoke "${cred.label}"? The companion using it stops receiving this profile's cards at once. This cannot be undone.`,
      'Revoke credential',
      { type: 'warning', confirmButtonText: 'Revoke', cancelButtonText: 'Cancel', confirmButtonClass: 'el-button--danger' },
    );
  } catch { return; }
  revoking.value = cred.id;
  try {
    await store.revokeCredential(cred.id);
    ElMessage.success('Credential revoked');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to revoke');
  } finally {
    revoking.value = null;
  }
}

onMounted(load);
defineExpose({ reload: load });
</script>

<template>
  <ElCard shadow="never" class="section-card">
    <template #header>
      <div class="section-header-row">
        <div>
          <span class="section-title">Content credentials</span>
          <p class="section-sub">
            A companion needs one to fetch this profile's cards. It only ever sees this profile's
            cards, for tags this profile owns on that companion.
          </p>
        </div>
        <ElButton type="primary" :disabled="companions.length === 0" @click="openCreate">
          <Icon icon="mdi:key-plus" class="btn-icon" /> New credential
        </ElButton>
      </div>
    </template>
    <p v-if="!loading && companions.length === 0" class="muted">
      No companion is registered yet. An admin registers one under Settings → Tags → Hardware.
    </p>
    <ElTable :data="rows" size="small" row-key="id" empty-text="No content credentials yet" v-loading="loading">
      <ElTableColumn label="Label" min-width="170">
        <template #default="{ row }">
          <div class="label-cell">{{ row.label }}</div>
          <code class="mono muted">{{ row.id }}</code>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Companion" min-width="130">
        <template #default="{ row }">{{ companionName(row.companion_id) }}</template>
      </ElTableColumn>
      <ElTableColumn label="Created" width="120">
        <template #default="{ row }">
          <span :title="formatTimestamp(row.created_at)">{{ formatRelativeTime(row.created_at, props.now) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Last used" width="120">
        <template #default="{ row }">
          <span v-if="row.last_used_at" :title="formatTimestamp(row.last_used_at)">{{ formatRelativeTime(row.last_used_at, props.now) }}</span>
          <span v-else class="muted">never</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Status" width="100">
        <template #default="{ row }">
          <ElTag :type="row.revoked ? 'info' : 'success'" size="small" effect="plain">{{ row.revoked ? 'revoked' : 'active' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="" width="96" align="right">
        <template #default="{ row }">
          <ElButton
            v-if="!row.revoked"
            size="small" type="danger" text
            :loading="revoking === row.id"
            @click="revoke(row as TagCredential)"
          >Revoke</ElButton>
        </template>
      </ElTableColumn>
    </ElTable>

    <ElDialog v-model="createOpen" title="New content credential" width="460px" append-to-body>
      <div class="field">
        <label class="field-label">Companion</label>
        <ElSelect v-model="companionId" placeholder="Pick the companion" class="full">
          <ElOption v-for="c in companions" :key="c.id" :label="c.name" :value="c.id">
            <span>{{ c.name }}</span>
            <span class="opt-meta">{{ c.online ? 'online' : 'offline' }}</span>
          </ElOption>
        </ElSelect>
      </div>
      <div class="field">
        <label class="field-label">Label <span class="optional">(optional)</span></label>
        <ElInput v-model="label" maxlength="128" placeholder="Desk PC" @keyup.enter="create" />
      </div>
      <template #footer>
        <ElButton @click="createOpen = false">Cancel</ElButton>
        <ElButton type="primary" :loading="creating" :disabled="!companionId" @click="create">Create</ElButton>
      </template>
    </ElDialog>

    <TagSecretDialog
      v-model="secretOpen"
      :payload="secret"
      title="Content credential created"
      purpose="The companion uses it to fetch this profile's cards."
    />
  </ElCard>
</template>

<style scoped>
.section-card { margin-bottom: 16px; }
.section-header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.section-title { font-weight: 600; color: var(--text-primary); }
.section-sub { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-tertiary); max-width: 560px; line-height: 1.45; }
.btn-icon { margin-right: 6px; }
.label-cell { color: var(--text-primary); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.75rem; }
.muted { color: var(--text-tertiary); font-size: 0.85rem; }
.field { margin-bottom: 14px; }
.field-label { display: block; margin-bottom: 6px; font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.optional { font-weight: 400; color: var(--text-tertiary); }
.full { width: 100%; }
.opt-meta { float: right; color: var(--text-tertiary); font-size: 0.8rem; }
</style>
