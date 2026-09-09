<script setup lang="ts">
/**
 * "Which Cremind am I actually running?" — the facts a support answer needs
 * (release channel, Docker vs native vs Kubernetes, VNC, where the data
 * lives) read straight off the server rather than guessed from the URL.
 * The same endpoint feeds `cremind server environment` and the agent's own
 * system prompt, so the page, the CLI and the chat answer cannot drift.
 */
import { computed, onMounted, ref } from 'vue';
import { ElButton, ElCard, ElMessage, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../../stores/settings';
import { fetchSystemEnvironment, type SystemEnvironment } from '../../services/configApi';

const settingsStore = useSettingsStore();

const env = ref<SystemEnvironment | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);

/** Everything here is best-effort: an older backend can omit any field, and
 *  a card that renders "unknown" is far better than one that renders
 *  "undefined" or throws on a missing key. */
function text(value: unknown): string {
  if (value === null || value === undefined) return 'unknown';
  const s = String(value).trim();
  return s === '' ? 'unknown' : s;
}

function yesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return 'unknown';
  return value ? 'Yes' : 'No';
}

const channelTagType = computed<'success' | 'info' | 'warning'>(() => {
  switch (env.value?.release_channel) {
    case 'dev': return 'warning';
    case 'test': return 'info';
    default: return 'success';
  }
});

// "docker (desktop image)" reads better than two separate rows, and the
// flavour is what decides whether a desktop exists at all.
const installLine = computed(() => {
  const mode = text(env.value?.install_mode);
  const flavor = env.value?.image_flavor;
  return flavor ? `${mode} (${flavor} image)` : mode;
});

const vncLine = computed(() => {
  const on = env.value?.vnc_enabled;
  if (on === null || on === undefined) return 'unknown';
  return on ? 'Enabled' : 'Disabled';
});

// Which cluster objects this pod is. The whole block is null off Kubernetes —
// presence is what puts the rows on the card, so there is no second flag to
// keep in step with the backend.
const k8s = computed(() => env.value?.kubernetes ?? null);

// The chart states these; a chart too old to state them leaves the app reading
// the pod name and the service-account namespace file, which cannot recover
// the Helm release. Flagging that is the difference between a name the reader
// can paste into `helm upgrade` and one they must not.
const k8sInferred = computed(() => k8s.value?.source === 'inferred');

// Deployment and Service are the same object name by chart construction, so
// one row beats two identical ones — and it stays honest if they ever diverge.
const k8sWorkloadLine = computed(() => {
  const workload = k8s.value?.workload;
  const service = k8s.value?.service;
  if (!workload && !service) return 'unknown';
  if (workload && service && workload !== service) return `${workload} / ${service}`;
  return text(workload ?? service);
});

const osLine = computed(() => {
  const os = text(env.value?.os);
  const release = env.value?.os_release;
  return release ? `${os} ${release}` : os;
});

// The resolved zone is the one that answers "when will my 09:00 schedule
// run?", so it is the row. The CREMIND_TIMEZONE boot default is worth showing
// only when it disagrees with it — on most installs it is blank, and a blank
// row here used to read as "no timezone configured" while the scheduler was
// happily firing in whatever the admin had set on the Config page.
const timezoneLine = computed(() => text(env.value?.effective_timezone));

const bootTimezoneLine = computed(() => {
  const boot = (env.value?.boot_timezone ?? '').trim();
  if (!boot || boot === (env.value?.effective_timezone ?? '').trim()) return '';
  return boot;
});

async function load(userInitiated = false) {
  loading.value = true;
  error.value = null;
  try {
    env.value = await fetchSystemEnvironment(
      settingsStore.agentUrl,
      settingsStore.authToken,
    );
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    error.value = message;
    // A failed page load is reported inline — the card is one of several and
    // a toast on every arrival would be noise. A retry the user asked for
    // gets a toast, because nothing else acknowledges the click.
    if (userInitiated) ElMessage.error(message);
  } finally {
    loading.value = false;
  }
}

onMounted(() => { void load(); });
</script>

<template>
  <ElCard class="environment-card" shadow="never">
    <template #header>
      <div class="environment-card-header">
        <div class="environment-card-title">
          <Icon icon="mdi:server-outline" class="environment-card-icon" />
          <span>Environment</span>
          <ElTag
            v-if="env?.release_channel"
            :type="channelTagType"
            size="small"
            effect="plain"
          >{{ env.release_channel }}</ElTag>
        </div>
        <ElButton
          v-if="error"
          size="small"
          :loading="loading"
          @click="load(true)"
        >
          <Icon icon="mdi:refresh" />
          Retry
        </ElButton>
      </div>
    </template>

    <div v-if="loading && !env" class="environment-note">Loading environment…</div>
    <div v-else-if="error && !env" class="environment-note environment-note-error">
      <Icon icon="mdi:alert-circle-outline" />
      <span>{{ error }}</span>
    </div>
    <template v-else>
      <dl class="environment-grid">
        <dt>Release channel</dt>
        <dd>
          <ElTag :type="channelTagType" size="small" effect="plain">
            {{ text(env?.release_channel) }}
          </ElTag>
        </dd>

        <dt>Deployment</dt>
        <dd>{{ text(env?.deployment) }}</dd>

        <dt>Install</dt>
        <dd>{{ installLine }}</dd>

        <dt>VNC desktop</dt>
        <dd>{{ vncLine }}</dd>

        <dt>Supervised</dt>
        <dd>{{ yesNo(env?.supervised) }}</dd>

        <dt>Electron</dt>
        <dd>{{ yesNo(env?.electron) }}</dd>

        <dt>OS</dt>
        <dd>{{ osLine }}</dd>

        <dt>Python</dt>
        <dd>{{ text(env?.python_version) }}</dd>

        <dt>Backend version</dt>
        <dd>{{ text(env?.backend_version) }}</dd>

        <dt>App URL</dt>
        <dd class="environment-mono">{{ text(env?.app_url) }}</dd>

        <dt>System directory</dt>
        <dd class="environment-mono">{{ text(env?.system_dir) }}</dd>

        <dt>Install directory</dt>
        <dd class="environment-mono">{{ text(env?.install_dir) }}</dd>

        <template v-if="k8s">
          <dt>Namespace</dt>
          <dd>{{ text(k8s.namespace) }}</dd>

          <dt>Helm release</dt>
          <dd>
            {{ text(k8s.release) }}
            <span v-if="k8sInferred && !k8s.release" class="environment-hint">
              (this chart does not state it — <code>helm list --all-namespaces</code>)
            </span>
          </dd>

          <dt>Deployment / Service</dt>
          <dd>
            {{ k8sWorkloadLine }}
            <span v-if="k8sInferred" class="environment-hint">(inferred from the pod name)</span>
          </dd>

          <template v-if="k8s.port_forward">
            <dt>Port-forward</dt>
            <dd class="environment-mono">{{ k8s.port_forward }}</dd>
          </template>
        </template>

        <dt>Timezone</dt>
        <dd>{{ timezoneLine }}</dd>

        <template v-if="bootTimezoneLine">
          <dt>Boot timezone</dt>
          <dd>{{ bootTimezoneLine }} <span class="environment-hint">(CREMIND_TIMEZONE default)</span></dd>
        </template>
      </dl>
      <div v-if="error" class="environment-note environment-note-error">
        <Icon icon="mdi:alert-circle-outline" />
        <span>Could not refresh: {{ error }}</span>
      </div>
    </template>
  </ElCard>
</template>

<style scoped>
.environment-card {
  background: var(--card-bg, var(--bg-color));
  margin-bottom: 16px;
}
.environment-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.environment-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  color: var(--text-primary);
}
.environment-card-icon { font-size: 20px; color: var(--primary-color); }

.environment-grid {
  display: grid;
  grid-template-columns: minmax(150px, max-content) 1fr;
  gap: 6px 20px;
  margin: 0;
  font-size: 0.875rem;
  align-items: baseline;
}
.environment-grid dt {
  color: var(--text-secondary);
}
.environment-grid dd {
  margin: 0;
  color: var(--text-primary);
  word-break: break-word;
}
.environment-mono {
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  font-size: 0.82rem;
}
.environment-hint {
  color: var(--text-secondary);
  font-size: 0.8rem;
}
.environment-hint code {
  font-family: var(--font-mono, ui-monospace, Consolas, monospace);
  background: var(--hover-bg);
  padding: 0 4px;
  border-radius: 3px;
}

.environment-note {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.85rem;
  color: var(--text-secondary);
}
.environment-note-error { color: var(--el-color-danger); margin-top: 10px; }
</style>
