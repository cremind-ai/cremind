<script setup lang="ts">
import { ref, onMounted, computed, nextTick, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useSettingsStore } from '../stores/settings';
import {
  getUserConfigSchema,
  getUserConfig,
  updateUserConfig,
  resetUserConfigKey,
  type UserConfigSchema,
} from '../services/configApi';
import { stableStringify } from '../composables/useUnsavedChanges';
import ConfigGroupCard from '../components/config/ConfigGroupCard.vue';
import SettingsSaveBar from '../components/shared/SettingsSaveBar.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const route = useRoute();
const settingsStore = useSettingsStore();

// Deep-link target: callers can navigate here with ``?section=<group>`` (e.g.
// the conversation memory panel's settings icon → ``?section=memory``) to
// scroll the matching group into view and briefly highlight it.
const groupEls = new Map<string, HTMLElement>();
const highlightedSection = ref<string | null>(null);
let highlightTimer: ReturnType<typeof setTimeout> | null = null;

function setGroupRef(key: string, el: Element | null) {
  if (el instanceof HTMLElement) groupEls.set(key, el);
  else groupEls.delete(key);
}

async function scrollToSection(section: string | null) {
  if (!section) return;
  await nextTick();
  const el = groupEls.get(section);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  highlightedSection.value = section;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => { highlightedSection.value = null; }, 2000);
}

const schema = ref<UserConfigSchema | null>(null);
/** The groups this page edits: one with a page of its own (Appearance) is
 *  left to that page. */
const cardGroups = computed<UserConfigSchema['groups']>(() => Object.fromEntries(
  Object.entries(schema.value?.groups ?? {}).filter(([, group]) => !group.page),
));
const values = ref<Record<string, unknown>>({});
const defaults = ref<Record<string, unknown>>({});
/** The values as last loaded (or reset): what an edit is a change from. */
const savedValues = ref<Record<string, unknown>>({});
const loading = ref(false);
const saving = ref(false);

/** What a field shows: its override, or the default without one. */
function effective(source: Record<string, unknown>, key: string): unknown {
  const v = source[key];
  return v === null || v === undefined ? defaults.value[key] : v;
}

/** Keys whose shown value differs from the saved one — a value typed back
 *  (the default included) is not a change. */
const changedKeys = computed(() =>
  Object.keys(values.value).filter(
    key => stableStringify(effective(values.value, key)) !== stableStringify(effective(savedValues.value, key)),
  ));
const hasChanges = computed(() => changedKeys.value.length > 0);

async function loadAll() {
  loading.value = true;
  try {
    const [schemaRes, valuesRes] = await Promise.all([
      getUserConfigSchema(settingsStore.agentUrl, settingsStore.authToken),
      getUserConfig(settingsStore.agentUrl, settingsStore.authToken),
    ]);
    schema.value = schemaRes;
    values.value = valuesRes.values;
    savedValues.value = { ...valuesRes.values };
    defaults.value = valuesRes.defaults;
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load configuration');
  } finally {
    loading.value = false;
  }
}

function handleUpdate(key: string, value: unknown) {
  // Held locally until the save bar sends every change in one batch.
  values.value = { ...values.value, [key]: value };
}

function handleDiscard() {
  values.value = { ...savedValues.value };
}

async function handleReset(key: string) {
  try {
    await ElMessageBox.confirm(
      `Reset "${key}" to its default value?`,
      'Reset value',
      { type: 'warning', confirmButtonText: 'Reset', cancelButtonText: 'Cancel' },
    );
  } catch {
    return;
  }
  try {
    await resetUserConfigKey(
      settingsStore.agentUrl,
      settingsStore.authToken,
      key,
    );
    // The reset is saved already: clear the override and any pending edit of
    // this key, keeping the other pending edits.
    values.value = { ...values.value, [key]: null };
    savedValues.value = { ...savedValues.value, [key]: null };
    ElMessage.success('Reset to default');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to reset value');
  }
}

async function handleSave() {
  if (!hasChanges.value) return;
  saving.value = true;
  try {
    await updateUserConfig(
      settingsStore.agentUrl,
      settingsStore.authToken,
      Object.fromEntries(changedKeys.value.map(key => [key, values.value[key]])),
    );
    ElMessage.success('Configuration saved');
    savedValues.value = { ...values.value };
    await loadAll();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save configuration');
  } finally {
    saving.value = false;
  }
}

function goBack() {
  router.push(`/${props.profile}/settings`);
}

function currentSection(): string | null {
  const s = route.query.section;
  return typeof s === 'string' && s ? s : null;
}

onMounted(async () => {
  await loadAll();
  await scrollToSection(currentSection());
});

// Re-scroll if the section query changes while the page stays mounted.
watch(() => route.query.section, (s) => {
  if (typeof s === 'string' && s) scrollToSection(s);
});
</script>

<template>
  <div class="config-page">
    <div class="config-container">
      <div class="config-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <h1 class="config-title">Config</h1>
        <p class="config-subtitle">
          Per-profile runtime tuning · Profile <strong>{{ profile }}</strong>
        </p>
      </div>

      <div v-if="loading" class="loading">Loading…</div>
      <div v-else-if="schema">
        <div
          v-for="(group, groupKey) in cardGroups"
          :key="groupKey"
          :ref="(el) => setGroupRef(String(groupKey), el as Element | null)"
          class="config-group-anchor"
          :class="{ 'config-group-highlight': highlightedSection === groupKey }"
        >
          <ConfigGroupCard
            :group-key="groupKey"
            :group="group"
            :values="values"
            :defaults="defaults"
            @update:value="handleUpdate"
            @reset="handleReset"
          />
        </div>
        <SettingsSaveBar
          :dirty="hasChanges"
          :saving="saving"
          @save="handleSave"
          @discard="handleDiscard"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.config-page {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-color);
  padding: 24px;
  box-sizing: border-box;
}
.config-container { max-width: 880px; margin: 0 auto; }
.config-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.config-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.config-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.loading {
  display: flex; align-items: center; justify-content: center;
  padding: 60px 0; color: var(--text-secondary);
}
.config-group-anchor {
  /* Keep a little breathing room when scrolled to via ?section=. */
  scroll-margin-top: 16px;
  border-radius: 8px;
  transition: box-shadow 0.3s ease, background-color 0.3s ease;
}
.config-group-highlight {
  box-shadow: 0 0 0 2px var(--primary-color);
}
</style>
