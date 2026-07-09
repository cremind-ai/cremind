<script setup lang="ts">
/**
 * Setup-wizard entry point for restoring from a backup instead of configuring
 * a fresh install. Self-contained so it can drop into the welcome pane without
 * threading state through the 2000-line wizard.
 *
 * Flow: pick a .cremind-backup → upload (open in setup mode) → POST restore →
 * poll restore status → on done, route to the profile selector (the restored
 * profiles + JWT secret are live because the setup-mode restore ran the boot).
 */

import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElInput, ElUpload } from 'element-plus';
import type { UploadRawFile } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../../stores/settings';
import {
  fetchRestoreStatus, startRestore, uploadBackup,
} from '../../services/backupApi';

const emit = defineEmits<{ (e: 'cancel'): void }>();
const router = useRouter();
const settings = useSettingsStore();

const passphrase = ref('');
const phase = ref<string>('idle');
const error = ref<string | null>(null);
const uploadedName = ref<string | null>(null);
const busy = ref(false);

const PHASE_LABEL: Record<string, string> = {
  queued: 'Queued…',
  validate: 'Validating the backup…',
  apply: 'Applying restored data…',
  migrate: 'Booting the restored system…',
  done: 'Restore complete',
  failed: 'Restore failed',
};

let timer: ReturnType<typeof setInterval> | null = null;

function poll() {
  if (timer) return;
  timer = setInterval(async () => {
    try {
      const st = await fetchRestoreStatus(settings.agentUrl, settings.authToken);
      phase.value = st.phase;
      if (st.phase === 'done') {
        clearInterval(timer!); timer = null;
        setTimeout(() => router.push('/'), 1200);
      } else if (st.phase === 'failed') {
        clearInterval(timer!); timer = null;
        error.value = st.error ?? 'Restore failed.';
        busy.value = false;
      }
    } catch {
      // server may be booting storage; keep polling
    }
  }, 1500);
}

async function onFile(file: UploadRawFile): Promise<boolean> {
  error.value = null;
  busy.value = true;
  phase.value = 'validate';
  try {
    const up = await uploadBackup(settings.agentUrl, settings.authToken, file);
    uploadedName.value = up.name;
    if (up.manifest?.encrypted && !passphrase.value) {
      error.value = 'This backup is encrypted — enter its passphrase, then click Restore.';
      phase.value = 'idle';
      busy.value = false;
      return false;
    }
    await doRestore();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    phase.value = 'idle';
    busy.value = false;
  }
  return false; // suppress element-plus default upload
}

async function doRestore() {
  if (!uploadedName.value) return;
  busy.value = true;
  error.value = null;
  phase.value = 'queued';
  try {
    await startRestore(settings.agentUrl, settings.authToken, uploadedName.value, passphrase.value || undefined);
    poll();
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    phase.value = 'idle';
    busy.value = false;
  }
}
</script>

<template>
  <div class="restore-card">
    <h2>Restore from a backup</h2>
    <p class="muted">
      Upload a <code>.cremind-backup</code> archive to recreate a previous
      Cremind system on this install — across machines and database backends.
    </p>

    <ElInput
      v-model="passphrase" type="password" show-password
      placeholder="Passphrase (only if the backup is encrypted)"
      style="margin-bottom: 12px"
    />

    <div class="actions">
      <ElUpload
        v-if="!uploadedName"
        :show-file-list="false"
        :before-upload="onFile"
        accept=".cremind-backup"
        :disabled="busy"
      >
        <ElButton type="primary" :loading="busy">
          <Icon icon="mdi:upload" />&nbsp;Choose backup &amp; restore
        </ElButton>
      </ElUpload>
      <ElButton v-else type="primary" :loading="busy" @click="doRestore">
        Restore {{ uploadedName }}
      </ElButton>
      <ElButton text :disabled="busy" @click="emit('cancel')">Cancel</ElButton>
    </div>

    <p v-if="phase !== 'idle'" class="phase">
      <Icon :icon="phase === 'failed' ? 'mdi:alert-circle' : 'mdi:loading'" :class="{ spin: busy }" />
      {{ PHASE_LABEL[phase] || phase }}
    </p>
    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<style scoped>
.restore-card { max-width: 520px; }
.muted { color: var(--text-secondary); font-size: 0.9rem; }
.actions { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.phase { display: flex; align-items: center; gap: 8px; margin-top: 14px; color: var(--text-secondary); }
.error { color: var(--el-color-danger); margin-top: 10px; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
