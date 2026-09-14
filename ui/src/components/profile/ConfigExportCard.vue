<script setup lang="ts">
/**
 * Re-download the Setup Wizard's configuration file.
 *
 * The wizard offers that file exactly once, on its last step; losing it
 * afterwards used to mean digging the JWT, the paths, the database credentials
 * and the VNC password back out of the server by hand. The server renders it
 * now (``GET /api/config/export``), so this card is a format picker and a
 * download — and, because the endpoint is scoped to the caller's own token
 * rather than gated on admin, it lives on the Profile page where every profile
 * can reach its own file.
 */
import { computed, ref } from 'vue';
import { ElButton, ElMessage, ElOption, ElSelect } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../../stores/settings';
import { fetchConfigExport } from '../../services/configApi';
import { downloadTextFile, type ExportFormat } from '../../utils/configExport';

const settingsStore = useSettingsStore();

const exportFormat = ref<ExportFormat>('md');
const downloading = ref(false);
const error = ref<string | null>(null);

const profile = computed(() => settingsStore.profileId);
/** Only the admin profile's file carries the install-wide sections. */
const isAdmin = computed(() => settingsStore.profileId === 'admin');

/** The endpoint no longer refuses non-admins, so the only permission failure
 *  left is a session that has expired. Saying so beats echoing a bare 401. */
function describeFailure(message: string): string {
  if (/unauthor|401|invalid token|expired/i.test(message)) {
    return `${message} — your session has expired. Sign in again as `
      + `${profile.value} to download it.`;
  }
  return message;
}

async function handleDownload() {
  const agentUrl = settingsStore.agentUrl;
  const token = settingsStore.authToken;
  if (!token) {
    ElMessage.warning('Not authenticated.');
    return;
  }
  downloading.value = true;
  error.value = null;
  try {
    const { text, mime } = await fetchConfigExport(agentUrl, token, exportFormat.value);
    downloadTextFile(`cremind-${profile.value}-config.${exportFormat.value}`, text, mime);
  } catch (e) {
    const message = describeFailure(e instanceof Error ? e.message : String(e));
    error.value = message;
    ElMessage.error(message);
  } finally {
    downloading.value = false;
  }
}
</script>

<template>
  <div class="config-export">
    <p class="config-export-description">
      Download the configuration file the Setup Wizard produced for
      <strong>{{ profile }}</strong>, refreshed from the running server: the JWT
      token and its expiry, the agent URL and the link to sign in with, where the
      token is kept on the server, the project paths, the deployment, whether
      vector embedding is on, and this profile's channels.
      <template v-if="isAdmin">
        As the <code>admin</code> profile it also carries the install-wide
        sections: database and vector-store parameters, embedding settings, the
        VNC password on desktop container installs, and on Kubernetes the
        namespace, Helm release and Deployment/Service plus the
        <code>kubectl port-forward</code> command that reconnects to them.
      </template>
      <template v-else>
        The database, vector-store, desktop and Kubernetes sections describe the
        server rather than your profile, so they are only in the
        <code>admin</code> profile's file.
      </template>
    </p>
    <p class="config-export-warning">
      <Icon icon="mdi:alert-outline" />
      <span>
        Sensitive: the file contains your JWT token in plain text (and, for
        admin, passwords). Store it somewhere safe and avoid sharing it.
      </span>
    </p>

    <div class="config-export-controls">
      <label class="config-export-label" for="config-export-format">Format</label>
      <ElSelect id="config-export-format" v-model="exportFormat" class="config-export-select">
        <ElOption label="Markdown (.md)" value="md" />
        <ElOption label="JSON (.json)" value="json" />
        <ElOption label="Env file (.env)" value="env" />
      </ElSelect>
      <ElButton
        type="primary"
        :loading="downloading"
        :disabled="downloading"
        @click="handleDownload"
      >
        <Icon icon="mdi:download" />
        Download
      </ElButton>
    </div>

    <div v-if="error" class="config-export-error">
      <Icon icon="mdi:close-circle-outline" />
      <span>{{ error }}</span>
    </div>
  </div>
</template>

<style scoped>
.config-export-description {
  font-size: 0.875rem;
  color: var(--text-secondary);
  margin: 0 0 8px 0;
  line-height: 1.5;
}
.config-export-description code {
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  font-size: 0.9em;
  background: var(--hover-bg);
  padding: 1px 4px;
  border-radius: 3px;
}
.config-export-warning {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 0.85rem;
  color: var(--el-color-warning);
  margin: 0 0 12px 0;
  line-height: 1.5;
}

.config-export-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.config-export-label {
  font-size: 0.875rem;
  color: var(--text-secondary);
}
.config-export-select { width: 200px; }

.config-export-error {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.85rem;
  color: var(--el-color-danger);
  margin-top: 10px;
}
</style>
