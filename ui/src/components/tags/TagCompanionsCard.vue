<script setup lang="ts">
/**
 * Admin: the registered companions (the PC app beside each gateway). Register
 * shows the new hardware credential once; Rotate replaces it (the old one
 * stops working at once); Delete forgets the companion with everything it
 * reported. All three act immediately.
 */
import { ref } from 'vue';
import {
  ElButton, ElCard, ElDialog, ElInput, ElMessage, ElMessageBox, ElTable, ElTableColumn, ElTag,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../../stores/tags';
import type { TagCompanion, TagSecretPayload } from '../../services/tagsApi';
import TagSecretDialog from './TagSecretDialog.vue';
import { formatRelativeTime } from '../../utils/relativeTime';
import { formatTimestamp } from '../../utils/usageFormat';

defineProps<{ companions: TagCompanion[]; deviceCount: Record<string, number>; now: number }>();
const emit = defineEmits<{ (e: 'changed'): void }>();

const store = useTagsStore();
const registerOpen = ref(false);
const name = ref('');
const registering = ref(false);
const busy = ref<string | null>(null);
const secret = ref<TagSecretPayload | null>(null);
const secretTitle = ref('');
const secretOpen = ref(false);

function liveCredential(c: TagCompanion) {
  return (c.credentials ?? []).find((x) => !x.revoked) ?? null;
}

function openRegister() {
  name.value = '';
  registerOpen.value = true;
}

async function register() {
  if (!name.value.trim()) return;
  registering.value = true;
  try {
    const res = await store.registerCompanion(name.value.trim());
    registerOpen.value = false;
    secret.value = res;
    secretTitle.value = `Companion "${res.companion.name}" registered`;
    secretOpen.value = true;
    emit('changed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to register the companion');
  } finally {
    registering.value = false;
  }
}

async function rotate(c: TagCompanion) {
  try {
    await ElMessageBox.confirm(
      `Issue a new hardware credential for "${c.name}"? The current one stops working at once — the companion is offline until you give it the new one.`,
      'Rotate credential',
      { type: 'warning', confirmButtonText: 'Rotate', cancelButtonText: 'Cancel' },
    );
  } catch { return; }
  busy.value = c.id;
  try {
    const res = await store.rotateCompanion(c.id);
    secret.value = res;
    secretTitle.value = `New hardware credential for "${c.name}"`;
    secretOpen.value = true;
    emit('changed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to rotate');
  } finally {
    busy.value = null;
  }
}

async function remove(c: TagCompanion, devices: number) {
  try {
    await ElMessageBox.confirm(
      `Delete "${c.name}"? Its credentials are revoked, and Cremind forgets the ${devices} device${devices === 1 ? '' : 's'} it reported — with their delivery history. This cannot be undone.`,
      'Delete companion',
      { type: 'error', confirmButtonText: 'Delete', cancelButtonText: 'Cancel', confirmButtonClass: 'el-button--danger' },
    );
  } catch { return; }
  busy.value = c.id;
  try {
    await store.deleteCompanion(c.id);
    ElMessage.success('Companion deleted');
    emit('changed');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to delete');
  } finally {
    busy.value = null;
  }
}
</script>

<template>
  <ElCard shadow="never" class="section-card">
    <template #header>
      <div class="section-header-row">
        <div>
          <span class="section-title">Companions</span>
          <p class="section-sub">
            The companion is the small app on the computer a Cremind Tag gateway is plugged into.
            It connects out to Cremind with a hardware credential.
          </p>
        </div>
        <ElButton type="primary" @click="openRegister">
          <Icon icon="mdi:plus" class="btn-icon" /> Register companion
        </ElButton>
      </div>
    </template>
    <ElTable :data="companions" size="small" row-key="id" empty-text="No companion registered yet">
      <ElTableColumn label="Companion" min-width="180">
        <template #default="{ row }">
          <div class="strong">{{ row.name }}</div>
          <div class="muted small">
            <span v-if="row.host">{{ row.host }} · </span>{{ deviceCount[row.id] ?? 0 }} device{{ (deviceCount[row.id] ?? 0) === 1 ? '' : 's' }}
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Status" width="170">
        <template #default="{ row }">
          <ElTag :type="row.online ? 'success' : 'info'" size="small" effect="plain">{{ row.online ? 'online' : 'offline' }}</ElTag>
          <div class="muted small" :title="row.last_seen_at ? formatTimestamp(row.last_seen_at) : ''">
            {{ row.last_seen_at ? `seen ${formatRelativeTime(row.last_seen_at, now)}` : 'never connected' }}
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Version" width="100">
        <template #default="{ row }">{{ row.version || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="Hardware credential" min-width="200">
        <template #default="{ row }">
          <template v-if="liveCredential(row as TagCompanion)">
            <code class="mono">{{ liveCredential(row as TagCompanion)!.id }}</code>
            <div class="muted small">
              {{ liveCredential(row as TagCompanion)!.last_used_at
                ? `used ${formatRelativeTime(liveCredential(row as TagCompanion)!.last_used_at!, now)}`
                : 'never used' }}
            </div>
          </template>
          <ElTag v-else type="danger" size="small" effect="plain">none live</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="" width="170" align="right">
        <template #default="{ row }">
          <ElButton size="small" text :loading="busy === row.id" @click="rotate(row as TagCompanion)">Rotate</ElButton>
          <ElButton size="small" text type="danger" :disabled="busy === row.id" @click="remove(row as TagCompanion, deviceCount[row.id] ?? 0)">Delete</ElButton>
        </template>
      </ElTableColumn>
    </ElTable>

    <ElDialog v-model="registerOpen" title="Register a companion" width="440px" append-to-body>
      <label class="field-label" for="tag-companion-name">Name</label>
      <ElInput id="tag-companion-name" v-model="name" maxlength="128" placeholder="Desk PC" @keyup.enter="register" />
      <p class="field-hint">Usually the computer it runs on. You get its hardware credential next.</p>
      <template #footer>
        <ElButton @click="registerOpen = false">Cancel</ElButton>
        <ElButton type="primary" :loading="registering" :disabled="!name.trim()" @click="register">Register</ElButton>
      </template>
    </ElDialog>

    <TagSecretDialog
      v-model="secretOpen"
      :payload="secret"
      :title="secretTitle"
      purpose="The companion uses it to report its gateways, bridges and tags and to run the hardware operations you queue."
    />
  </ElCard>
</template>

<style scoped>
.section-card { margin-bottom: 16px; }
.section-header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.section-title { font-weight: 600; color: var(--text-primary); }
.section-sub { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-tertiary); max-width: 560px; line-height: 1.45; }
.btn-icon { margin-right: 6px; }
.strong { font-weight: 600; color: var(--text-primary); }
.muted { color: var(--text-tertiary); }
.small { font-size: 0.75rem; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.75rem; color: var(--text-secondary); }
.field-label { display: block; margin-bottom: 6px; font-size: 0.82rem; font-weight: 600; color: var(--text-secondary); }
.field-hint { margin: 6px 0 0; font-size: 0.78rem; color: var(--text-tertiary); }
</style>
