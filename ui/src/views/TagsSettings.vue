<script setup lang="ts">
/**
 * Settings → Tags: the profile's Cremind Tag hardware first (simple setup —
 * connect a gateway, add bridges and tags from their labels; see
 * components/tags/setup/), then whether Cremind makes cards for this
 * profile's tags, which tags get which kinds of card and how cards look, and
 * last a collapsed "Advanced" part: the content credentials a manually run
 * companion uses, diagnostics, and (admin) the legacy hardware page.
 *
 * When the server has simple setup off (GET /api/tags/connections says
 * `simple_setup: false` or answers 403 `simple_setup_disabled`), or is too old
 * to offer it, the page is the previous one — switch, Cards, credentials,
 * admin hardware — with a one-line note.
 *
 * The on/off switch acts at once (PATCH {enabled}); the routing and look form
 * saves from the bar as a PATCH of only the keys that changed (`null` for one
 * put back on "Use admin default"), so an edit made elsewhere meanwhile — the
 * CLI — is not overwritten. Credentials are created and revoked immediately.
 *
 * Hardware state is polled through the tagsSetup store: every 15 s, and every
 * 1.5 s while a setup dialog follows something.
 *
 * CLI counterpart: `cremind tags settings` / `cremind tags configure` /
 * `cremind tags credentials …`.
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElButton, ElCard, ElMessage, ElSwitch } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useTagsStore } from '../stores/tags';
import { useTagsSetupStore } from '../stores/tagsSetup';
import { useSettingsStore } from '../stores/settings';
import { useSavedSnapshot } from '../composables/useUnsavedChanges';
import { useNow } from '../composables/useNow';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import SettingsSaveBar from '../components/shared/SettingsSaveBar.vue';
import TagOptionsForm from '../components/tags/TagOptionsForm.vue';
import TagContentCredentials from '../components/tags/TagContentCredentials.vue';
import TagsHardwareSection from '../components/tags/setup/TagsHardwareSection.vue';
import {
  draftFromOptions, draftPatch, draftProblem, inheritedOptions, type TagOptionsDraft,
} from '../utils/tagsFormat';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const store = useTagsStore();
const setup = useTagsSetupStore();
const settingsStore = useSettingsStore();
const { now } = useNow();

store.reset(props.profile);
setup.reset(props.profile);

const loading = ref(false);
const loadError = ref('');
const saving = ref(false);
const toggling = ref(false);
const advancedOpen = ref(false);
/** Mounted on first open (it loads the credentials), then kept. */
const advancedMounted = ref(false);

const settings = computed(() => store.settings);
const kinds = computed(() => settings.value?.routable_kinds ?? []);
const form = ref<TagOptionsDraft>(draftFromOptions({}, []));
const snapshot = useSavedSnapshot(() => form.value);
const dirty = computed(() => !!settings.value && snapshot.dirty.value);
const problem = computed(() => draftProblem(form.value));

const inherited = computed(() => (settings.value
  ? inheritedOptions(settings.value.defaults, settings.value.builtin)
  : null));
const ownTimezoneHint = computed(() => {
  const s = settings.value;
  // With no override anywhere, the resolved timezone IS the profile's own.
  if (s && s.options.timezone == null && !s.defaults.timezone && s.timezone) {
    return `your profile's timezone (${s.timezone})`;
  }
  return "your profile's timezone";
});
const isAdmin = computed(() => !!settings.value?.is_admin);

/** The hardware section replaces the manual way when the server offers simple setup. */
const simpleSetup = computed(() => setup.availability === 'available');
const setupNote = computed(() => {
  if (setup.availability === 'disabled') {
    return 'Simple hardware setup is turned off on this server, so tags are connected the manual way (content credentials below).';
  }
  if (setup.availability === 'unsupported') {
    return 'This server does not offer simple hardware setup yet, so tags are connected the manual way (content credentials below).';
  }
  return '';
});

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
      // The owned tags, for "specific tags" routes (and the hardware list's previews).
      store.loadOverview().catch(() => null),
      // Never throw: a failure stays in setup.loadError (the computers keep their last list).
      setup.loadConnections(),
      setup.loadHosts(),
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
    await store.saveSettings({ options: draftPatch(snapshot.saved(), form.value) });
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

function toggleAdvanced() {
  advancedOpen.value = !advancedOpen.value;
  if (advancedOpen.value) advancedMounted.value = true;
}

const poll = useVisiblePoll(() => setup.tick(), () => setup.pollIntervalMs);

onMounted(() => {
  setup.setPollTrigger(poll.trigger);
  void load();
});
onBeforeUnmount(() => setup.setPollTrigger(null));
watch(() => settingsStore.authToken, (t, prev) => { if (t && !prev) void load(); });
// Another profile's copy of this page (same route, new param): the view is
// reused, so drop everything of the previous profile and load this one's.
watch(() => props.profile, (next, prev) => {
  if (next === prev) return;
  store.reset(next);
  setup.reset(next);
  advancedOpen.value = false;
  advancedMounted.value = false;
  void load();
});

// A tag added or removed: re-read the overview (the "specific tags" picker and the previews).
watch(() => setup.tags.map((t) => t.device.id).join(','), (next, prev) => {
  if (prev !== undefined && next !== prev && settings.value) void store.loadOverview().catch(() => null);
});
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
              <template v-if="simpleSetup">Your Cremind Tag hardware and what its screens show</template>
              <template v-else>What your Cremind Tag screens show</template>
              · Profile <strong>{{ profile }}</strong>
            </p>
          </div>
          <div class="header-actions">
            <ElButton @click="router.push(`/${props.profile}/tags`)">
              <Icon icon="mdi:tablet-dashboard" class="btn-icon" /> Your tags
            </ElButton>
            <ElButton v-if="isAdmin && !simpleSetup" @click="router.push(`/${props.profile}/settings/tags/hardware`)">
              <Icon icon="mdi:chip" class="btn-icon" /> Hardware
            </ElButton>
          </div>
        </div>
      </div>

      <div v-if="loading && !settings" class="loading">Loading…</div>
      <div v-else-if="loadError && !settings" class="load-error">{{ loadError }}</div>
      <template v-else-if="settings">
        <TagsHardwareSection v-if="simpleSetup" :key="profile" :profile="profile" :now="now" />
        <ElCard v-else-if="setup.availability === 'unknown' && setup.loadError" shadow="never" class="section-card">
          <div class="enable-row">
            <div>
              <div class="enable-title">Your hardware</div>
              <p class="field-hint load-error-text" role="alert">Your devices could not be loaded: {{ setup.loadError }}</p>
            </div>
            <ElButton @click="setup.loadConnections()">Try again</ElButton>
          </div>
        </ElCard>
        <p v-else-if="setupNote" class="setup-note" role="note">
          <Icon icon="mdi:information-outline" aria-hidden="true" /> {{ setupNote }}
        </p>

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
            v-if="inherited"
            v-model="form"
            :inherited="inherited"
            :kinds="kinds"
            :layouts="settings.layouts"
            :devices="store.devices"
            inherit-label="admin default"
            :own-timezone-hint="ownTimezoneHint"
          />
        </ElCard>

        <section v-if="simpleSetup" class="advanced">
          <button
            type="button"
            class="advanced-toggle"
            :aria-expanded="advancedOpen"
            aria-controls="tags-advanced"
            @click="toggleAdvanced"
          >
            <Icon :icon="advancedOpen ? 'mdi:chevron-down' : 'mdi:chevron-right'" aria-hidden="true" />
            <span class="advanced-title">Advanced</span>
            <span class="advanced-hint">Content credentials, connecting a companion manually, diagnostics</span>
          </button>
          <div v-show="advancedOpen" id="tags-advanced" class="advanced-body">
            <TagContentCredentials v-if="advancedMounted" :key="profile" :now="now" />

            <ElCard shadow="never" class="section-card">
              <div class="enable-title">Connect a companion manually</div>
              <p class="field-hint">
                For developers who run the companion themselves instead of letting Cremind drive the
                gateway: create a content credential above and give it to the companion, which also needs a hardware
                credential{{ isAdmin ? ' from Tag hardware below' : ' from an admin' }}. Tags connected
                this way are managed on the Tag hardware page, not in the lists above.
              </p>
            </ElCard>

            <ElCard shadow="never" class="section-card">
              <div class="enable-row">
                <div>
                  <div class="enable-title">Diagnostics</div>
                  <p class="field-hint">
                    Each tag's delivery history (what was sent, when it arrived, what failed) and its
                    screen previews are on the Tags page.
                  </p>
                </div>
                <ElButton @click="router.push(`/${props.profile}/tags`)">
                  Open Tags <Icon icon="mdi:chevron-right" />
                </ElButton>
              </div>
            </ElCard>

            <ElCard v-if="isAdmin" shadow="never" class="section-card admin-card">
              <div class="enable-row">
                <div>
                  <div class="enable-title">Tag hardware</div>
                  <p class="field-hint">
                    Admin only: companions connected the manual way, every gateway, bridge and tag they
                    report, which profile owns which of those tags, and the defaults every profile inherits.
                  </p>
                </div>
                <ElButton @click="router.push(`/${props.profile}/settings/tags/hardware`)">
                  Open Tag hardware <Icon icon="mdi:chevron-right" />
                </ElButton>
              </div>
            </ElCard>
          </div>
        </section>

        <template v-else>
          <TagContentCredentials :key="profile" :now="now" />

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
        </template>

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
.load-error-text { color: var(--el-color-danger); }
.section-card { margin-bottom: 16px; }
.section-title { font-weight: 600; color: var(--text-primary); }
.enable-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.enable-title { font-weight: 600; color: var(--text-primary); }
.field-hint { margin: 4px 0 0; font-size: 0.8rem; line-height: 1.5; color: var(--text-tertiary); max-width: 600px; }
.setup-note {
  display: flex; align-items: flex-start; gap: 8px; margin: 0 0 16px; font-size: 0.85rem; line-height: 1.45;
  color: var(--text-secondary);
}
.setup-note :deep(svg) { flex-shrink: 0; margin-top: 2px; color: var(--primary-color); }
.advanced { margin-bottom: 8px; }
.advanced-toggle {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap; width: 100%; padding: 8px 0; margin-bottom: 8px;
  background: none; border: none; cursor: pointer; text-align: left; color: var(--text-secondary);
}
.advanced-toggle:hover .advanced-title, .advanced-toggle:focus-visible .advanced-title { color: var(--primary-color); }
.advanced-title { font-weight: 600; font-size: 0.95rem; color: var(--text-primary); }
.advanced-hint { font-size: 0.8rem; color: var(--text-tertiary); }
</style>
