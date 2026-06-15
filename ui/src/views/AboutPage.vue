<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import { ElCard, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const APP_NAME =
  typeof window !== 'undefined' && !!window.cremind
    ? 'Cremind App'
    : 'Cremind Web UI';
const UI_VERSION =
  typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '';

const GITHUB_URL = 'https://github.com/cremind-ai/cremind';
const ISSUES_URL = 'https://github.com/cremind-ai/cremind/issues';
const LICENSE_URL = 'https://github.com/cremind-ai/cremind/blob/main/LICENSE';

const PACKAGE_NAME = 'cremind';

type Channel = 'production' | 'test' | 'dev';

interface BackendVersion {
  backend: string;
  schema: string;
  min_supported_upgrade_from: string;
  channel?: Channel;
}

const backendInfo = ref<BackendVersion | null>(null);
const backendError = ref(false);

const channel = computed<Channel | null>(() => backendInfo.value?.channel ?? null);

const channelLabel = computed(() => {
  switch (channel.value) {
    case 'production': return 'Production';
    case 'test': return 'Test';
    case 'dev': return 'Development';
    default: return 'Unknown';
  }
});

const channelTone = computed<'success' | 'warning' | 'info'>(() => {
  switch (channel.value) {
    case 'production': return 'success';
    case 'test': return 'warning';
    default: return 'info';
  }
});

// PyPI page for this specific version. Test builds live on test.pypi.org;
// production builds on pypi.org. Dev installs come from a local source
// checkout, so there is no upstream page to link to.
const pypiUrl = computed<string | null>(() => {
  const version = backendInfo.value?.backend;
  if (!version) return null;
  if (channel.value === 'production') {
    return `https://pypi.org/project/${PACKAGE_NAME}/${version}/`;
  }
  if (channel.value === 'test') {
    return `https://test.pypi.org/project/${PACKAGE_NAME}/${version}/`;
  }
  return null;
});

const pypiIndexLabel = computed(() => {
  switch (channel.value) {
    case 'production': return 'pypi.org';
    case 'test': return 'test.pypi.org';
    case 'dev': return 'Editable install (local source)';
    default: return 'Unknown';
  }
});

async function loadBackendVersion() {
  const base = settingsStore.agentUrl;
  if (!base) {
    backendError.value = true;
    return;
  }
  try {
    const r = await fetch(`${base}/version`);
    if (!r.ok) {
      backendError.value = true;
      return;
    }
    backendInfo.value = await r.json();
  } catch {
    backendError.value = true;
  }
}

onMounted(() => {
  void loadBackendVersion();
});

function goBack() {
  goBackToChat(router, props.profile);
}
</script>

<template>
  <div class="about-page">
    <div class="about-container">
      <div class="about-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Chat
        </button>
        <h1 class="about-title">About</h1>
        <p class="about-subtitle">
          Project information, license, version, and links.
        </p>
      </div>

      <!-- Identity -->
      <ElCard class="section-card identity-card" shadow="never">
        <div class="identity">
          <img src="/logo.png" alt="Cremind logo" class="identity-logo" />
          <div class="identity-text">
            <div class="identity-name">{{ APP_NAME }}</div>
            <div class="identity-tagline">
              Personal AI Assistant — server + CLI.
            </div>
          </div>
        </div>
      </ElCard>

      <!-- Version -->
      <ElCard class="section-card" shadow="never">
        <h3 class="section-title">Version</h3>
        <div class="info-row">
          <span class="info-label">UI</span>
          <span class="info-value">{{ UI_VERSION || 'unknown' }}</span>
        </div>
        <div class="info-row">
          <span class="info-label">Backend</span>
          <span class="info-value">
            <template v-if="backendInfo">{{ backendInfo.backend }}</template>
            <span v-else-if="backendError" class="info-muted">Unavailable</span>
            <span v-else class="info-muted">Loading…</span>
          </span>
        </div>
        <div class="info-row">
          <span class="info-label">Database schema</span>
          <span class="info-value mono">
            <template v-if="backendInfo">{{ backendInfo.schema }}</template>
            <span v-else-if="backendError" class="info-muted">Unavailable</span>
            <span v-else class="info-muted">Loading…</span>
          </span>
        </div>
      </ElCard>

      <!-- Distribution / PyPI -->
      <ElCard class="section-card" shadow="never">
        <h3 class="section-title">Distribution</h3>
        <div class="info-row">
          <span class="info-label">Release channel</span>
          <span class="info-value">
            <template v-if="backendInfo">
              <ElTag :type="channelTone" effect="light" round size="small">
                {{ channelLabel }}
              </ElTag>
            </template>
            <span v-else-if="backendError" class="info-muted">Unavailable</span>
            <span v-else class="info-muted">Loading…</span>
          </span>
        </div>
        <div class="info-row">
          <span class="info-label">Package</span>
          <span class="info-value mono">
            <template v-if="backendInfo">
              {{ PACKAGE_NAME }}=={{ backendInfo.backend }}
            </template>
            <span v-else-if="backendError" class="info-muted">Unavailable</span>
            <span v-else class="info-muted">Loading…</span>
          </span>
        </div>
        <div class="info-row">
          <span class="info-label">PyPI</span>
          <span class="info-value">
            <template v-if="pypiUrl">
              <a :href="pypiUrl" target="_blank" rel="noopener" class="link">
                {{ pypiIndexLabel }}
                <Icon icon="mdi:open-in-new" class="link-icon" />
              </a>
            </template>
            <template v-else-if="backendInfo">
              <span class="info-muted">{{ pypiIndexLabel }}</span>
            </template>
            <span v-else-if="backendError" class="info-muted">Unavailable</span>
            <span v-else class="info-muted">Loading…</span>
          </span>
        </div>
      </ElCard>

      <!-- Project info -->
      <ElCard class="section-card" shadow="never">
        <h3 class="section-title">Project</h3>
        <div class="info-row">
          <span class="info-label">License</span>
          <span class="info-value">
            <a :href="LICENSE_URL" target="_blank" rel="noopener" class="link">
              MIT
              <Icon icon="mdi:open-in-new" class="link-icon" />
            </a>
          </span>
        </div>
        <div class="info-row">
          <span class="info-label">Author</span>
          <span class="info-value">Cremind contributors</span>
        </div>
        <div class="info-row">
          <span class="info-label">Copyright</span>
          <span class="info-value">© 2026 cremind</span>
        </div>
      </ElCard>

      <!-- Links -->
      <ElCard class="section-card" shadow="never">
        <h3 class="section-title">Links</h3>
        <a :href="GITHUB_URL" target="_blank" rel="noopener" class="link-row">
          <Icon icon="mdi:github" class="link-row-icon" />
          <div class="link-row-text">
            <div class="link-row-title">GitHub</div>
            <div class="link-row-url">{{ GITHUB_URL }}</div>
          </div>
          <Icon icon="mdi:open-in-new" class="link-row-chevron" />
        </a>
        <a :href="ISSUES_URL" target="_blank" rel="noopener" class="link-row">
          <Icon icon="mdi:bug-outline" class="link-row-icon" />
          <div class="link-row-text">
            <div class="link-row-title">Report an issue</div>
            <div class="link-row-url">{{ ISSUES_URL }}</div>
          </div>
          <Icon icon="mdi:open-in-new" class="link-row-chevron" />
        </a>
      </ElCard>
    </div>
  </div>
</template>

<style scoped>
.about-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.about-container { max-width: 720px; margin: 0 auto; }
.about-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.about-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.about-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }

.section-card { margin-bottom: 12px; background: var(--surface-color); }
.section-title { font-size: 1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 12px 0; }

.identity { display: flex; align-items: center; gap: 16px; }
.identity-logo { width: 64px; height: 64px; border-radius: 12px; flex-shrink: 0; }
.identity-text { flex: 1; min-width: 0; }
.identity-name { font-size: 1.125rem; font-weight: 700; color: var(--text-primary); }
.identity-tagline { font-size: 0.875rem; color: var(--text-secondary); margin-top: 4px; }

.info-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 10px 0; border-bottom: 1px solid var(--border-color);
}
.info-row:last-child { border-bottom: none; }
.info-label { font-size: 0.875rem; color: var(--text-secondary); }
.info-value { font-size: 0.875rem; color: var(--text-primary); font-weight: 500; text-align: right; }
.info-value.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.8rem; }
.info-muted { color: var(--text-tertiary); font-weight: 400; font-style: italic; }

.link {
  display: inline-flex; align-items: center; gap: 4px;
  color: var(--primary-color); text-decoration: none;
}
.link:hover { text-decoration: underline; }
.link-icon { font-size: 14px; }

.link-row {
  display: flex; align-items: center; gap: 14px;
  padding: 12px 0; border-bottom: 1px solid var(--border-color);
  text-decoration: none; color: inherit;
}
.link-row:last-child { border-bottom: none; }
.link-row:hover .link-row-title { color: var(--primary-color); }
.link-row-icon { font-size: 22px; color: var(--text-tertiary); flex-shrink: 0; }
.link-row-text { flex: 1; min-width: 0; }
.link-row-title { font-size: 0.9rem; font-weight: 500; color: var(--text-primary); transition: color 0.2s; }
.link-row-url {
  font-size: 0.8rem; color: var(--text-tertiary); margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.link-row-chevron { font-size: 16px; color: var(--text-tertiary); flex-shrink: 0; }
</style>
