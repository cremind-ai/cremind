<script setup lang="ts">
// Settings → HTTPS & Certificate.
//
// The Setup Wizard's "Secure this install" step is deliberately skippable, and
// people skip it — they close the tab before downloading the CA, or never run
// the trust command. Without this page the only way back to that step is to
// re-run setup, so a skipped step turns into a permanent browser warning.
// Same panel, same endpoints, reachable at any time.
//
// Admin-only: the fingerprint the trust POST must echo comes from
// ``/api/services/capabilities``, which is ``require_admin`` once setup is
// complete. A non-admin profile could never obtain it, so the card is hidden
// and the route redirects (see SettingsPage.vue / router/index.ts).
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { fetchServiceCapabilities, type TlsStatus } from '../services/configApi';
import CaTrustPanel from '../components/shared/CaTrustPanel.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const loading = ref(true);
const loadError = ref<string | null>(null);
const tls = ref<TlsStatus | null>(null);
const installMode = ref<string | null>(null);

/** No TLS on this install at all — nothing to download, nothing to trust.
 *  Also the shape an older server (no ``tls`` block) and Electron produce. */
const tlsDisabled = computed(() => !tls.value || tls.value.mode === '');
/** TLS is configured but the CA has not been generated yet: that happens on
 *  the first boot that actually binds TLS. */
const caMissing = computed(
  () => !tlsDisabled.value && !tls.value?.ca_sha256,
);

const statusLine = computed(() => {
  const t = tls.value;
  if (!t) return null;
  if (t.serving_https) {
    return t.https_url
      ? `This install serves HTTPS at ${t.https_url}.`
      : 'This install is serving HTTPS.';
  }
  if (t.pending_https) {
    return 'HTTPS starts the next time the server restarts.';
  }
  return null;
});

async function load() {
  loading.value = true;
  loadError.value = null;
  try {
    // Admin-gated post-setup — the token is required or this 401s.
    const caps = await fetchServiceCapabilities(
      settingsStore.agentUrl,
      settingsStore.authToken,
    );
    tls.value = caps.tls ?? null;
    installMode.value = caps.install_mode ?? null;
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Failed to load TLS status';
    ElMessage.error(loadError.value);
  } finally {
    loading.value = false;
  }
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

onMounted(load);
</script>

<template>
  <div class="security-page">
    <div class="security-container">
      <div class="security-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <h1 class="security-title">HTTPS &amp; Certificate</h1>
        <p class="security-subtitle">
          Trust this install's private certificate authority on a device, so
          the browser stops warning about the connection.
        </p>
      </div>

      <div v-if="loading" class="loading">Loading…</div>

      <div v-else-if="loadError" class="section-card">
        <h3 class="section-title">Could not read the TLS status</h3>
        <p class="empty-text">{{ loadError }}</p>
        <button type="button" class="retry-btn" @click="load">
          <Icon icon="mdi:refresh" /> Try again
        </button>
      </div>

      <!-- 1. TLS switched off for this install. -->
      <div v-else-if="tlsDisabled" class="section-card">
        <h3 class="section-title">HTTPS is not enabled on this install</h3>
        <p class="empty-text">
          Cremind is serving plain HTTP, so there is no certificate authority to
          download or trust. To turn HTTPS on, re-run the installer with
          <code>--ssl auto</code> (or <code>-Ssl auto</code> on Windows).
        </p>
      </div>

      <!-- 2. TLS on, but the CA has not been generated yet. -->
      <div v-else-if="caMissing" class="section-card">
        <h3 class="section-title">No certificate authority yet</h3>
        <p class="empty-text">
          The certificate authority is generated the first time the server
          starts with TLS enabled. Restart Cremind, then reload this page.
        </p>
      </div>

      <!-- 3. The real thing. -->
      <div v-else class="section-card">
        <p v-if="statusLine" class="status-line">
          <Icon icon="mdi:lock-outline" />
          {{ statusLine }}
        </p>
        <CaTrustPanel
          variant="settings"
          :agent-url="settingsStore.agentUrl"
          :tls="tls"
          :install-mode="installMode"
          :auth-token="settingsStore.authToken"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.security-page {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}
.security-container { max-width: 880px; margin: 0 auto; }
.security-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.security-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.security-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }

.loading {
  display: flex; align-items: center; justify-content: center;
  padding: 60px 0; color: var(--text-secondary);
}

.section-card {
  background: var(--surface-color);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 20px 24px;
}
.section-title {
  font-size: 1rem; font-weight: 600; color: var(--text-primary);
  margin: 0 0 8px 0;
}
.empty-text {
  color: var(--text-secondary); font-size: 0.875rem;
  margin: 0; line-height: 1.6;
}
.empty-text code {
  background: var(--hover-bg); padding: 1px 5px;
  border-radius: 3px; font-size: 0.8rem;
}
.status-line {
  display: flex; align-items: center; gap: 6px;
  margin: 0 0 20px 0; padding-bottom: 16px;
  border-bottom: 1px solid var(--border-color);
  color: var(--text-secondary); font-size: 0.825rem;
}
.retry-btn {
  display: inline-flex; align-items: center; gap: 6px;
  margin-top: 14px; padding: 7px 14px;
  border: 1px solid var(--border-color); border-radius: 4px;
  background: none; color: var(--text-primary);
  font-size: 0.825rem; cursor: pointer;
}
.retry-btn:hover { border-color: var(--primary-color); color: var(--primary-color); }
</style>
