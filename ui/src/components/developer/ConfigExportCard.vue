<script setup lang="ts">
/**
 * Re-download the Setup Wizard's configuration file.
 *
 * The wizard offers that file exactly once, on its last step; losing it
 * afterwards used to mean digging the JWT, the paths, the database
 * credentials and the VNC password back out of the server by hand. Every
 * input still exists post-setup, just spread across four endpoints and the
 * channels store, so this card gathers them and runs the same assembler the
 * wizard runs — the file a user re-downloads here is the file they were
 * given at setup, refreshed.
 */
import { computed, ref } from 'vue';
import { ElButton, ElCard, ElMessage, ElOption, ElSelect } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../../stores/settings';
import { useChannelsStore } from '../../stores/channels';
import {
  fetchInstallSecrets,
  fetchSystemEnvironment,
  getEmbeddingConfig,
  getServerConfig,
} from '../../services/configApi';
import {
  assembleConfigSnapshot,
  downloadConfigExport,
  type ConfigSnapshotSources,
  type ExportFormat,
} from '../../utils/configExport';
import { jwtExpiry } from '../../utils/jwt';

const settingsStore = useSettingsStore();
const channelsStore = useChannelsStore();

const exportFormat = ref<ExportFormat>('md');
const downloading = ref(false);
const error = ref<string | null>(null);

const profile = computed(() => settingsStore.profileId);

/** The endpoints behind this card are admin-only. Saying so beats echoing a
 *  bare "Forbidden", which reads like a bug rather than a permission. */
function describeFailure(message: string): string {
  if (/forbidden|unauthor|not authorized|admin/i.test(message)) {
    return `${message} — the configuration file is admin-only. Sign in with an `
      + 'administrator profile to download it.';
  }
  return message;
}

/** Narrow a free-form server string to one of the export's known values.
 *  A backend that grows a new deployment kind must not break the download. */
function pick<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return allowed.find((a) => a === value) ?? fallback;
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
    // All four are independent reads; serialising them would triple the
    // wait for nothing. ``fetchInstallSecrets`` resolves to an
    // ``available: false`` stub rather than rejecting, so a native install
    // with no Docker secrets still produces a file.
    const [env, secrets, server, embedding] = await Promise.all([
      fetchSystemEnvironment(agentUrl, token),
      fetchInstallSecrets(agentUrl, token),
      getServerConfig(agentUrl, token),
      getEmbeddingConfig(agentUrl, token),
    ]);
    if (channelsStore.channels.length === 0) {
      await channelsStore.loadChannels();
    }

    const sources: ConfigSnapshotSources = {
      profile: profile.value,
      token,
      // The wizard is handed an expiry by the setup response; here the JWT
      // in hand is the only source, and its ``exp`` claim is public data.
      tokenExpiresAt: jwtExpiry(token),
      agentUrl,
      // Only the wizard can be mid-pivot to an HTTPS origin that isn't
      // serving yet; by the time this page loads, the address in the store
      // is the one that answered it.
      agentUrlPendingHttps: false,
      generatedAt: new Date().toISOString(),
      installDeployment: pick(
        env.deployment, ['local', 'server', 'custom', 'kubernetes'] as const, 'local',
      ),
      installMode: pick(
        env.install_mode, ['docker', 'native', 'kubernetes'] as const, 'native',
      ),
      installCustomValues: env.deployment_custom_fields ?? {},
      installSecrets: secrets,
      serverConfig: {
        ...server.config,
        // ``db_provider`` and ``system_dir`` are bootstrap-only keys that
        // /api/config/server deliberately never returns. Install-secrets
        // knows the first, the environment endpoint knows the second.
        db_provider: secrets.db_provider ?? server.config.db_provider,
        system_dir: server.config.system_dir ?? env.system_dir ?? undefined,
      },
      embeddingConfig: embedding.config,
      channels: channelsStore.channels.map((ch) => ({
        type: ch.channel_type,
        mode: ch.mode,
        id: ch.id,
      })),
    };
    downloadConfigExport(exportFormat.value, assembleConfigSnapshot(sources), profile.value);
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
  <ElCard class="config-export-card" shadow="never">
    <template #header>
      <div class="config-export-header">
        <Icon icon="mdi:file-key-outline" class="config-export-icon" />
        <span>Configuration File</span>
      </div>
    </template>

    <p class="config-export-description">
      Re-download the configuration file the Setup Wizard produced for
      <strong>{{ profile }}</strong> — the JWT token, agent URL, project paths,
      database and vector-store parameters, embedding settings, channels, the
      VNC password on desktop container installs, and on Kubernetes the
      namespace, Helm release and Deployment/Service plus the
      <code>kubectl port-forward</code> command that reconnects to them.
    </p>
    <p class="config-export-warning">
      <Icon icon="mdi:alert-outline" />
      <span>
        Sensitive: the file contains your JWT token and passwords in plain text.
        Store it somewhere safe and avoid sharing it.
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
  </ElCard>
</template>

<style scoped>
.config-export-card {
  background: var(--card-bg, var(--bg-color));
  margin-bottom: 16px;
}
.config-export-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text-primary);
}
.config-export-icon { font-size: 20px; color: var(--primary-color); }

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
