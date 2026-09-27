<script setup lang="ts">
/**
 * Settings → Tags → Hardware (admin only; the route guard hides it from other
 * profiles and the server answers 403 regardless). Companions, the full
 * inventory with ownership, hardware operations with their command queue,
 * and the defaults every profile inherits (saved from the bar).
 *
 * Polls GET /api/tags/hardware while visible: every 3 s while a command is
 * queued or running, else every 15 s.
 *
 * CLI counterpart: `cremind tags hardware …`.
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElCard, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../stores/tags';
import { useSettingsStore } from '../stores/settings';
import { listProfiles } from '../services/configApi';
import { useSavedSnapshot } from '../composables/useUnsavedChanges';
import { useNow } from '../composables/useNow';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import type { TagEffectiveOptions } from '../services/tagsApi';
import SettingsSaveBar from '../components/shared/SettingsSaveBar.vue';
import TagCompanionsCard from '../components/tags/TagCompanionsCard.vue';
import TagInventoryCard from '../components/tags/TagInventoryCard.vue';
import TagOperationsCard from '../components/tags/TagOperationsCard.vue';
import TagOptionsForm from '../components/tags/TagOptionsForm.vue';
import {
  TAG_BUILTIN_DEFAULTS, draftFromOptions, draftProblem, isCommandActive, optionsFromDraft,
  type TagOptionsDraft,
} from '../utils/tagsFormat';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const store = useTagsStore();
const settingsStore = useSettingsStore();
const { now } = useNow();

store.reset(props.profile);

const loading = ref(false);
const loadError = ref('');
const profiles = ref<string[]>([]);
const builtin = ref<TagEffectiveOptions>(TAG_BUILTIN_DEFAULTS);
const kinds = ref<string[]>(Object.keys(TAG_BUILTIN_DEFAULTS.routes));
const layouts = ref<string[]>([TAG_BUILTIN_DEFAULTS.layout]);
const defaultsLoaded = ref(false);
const saving = ref(false);

const form = ref<TagOptionsDraft>(draftFromOptions({}, kinds.value));
const snapshot = useSavedSnapshot(() => form.value);
const dirty = computed(() => defaultsLoaded.value && snapshot.dirty.value);
const problem = computed(() => draftProblem(form.value));

const hardware = computed(() => store.hardware);
const companions = computed(() => hardware.value?.companions ?? []);
const devices = computed(() => hardware.value?.devices ?? []);
const commands = computed(() => hardware.value?.commands ?? []);
const anyActive = computed(() => commands.value.some((c) => isCommandActive(c.status)));
const deviceCount = computed(() => {
  const out: Record<string, number> = {};
  for (const d of devices.value) out[d.companion_id] = (out[d.companion_id] ?? 0) + 1;
  return out;
});

async function loadHardware(initial = false) {
  if (!settingsStore.authToken) return;
  if (initial) loading.value = true;
  try {
    await store.loadHardware();
    loadError.value = '';
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Failed to load the hardware inventory';
  } finally {
    loading.value = false;
  }
}

const poll = useVisiblePoll(() => loadHardware(), () => (anyActive.value ? 3000 : 15000));

async function loadDefaults() {
  try {
    const [res, own] = await Promise.all([store.defaults(), store.loadSettings().catch(() => null)]);
    builtin.value = res.builtin;
    if (own) {
      kinds.value = own.routable_kinds;
      layouts.value = own.layouts;
    }
    form.value = draftFromOptions(res.defaults, kinds.value);
    snapshot.commit();
    defaultsLoaded.value = true;
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to load the defaults');
  }
}

async function saveDefaults() {
  if (!dirty.value || problem.value) return;
  saving.value = true;
  try {
    const res = await store.saveDefaults(optionsFromDraft(form.value));
    builtin.value = res.builtin;
    form.value = draftFromOptions(res.defaults, kinds.value);
    snapshot.commit();
    ElMessage.success('Defaults saved — profiles that have not set their own now use them');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save the defaults');
  } finally {
    saving.value = false;
  }
}

function discardDefaults() {
  form.value = snapshot.saved();
}

async function loadAll() {
  await Promise.all([
    loadHardware(true),
    loadDefaults(),
    listProfiles(settingsStore.agentUrl, settingsStore.authToken)
      .then(({ profiles: names }) => { profiles.value = names; })
      .catch(() => { profiles.value = []; }),
  ]);
}

onMounted(loadAll);
watch(() => settingsStore.authToken, (t, prev) => { if (t && !prev) void loadAll(); });
</script>

<template>
  <div class="tags-hw-page">
    <div class="settings-container">
      <div class="settings-header">
        <button class="back-btn" @click="router.push(`/${props.profile}/settings/tags`)">
          <Icon icon="mdi:arrow-left" />
          Back to Tags settings
        </button>
        <h1 class="settings-title">Tag hardware</h1>
        <p class="settings-subtitle">
          Admin only · the companions, gateways, bridges and tags on this server, and which profile
          owns which tag.
        </p>
      </div>

      <div v-if="loading && !hardware" class="loading">Loading…</div>
      <div v-else-if="loadError && !hardware" class="load-error">{{ loadError }}</div>
      <template v-else-if="hardware">
        <TagCompanionsCard :companions="companions" :device-count="deviceCount" :now="now" @changed="poll.trigger()" />
        <TagInventoryCard
          :devices="devices"
          :companions="companions"
          :profiles="profiles"
          :now="now"
          @changed="poll.trigger()"
        />
        <TagOperationsCard
          :companions="companions"
          :commands="commands"
          :devices="devices"
          :now="now"
          @changed="poll.trigger()"
        />
      </template>

      <ElCard v-if="defaultsLoaded" shadow="never" class="section-card">
        <template #header>
          <div>
            <span class="section-title">Defaults for every profile</span>
            <p class="section-sub">
              What a profile gets for anything it has not set itself in Settings → Tags.
            </p>
          </div>
        </template>
        <TagOptionsForm
          v-model="form"
          :inherited="builtin"
          :kinds="kinds"
          :layouts="layouts"
          :devices="null"
          inherit-label="built-in default"
          own-timezone-hint="each profile's own timezone"
        />
      </ElCard>

      <SettingsSaveBar
        v-if="defaultsLoaded"
        :dirty="dirty"
        :saving="saving"
        :disabled="!!problem"
        :hint="dirty ? problem : ''"
        save-label="Save defaults"
        @save="saveDefaults"
        @discard="discardDefaults"
      />
    </div>
  </div>
</template>

<style scoped>
.tags-hw-page {
  width: 100%; height: 100%; overflow-y: auto; box-sizing: border-box;
  padding: 24px; background: var(--bg-color); color: var(--text-primary);
}
.settings-container { max-width: 1040px; margin: 0 auto; }
.settings-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.settings-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.settings-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.loading { padding: 60px 0; text-align: center; color: var(--text-secondary); }
.load-error { padding: 24px 0; color: var(--el-color-danger); }
.section-card { margin-bottom: 16px; }
.section-title { font-weight: 600; color: var(--text-primary); }
.section-sub { margin: 4px 0 0; font-size: 0.8rem; color: var(--text-tertiary); }
</style>
