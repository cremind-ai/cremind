<script setup lang="ts">
/**
 * "Install Cremind Connect": in the desktop app, its bundled copy (one click,
 * through the optional `window.cremind.connect` bridge); in a browser, the
 * installer for this computer's system from GET /api/tags/connect, with the
 * other systems one click away.
 */
import { computed, onMounted, ref } from 'vue';
import { ElButton } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsSetupStore } from '../../../stores/tagsSetup';
import { connectBridge } from '../../../services/connectBridge';
import {
  detectClientPlatform, installerChoices, type ClientPlatform,
} from '../../../utils/tagsSetupFormat';

const emit = defineEmits<{ (e: 'installed'): void }>();

const store = useTagsSetupStore();
const bridge = connectBridge();
const loading = ref(false);
const loadError = ref('');
const installing = ref(false);
const installError = ref('');
const showOthers = ref(false);
const platform = ref<ClientPlatform>(detectClientPlatform(typeof navigator === 'undefined' ? null : navigator));

const choices = computed(() => installerChoices(store.downloads, platform.value));
const version = computed(() => store.downloads?.latest_version || '');
const OS_NAME: Record<string, string> = { windows: 'Windows', macos: 'macOS', linux: 'Linux' };
const osName = computed(() => OS_NAME[platform.value.os] ?? '');
const offerText = computed(() => {
  const what = ['Install Cremind Connect', version.value].filter(Boolean).join(' ');
  const where = osName.value && choices.value.recommended.length ? ` for ${osName.value}` : '';
  return `${what}${where}, then open it once:`;
});

async function refineArchitecture() {
  // Chromium can say which chip a Mac has; Safari and Firefox cannot.
  const uad = (navigator as unknown as {
    userAgentData?: { getHighEntropyValues?: (hints: string[]) => Promise<{ architecture?: string }> };
  }).userAgentData;
  if (platform.value.os !== 'macos' || !uad?.getHighEntropyValues) return;
  try {
    const { architecture } = await uad.getHighEntropyValues(['architecture']);
    if (architecture === 'arm') platform.value = { ...platform.value, arch: 'arm64' };
    else if (architecture === 'x86') platform.value = { ...platform.value, arch: 'x64' };
  } catch {
    /* keep both Mac installers on offer */
  }
}

async function load() {
  if (bridge) return;
  loading.value = true;
  loadError.value = '';
  try {
    await Promise.all([store.loadDownloads(), refineArchitecture()]);
  } catch {
    loadError.value = 'The download links could not be loaded. Try again in a moment.';
  } finally {
    loading.value = false;
  }
}

async function installBundled() {
  if (!bridge) return;
  installing.value = true;
  installError.value = '';
  try {
    const res = await bridge.install();
    if (res.ok) emit('installed');
    else installError.value = res.error || 'Cremind Connect could not be installed.';
  } catch (e) {
    installError.value = e instanceof Error ? e.message : 'Cremind Connect could not be installed.';
  } finally {
    installing.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="install-offer">
    <template v-if="bridge">
      <p class="offer-text">Cremind Connect comes with this app. Install it on this computer:</p>
      <div class="offer-actions">
        <ElButton type="primary" :loading="installing" @click="installBundled">
          <Icon v-if="!installing" icon="mdi:download" class="btn-icon" /> Install Cremind Connect
        </ElButton>
      </div>
      <p v-if="installError" class="offer-error" role="alert">{{ installError }}</p>
    </template>

    <template v-else>
      <p v-if="loading" class="offer-text">Finding the right installer…</p>
      <p v-else-if="loadError" class="offer-error" role="alert">
        {{ loadError }}
        <ElButton text size="small" @click="load">Retry</ElButton>
      </p>
      <template v-else>
        <p class="offer-text">{{ offerText }}</p>
        <div v-if="choices.recommended.length" class="offer-actions">
          <a
            v-for="(c, i) in choices.recommended"
            :key="c.key"
            :href="c.url"
            class="download-link"
            :class="{ primary: i === 0 }"
            target="_blank"
            rel="noopener"
          >
            <Icon icon="mdi:download" aria-hidden="true" /> Download for {{ c.label }}
          </a>
        </div>
        <p v-if="platform.os === 'macos' && choices.recommended.length > 1" class="offer-hint">
          Apple silicon is for Macs with an M1 chip or later (most Macs from 2021 on); Intel is for older Macs.
        </p>
        <p v-if="!choices.recommended.length && !choices.others.length" class="offer-text">
          No installer is available from this server yet.
        </p>
        <button
          v-if="choices.others.length"
          type="button"
          class="others-toggle"
          :aria-expanded="showOthers"
          @click="showOthers = !showOthers"
        >
          <Icon :icon="showOthers ? 'mdi:chevron-down' : 'mdi:chevron-right'" aria-hidden="true" />
          {{ choices.recommended.length ? 'Other systems' : 'Choose your system' }}
        </button>
        <ul v-if="showOthers" class="others">
          <li v-for="c in choices.others" :key="c.key">
            <a :href="c.url" target="_blank" rel="noopener">{{ c.label }}</a>
          </li>
        </ul>
      </template>
    </template>
  </div>
</template>

<style scoped>
.install-offer { display: flex; flex-direction: column; gap: 8px; }
.offer-text { margin: 0; font-size: 0.88rem; color: var(--text-primary); line-height: 1.5; }
.offer-hint { margin: 0; font-size: 0.78rem; color: var(--text-tertiary); line-height: 1.45; }
.offer-error { margin: 0; font-size: 0.85rem; color: var(--el-color-danger); }
.offer-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.btn-icon { margin-right: 6px; }
.download-link {
  display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px;
  border-radius: 6px; border: 1px solid var(--border-color); font-size: 0.875rem;
  color: var(--text-primary); background: var(--surface-color); text-decoration: none;
}
.download-link:hover, .download-link:focus-visible { border-color: var(--primary-color); color: var(--primary-color); }
.download-link.primary { background: var(--primary-color); border-color: var(--primary-color); color: #fff; }
.download-link.primary:hover, .download-link.primary:focus-visible { filter: brightness(1.08); color: #fff; }
.others-toggle {
  display: inline-flex; align-items: center; gap: 4px; align-self: flex-start; padding: 2px 0;
  background: none; border: none; cursor: pointer; color: var(--text-secondary); font-size: 0.82rem;
}
.others-toggle:hover { color: var(--primary-color); }
.others { margin: 0; padding-left: 22px; font-size: 0.85rem; line-height: 1.7; }
.others a { color: var(--primary-color); }
</style>
