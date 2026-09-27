<script setup lang="ts">
/**
 * Settings → Tags: whether Cremind makes cards for this profile's tags, which
 * tags get which kinds of card, how cards look, and the content credentials a
 * companion uses to fetch them.
 *
 * The on/off switch acts at once (PUT {enabled}); the routing and look form
 * saves from the bar (PUT {options}, which replaces the profile's own
 * overrides — a field left on "Use admin default" is simply not sent).
 * Credentials are created and revoked immediately.
 *
 * CLI counterpart: `cremind tags settings` / `cremind tags configure` /
 * `cremind tags credentials …`.
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElCard, ElMessage, ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../stores/tags';
import { useSettingsStore } from '../stores/settings';
import { useSavedSnapshot } from '../composables/useUnsavedChanges';
import { useNow } from '../composables/useNow';
import SettingsSaveBar from '../components/shared/SettingsSaveBar.vue';
import TagOptionsForm from '../components/tags/TagOptionsForm.vue';
import TagContentCredentials from '../components/tags/TagContentCredentials.vue';
import {
  draftFromOptions, draftProblem, inheritedOptions, optionsFromDraft, type TagOptionsDraft,
} from '../utils/tagsFormat';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const store = useTagsStore();
const settingsStore = useSettingsStore();
const { now } = useNow();

store.reset(props.profile);

const loading = ref(false);
const loadError = ref('');
const saving = ref(false);
const toggling = ref(false);

const settings = computed(() => store.settings);
const kinds = computed(() => settings.value?.routable_kinds ?? []);
const form = ref<TagOptionsDraft>(draftFromOptions({}, []));
const snapshot = useSavedSnapshot(() => form.value);
const dirty = computed(() => !!settings.value && snapshot.dirty.value);
const problem = computed(() => draftProblem(form.value));

const inherited = computed(() => inheritedOptions(settings.value?.defaults));
const ownTimezoneHint = computed(() => {
  const s = settings.value;
  // With no override anywhere, the resolved timezone IS the profile's own.
  if (s && s.options.timezone == null && !s.defaults.timezone && s.timezone) {
    return `your profile's timezone (${s.timezone})`;
  }
  return "your profile's timezone";
});
const isAdmin = computed(() => !!settings.value?.is_admin);

function hydrate() {
  const s = settings.value;
  if (!s) return;
  form.value = draftFromOptions(s.options, s.routable_kinds);
  snapshot.commit();
}

async function load() {
  if (!settingsStore.authToken) return;
  loading.value = true;
  loadError.value = '';
  try {
    await Promise.all([
      store.loadSettings(),
      // The owned tags, for "specific tags" routes.
      store.loadOverview().catch(() => null),
    ]);
    hydrate();
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Failed to load Tags settings';
  } finally {
    loading.value = false;
  }
}

async function toggleEnabled(value: boolean) {
  toggling.value = true;
  try {
    await store.saveSettings({ enabled: value });
    ElMessage.success(value ? 'Tags turned on for this profile' : 'Tags turned off for this profile');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to update');
  } finally {
    toggling.value = false;
  }
}

async function save() {
  if (!dirty.value || problem.value) return;
  saving.value = true;
  try {
    await store.saveSettings({ options: optionsFromDraft(form.value) });
    hydrate();
    ElMessage.success('Tags settings saved');
  } catch (e) {
    // invalid_settings names each refused field in its message.
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save');
  } finally {
    saving.value = false;
  }
}

function discard() {
  form.value = snapshot.saved();
}

onMounted(load);
watch(() => settingsStore.authToken, (t, prev) => { if (t && !prev) void load(); });
</script>

<template>
  <div class="tags-settings-page">
    <div class="settings-container">
      <div class="settings-header">
        <button class="back-btn" @click="router.push(`/${props.profile}/settings`)">
          <Icon icon="mdi:arrow-left" />
          Back to Settings
        </button>
        <div class="header-row">
          <div>
            <h1 class="settings-title">Tags</h1>
            <p class="settings-subtitle">
              What your Cremind Tag screens show · Profile <strong>{{ profile }}</strong>
            </p>
          </div>
          <div class="header-actions">
            <ElButton @click="router.push(`/${props.profile}/tags`)">
              <Icon icon="mdi:tablet-dashboard" class="btn-icon" /> Your tags
            </ElButton>
            <ElButton v-if="isAdmin" @click="router.push(`/${props.profile}/settings/tags/hardware`)">
              <Icon icon="mdi:chip" class="btn-icon" /> Hardware
            </ElButton>
          </div>
        </div>
      </div>

      <div v-if="loading && !settings" class="loading">Loading…</div>
      <div v-else-if="loadError && !settings" class="load-error">{{ loadError }}</div>
      <template v-else-if="settings">
        <ElCard shadow="never" class="section-card">
          <div class="enable-row">
            <div>
              <div class="enable-title">Send this profile's activity to its tags</div>
              <p class="field-hint">
                When on, replies, questions waiting for you, automation results and the other kinds
                below become cards on your tags. Off stops new cards; the tags keep what they show
                until it expires.
              </p>
            </div>
            <ElSwitch
              :model-value="settings.enabled"
              :loading="toggling"
              aria-label="Send this profile's activity to its tags"
              @update:model-value="(v) => toggleEnabled(!!v)"
            />
          </div>
        </ElCard>

        <ElCard shadow="never" class="section-card">
          <template #header><span class="section-title">Cards</span></template>
          <TagOptionsForm
            v-model="form"
            :inherited="inherited"
            :kinds="kinds"
            :layouts="settings.layouts"
            :devices="store.devices"
            inherit-label="admin default"
            :own-timezone-hint="ownTimezoneHint"
          />
        </ElCard>

        <TagContentCredentials :now="now" />

        <ElCard v-if="isAdmin" shadow="never" class="section-card admin-card">
          <div class="enable-row">
            <div>
              <div class="enable-title">Tag hardware</div>
              <p class="field-hint">
                Admin only: register companions, see every gateway, bridge and tag, decide which
                profile owns which tag, and set the defaults every profile inherits.
              </p>
            </div>
            <ElButton @click="router.push(`/${props.profile}/settings/tags/hardware`)">
              Open Hardware <Icon icon="mdi:chevron-right" />
            </ElButton>
          </div>
        </ElCard>

        <SettingsSaveBar
          :dirty="dirty"
          :saving="saving"
          :disabled="!!problem"
          :hint="dirty ? problem : ''"
          @save="save"
          @discard="discard"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.tags-settings-page {
  width: 100%; height: 100%; overflow-y: auto; box-sizing: border-box;
  padding: 24px; background: var(--bg-color); color: var(--text-primary);
}
.settings-container { max-width: 880px; margin: 0 auto; }
.settings-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 16px; transition: color 0.2s;
}
.back-btn:hover { color: var(--primary-color); }
.header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.header-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.header-actions .el-button + .el-button { margin-left: 0; }
.settings-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.settings-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.btn-icon { margin-right: 6px; }
.loading { padding: 60px 0; text-align: center; color: var(--text-secondary); }
.load-error { padding: 24px 0; color: var(--el-color-danger); }
.section-card { margin-bottom: 16px; }
.section-title { font-weight: 600; color: var(--text-primary); }
.enable-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.enable-title { font-weight: 600; color: var(--text-primary); }
.field-hint { margin: 4px 0 0; font-size: 0.8rem; line-height: 1.5; color: var(--text-tertiary); max-width: 600px; }
</style>
