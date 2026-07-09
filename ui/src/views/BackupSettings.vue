<script setup lang="ts">
/**
 * Backup & Restore settings page.
 *
 * Create full-system backups (optionally passphrase-encrypted), list/download/
 * upload/delete archives, and restore one — with live progress that survives
 * the server restart a restore triggers. A post-restore report panel surfaces
 * relocation warnings plus any autostart processes or channels that couldn't
 * start in the (possibly new) environment.
 */

import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ElButton, ElCard, ElDialog, ElEmpty, ElInput, ElMessageBox, ElTable,
  ElTableColumn, ElTag, ElUpload,
} from 'element-plus';
import type { UploadRawFile } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useBackup } from '../composables/useBackup';
import type { BackupEntry } from '../services/backupApi';

const props = defineProps<{ profile: string }>();
const router = useRouter();

const {
  backups, loading, createPhase, createError, restorePhase, restoreError,
  restoreActive, report, hasUnackedReport, refresh, create, restore, upload,
  download, remove, dismissReport,
} = useBackup();

// ── create ────────────────────────────────────────────────────────────────
const showCreate = ref(false);
const createPass = ref('');
const createPassConfirm = ref('');
const creating = computed(() => !['idle', 'done', 'failed'].includes(createPhase.value));

async function doCreate() {
  if (createPass.value && createPass.value !== createPassConfirm.value) {
    ElMessageBox.alert('Passphrases do not match.', 'Error');
    return;
  }
  const pass = createPass.value || undefined;
  showCreate.value = false;
  createPass.value = '';
  createPassConfirm.value = '';
  await create(pass);
}

// ── upload ────────────────────────────────────────────────────────────────
const uploading = ref(false);
async function onUpload(file: UploadRawFile): Promise<boolean> {
  uploading.value = true;
  try {
    await upload(file);
  } catch (e) {
    ElMessageBox.alert(e instanceof Error ? e.message : String(e), 'Upload failed');
  } finally {
    uploading.value = false;
  }
  return false; // prevent element-plus default XHR
}

// ── restore ───────────────────────────────────────────────────────────────
const showRestore = ref(false);
const restoreTarget = ref<BackupEntry | null>(null);
const restorePass = ref('');

function openRestore(entry: BackupEntry) {
  restoreTarget.value = entry;
  restorePass.value = '';
  showRestore.value = true;
}

async function confirmRestore() {
  const target = restoreTarget.value;
  if (!target) return;
  const pass = restorePass.value || undefined;
  showRestore.value = false;
  await restore(target.name, pass);
}

const restoreInFlight = computed(
  () => restoreActive.value || !['idle', 'done', 'failed'].includes(restorePhase.value),
);

const RESTORE_PHASE_LABEL: Record<string, string> = {
  queued: 'Queued…',
  validate: 'Validating the backup…',
  safety_backup: 'Backing up current system…',
  stage: 'Preparing restore…',
  restart: 'Restarting the server…',
  apply: 'Applying restored data…',
  migrate: 'Finalizing…',
  done: 'Restore complete',
  failed: 'Restore failed',
};

// ── delete ────────────────────────────────────────────────────────────────
async function onDelete(entry: BackupEntry) {
  try {
    await ElMessageBox.confirm(
      `Delete backup "${entry.name}"? This cannot be undone.`,
      'Delete backup', { type: 'warning' },
    );
  } catch {
    return;
  }
  await remove(entry.name);
}

async function onDownload(entry: BackupEntry) {
  try {
    await download(entry.name);
  } catch (e) {
    ElMessageBox.alert(e instanceof Error ? e.message : String(e), 'Download failed');
  }
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

onMounted(() => { void refresh(); });
</script>

<template>
  <div class="backup-page">
    <div class="backup-container">
      <div class="backup-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="backup-title">Backup &amp; Restore</h1>
        <p class="backup-subtitle">
          Create a full-system backup (database, skills, channels, settings, and
          files) and restore it into this or a fresh install — across machines
          and database backends.
        </p>
      </div>

      <!-- Restore report banner -->
      <ElCard v-if="hasUnackedReport && report?.report" class="report-card" shadow="never">
        <div class="report-head">
          <Icon :icon="report.report.ok ? 'mdi:check-circle' : 'mdi:alert-circle'" />
          <strong>{{ report.report.ok ? 'Restore completed' : 'Restore failed' }}</strong>
          <ElButton size="small" text @click="dismissReport">Dismiss</ElButton>
        </div>
        <ul class="report-warnings">
          <li v-for="(w, i) in report.report.warnings || []" :key="'w' + i">{{ w }}</li>
          <li v-for="af in report.warnings.autostart_failures" :key="af.id">
            Autostart process failed:
            <code>{{ af.command }}</code> — {{ af.error }}
            (<a @click="router.push(`/${profile}/processes`)">Process Manager</a>)
          </li>
          <li v-for="dc in report.warnings.disabled_channels" :key="dc.id">
            Channel disabled: {{ dc.channel_type }} — {{ dc.error }}
            (<a @click="router.push(`/${profile}/settings/channels`)">Channels</a>)
          </li>
        </ul>
      </ElCard>

      <!-- Create -->
      <ElCard class="section-card" shadow="never">
        <div class="section-row">
          <div>
            <h3>Create a backup</h3>
            <p class="muted">A snapshot of the entire system, written to the server's backups folder.</p>
          </div>
          <ElButton type="primary" :loading="creating" @click="showCreate = true">
            <Icon icon="mdi:plus" />&nbsp;Create backup
          </ElButton>
        </div>
        <p v-if="creating" class="phase-line">Creating backup… [{{ createPhase }}]</p>
        <p v-if="createError" class="error-line">{{ createError }}</p>
      </ElCard>

      <!-- List -->
      <ElCard class="section-card" shadow="never">
        <div class="section-row">
          <h3>Backups</h3>
          <div class="row-actions">
            <ElUpload
              :show-file-list="false"
              :before-upload="onUpload"
              accept=".cremind-backup"
            >
              <ElButton :loading="uploading">
                <Icon icon="mdi:upload" />&nbsp;Upload
              </ElButton>
            </ElUpload>
            <ElButton :loading="loading" @click="refresh">
              <Icon icon="mdi:refresh" />
            </ElButton>
          </div>
        </div>

        <ElEmpty v-if="!backups.length && !loading" description="No backups yet" />
        <ElTable v-else :data="backups" style="width: 100%">
          <ElTableColumn prop="name" label="Name" min-width="220" show-overflow-tooltip />
          <ElTableColumn label="Created" width="180">
            <template #default="{ row }">{{ row.manifest?.created_at || '—' }}</template>
          </ElTableColumn>
          <ElTableColumn label="Size" width="110">
            <template #default="{ row }">{{ fmtSize(row.size_bytes) }}</template>
          </ElTableColumn>
          <ElTableColumn label="DB" width="110">
            <template #default="{ row }">{{ row.manifest?.db_provider || '—' }}</template>
          </ElTableColumn>
          <ElTableColumn label="Encrypted" width="110">
            <template #default="{ row }">
              <ElTag v-if="row.manifest?.encrypted" type="warning" size="small">encrypted</ElTag>
              <span v-else class="muted">no</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="Actions" width="240">
            <template #default="{ row }">
              <ElButton size="small" @click="onDownload(row as BackupEntry)">Download</ElButton>
              <ElButton size="small" type="primary" @click="openRestore(row as BackupEntry)">Restore…</ElButton>
              <ElButton size="small" type="danger" text @click="onDelete(row as BackupEntry)">Delete</ElButton>
            </template>
          </ElTableColumn>
        </ElTable>
      </ElCard>
    </div>

    <!-- Create dialog -->
    <ElDialog v-model="showCreate" title="Create backup" width="440px">
      <p class="muted">
        Optionally encrypt this backup with a passphrase. A backup contains all
        secrets (API keys, tokens) in the clear unless encrypted.
      </p>
      <ElInput v-model="createPass" type="password" placeholder="Passphrase (optional)" show-password />
      <ElInput
        v-model="createPassConfirm" type="password" placeholder="Confirm passphrase"
        show-password style="margin-top: 8px"
      />
      <template #footer>
        <ElButton @click="showCreate = false">Cancel</ElButton>
        <ElButton type="primary" @click="doCreate">Create</ElButton>
      </template>
    </ElDialog>

    <!-- Restore dialog -->
    <ElDialog v-model="showRestore" title="Restore from backup" width="480px">
      <p class="danger-copy">
        <Icon icon="mdi:alert" /> This replaces <strong>ALL</strong> current data
        and restarts the server. A safety backup is taken first.
      </p>
      <p class="muted">Restoring: <code>{{ restoreTarget?.name }}</code></p>
      <ElInput
        v-if="restoreTarget?.manifest?.encrypted"
        v-model="restorePass" type="password" placeholder="Passphrase" show-password
      />
      <template #footer>
        <ElButton @click="showRestore = false">Cancel</ElButton>
        <ElButton type="danger" @click="confirmRestore">Restore</ElButton>
      </template>
    </ElDialog>

    <!-- Restore progress modal -->
    <ElDialog
      :model-value="restoreInFlight || restorePhase === 'failed'"
      title="Restoring" width="440px"
      :show-close="restorePhase === 'failed'"
      :close-on-click-modal="false"
      @update:model-value="(v: boolean) => { if (!v) restorePhase = 'idle'; }"
    >
      <div class="restore-progress">
        <Icon
          :icon="restorePhase === 'failed' ? 'mdi:alert-circle' : 'mdi:loading'"
          :class="{ spin: restoreInFlight }"
        />
        <span>{{ RESTORE_PHASE_LABEL[restorePhase] || restorePhase }}</span>
      </div>
      <p v-if="restoreError" class="error-line">{{ restoreError }}</p>
      <p v-else class="muted">
        The connection will drop while the server restarts — this page reconnects
        automatically and will return you to sign-in when the restore completes.
      </p>
    </ElDialog>
  </div>
</template>

<style scoped>
.backup-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.backup-container { max-width: 860px; margin: 0 auto; }
.backup-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none; border: none;
  color: var(--text-secondary); cursor: pointer; font-size: 0.875rem;
  padding: 4px 0; margin-bottom: 16px;
}
.back-btn:hover { color: var(--primary-color); }
.backup-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px; }
.backup-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; max-width: 640px; }
.section-card { margin-bottom: 16px; }
.section-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.section-row h3 { margin: 0 0 2px; font-size: 1rem; }
.row-actions { display: flex; gap: 8px; align-items: center; }
.muted { color: var(--text-secondary); font-size: 0.8rem; margin: 4px 0 0; }
.phase-line { color: var(--text-secondary); font-size: 0.85rem; margin: 12px 0 0; }
.error-line { color: var(--el-color-danger); font-size: 0.85rem; margin: 8px 0 0; }
.report-card { margin-bottom: 16px; border-left: 3px solid var(--el-color-warning); }
.report-head { display: flex; align-items: center; gap: 8px; }
.report-head .el-button { margin-left: auto; }
.report-warnings { margin: 8px 0 0; padding-left: 20px; font-size: 0.85rem; }
.report-warnings a { color: var(--primary-color); cursor: pointer; }
.danger-copy { color: var(--el-color-danger); display: flex; align-items: center; gap: 6px; }
.restore-progress { display: flex; align-items: center; gap: 10px; font-size: 0.95rem; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
