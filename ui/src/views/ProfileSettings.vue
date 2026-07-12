<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElInput, ElButton, ElMessage, ElTable, ElTableColumn, ElPopconfirm, ElAlert, ElDialog, ElCheckbox, ElCheckboxGroup } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { listProfiles, deleteProfile, reconfigure, getPersona, updatePersona, getAgentName, setAgentName } from '../services/configApi';
import { cleanProfileData, CLEAN_GROUPS, type CleanScope } from '../services/cleanApi';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const profiles = ref<string[]>([]);
const loading = ref(false);
const newProfileName = ref('');
const nameError = ref('');
const showReconfigureConfirm = ref(false);
const reconfiguring = ref(false);

// Danger Zone — per-profile "clean data". Acts on the signed-in profile (the one
// /api/clean resolves from the token), so gate it when viewing another profile.
const cleanGroups = CLEAN_GROUPS;
const selectedComponents = ref<string[]>([]);
const cleaning = ref(false);
const showWorkingConfirm = ref(false);
const showFactoryConfirm = ref(false);
const factoryConfirmText = ref('');
const isOwnProfile = computed(() => props.profile === settingsStore.profileId);

// Agent name (loaded from backend)
const agentName = ref('');
const loadingAgentName = ref(false);
const savingAgentName = ref(false);

// PERSONA.md content (loaded from backend)
const personaContent = ref('');
const loadingPersona = ref(false);
const savingPersona = ref(false);

async function loadAgentName() {
  loadingAgentName.value = true;
  try {
    const res = await getAgentName(settingsStore.agentUrl, settingsStore.authToken, props.profile);
    agentName.value = res.name;
  } catch {
    ElMessage.error('Failed to load agent name');
  } finally {
    loadingAgentName.value = false;
  }
}

async function saveAgentName() {
  const name = agentName.value.trim();
  if (!name) { ElMessage.error('Agent name is required'); return; }
  savingAgentName.value = true;
  try {
    await setAgentName(settingsStore.agentUrl, settingsStore.authToken, props.profile, name);
    agentName.value = name;
    ElMessage.success('Agent name saved');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to save agent name');
  } finally {
    savingAgentName.value = false;
  }
}

async function loadPersona() {
  loadingPersona.value = true;
  try {
    const res = await getPersona(settingsStore.agentUrl, settingsStore.authToken, props.profile);
    personaContent.value = res.content;
  } catch {
    ElMessage.error('Failed to load PERSONA.md');
  } finally {
    loadingPersona.value = false;
  }
}

async function savePersona() {
  savingPersona.value = true;
  try {
    await updatePersona(settingsStore.agentUrl, settingsStore.authToken, props.profile, personaContent.value);
    ElMessage.success('PERSONA.md saved');
  } catch {
    ElMessage.error('Failed to save PERSONA.md');
  } finally {
    savingPersona.value = false;
  }
}

// Profile name validation regex: lowercase, numbers, hyphens, underscores
const PROFILE_NAME_RE = /^[a-z0-9_-]+$/;

onMounted(async () => {
  await Promise.all([loadProfiles(), loadAgentName(), loadPersona()]);
});

async function loadProfiles() {
  loading.value = true;
  try {
    const res = await listProfiles(settingsStore.agentUrl, settingsStore.authToken);
    profiles.value = res.profiles;
  } catch { ElMessage.error('Failed to load profiles'); }
  finally { loading.value = false; }
}

function validateName(): boolean {
  const name = newProfileName.value.trim();
  if (!name) { nameError.value = 'Profile name is required'; return false; }
  if (!PROFILE_NAME_RE.test(name)) {
    nameError.value = 'Only lowercase letters, numbers, hyphens, and underscores allowed';
    return false;
  }
  if (name.length > 64) { nameError.value = 'Max 64 characters'; return false; }
  if (profiles.value.includes(name)) { nameError.value = 'Profile already exists'; return false; }
  nameError.value = '';
  return true;
}

function handleCreateProfile() {
  if (!validateName()) return;
  const name = newProfileName.value.trim();

  // Open a new tab with the setup page for this profile
  // The actual profile creation happens during the setup flow in the new tab
  const setupUrl = `${window.location.origin}${window.location.pathname}#/setup/${encodeURIComponent(name)}`;
  window.open(setupUrl, '_blank');

  newProfileName.value = '';
}

async function handleDeleteProfile(name: string) {
  try {
    await deleteProfile(settingsStore.agentUrl, settingsStore.authToken, name);
    // Drop the deleted profile's cached token + saved-profile entry so it
    // stops appearing on the profile selector in this browser. Other browsers
    // reconcile against the live list on the selector (ProfileSelector.vue).
    settingsStore.removeTokenForProfile(name);
    ElMessage.success(`Profile '${name}' deleted`);
    await loadProfiles();
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to delete profile');
  }
}

async function handleReconfigure() {
  reconfiguring.value = true;
  try {
    await reconfigure(settingsStore.agentUrl, settingsStore.authToken);
    ElMessage.success('Setup status reset. Redirecting to setup...');
    showReconfigureConfirm.value = false;
    // Redirect to setup in current tab
    router.push('/setup');
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to reconfigure');
  } finally {
    reconfiguring.value = false;
  }
}

async function runClean(scope: CleanScope, components: string[] = []) {
  cleaning.value = true;
  try {
    const r = await cleanProfileData(settingsStore.agentUrl, settingsStore.authToken, scope, components);
    const errorCount = Object.keys(r.errors || {}).length;
    if (errorCount) {
      ElMessage.warning(`Cleaned ${r.total} item(s); ${errorCount} component(s) had errors`);
    } else {
      ElMessage.success(`Cleaned ${r.total} item(s)`);
    }
    // Refresh the conversation list if we may have cleared it.
    if (scope !== 'custom' || components.includes('conversations')) {
      try {
        const { useChatStore } = await import('../stores/chat');
        await useChatStore().loadConversations();
      } catch { /* not on a chat route / store not ready */ }
    }
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : 'Failed to clean data');
  } finally {
    cleaning.value = false;
  }
}

async function handleCleanSelected() {
  if (!selectedComponents.value.length) { ElMessage.warning('Select at least one item to clean'); return; }
  await runClean('custom', [...selectedComponents.value]);
  selectedComponents.value = [];
}

async function handleWorkingReset() {
  await runClean('working');
  showWorkingConfirm.value = false;
}

async function handleFactoryReset() {
  if (factoryConfirmText.value !== props.profile) return;
  await runClean('factory');
  showFactoryConfirm.value = false;
  factoryConfirmText.value = '';
}

function goBack() { router.push(`/${props.profile}/settings`); }
</script>

<template>
  <div class="profile-settings-page">
    <div class="page-container">
      <div class="page-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="page-title">Profiles</h1>
        <p class="page-subtitle">Manage user profiles</p>
      </div>

      <!-- Agent Name -->
      <div class="section">
        <h2 class="section-title">
          <Icon icon="mdi:robot-outline" class="section-icon" /> Agent Name
        </h2>
        <p class="section-desc">
          The name your assistant goes by. Available in prompts as
          <code>$CREMIND_AGENT_NAME</code> and shown in the chat <code>@</code> menu.
        </p>
        <div v-if="loadingAgentName" class="loading-state">Loading...</div>
        <div v-else class="create-form">
          <div class="input-group">
            <ElInput
              v-model="agentName"
              placeholder="e.g. Cremind"
              class="profile-input"
              maxlength="128"
            />
          </div>
          <ElButton type="primary" :loading="savingAgentName" :disabled="!agentName.trim()" @click="saveAgentName">
            <Icon icon="mdi:content-save" style="margin-right: 6px;" /> Save
          </ElButton>
        </div>
      </div>

      <!-- PERSONA.md -->
      <div class="section">
        <h2 class="section-title">
          <Icon icon="mdi:file-document-edit-outline" class="section-icon" /> PERSONA.md
        </h2>
        <p class="section-desc">
          Define the AI assistant's persona and user context. This file is stored on the server at
          <code>&lt;working_dir&gt;/{{ profile }}/PERSONA.md</code>.
        </p>
        <div v-if="loadingPersona" class="loading-state">Loading...</div>
        <ElInput
          v-else
          v-model="personaContent"
          type="textarea"
          :autosize="{ minRows: 4, maxRows: 16 }"
          placeholder="You are a personal AI assistant..."
          style="font-family: monospace;"
        />
        <ElButton type="primary" :loading="savingPersona" @click="savePersona" style="margin-top: 12px;">
          <Icon icon="mdi:content-save" style="margin-right: 6px;" /> Save
        </ElButton>
      </div>

      <!-- Create Profile -->
      <div class="section">
        <h2 class="section-title">Create New Profile</h2>
        <p class="section-desc">
          Creates a new profile and opens its setup page in a new tab.
          Profile names must be lowercase with only letters, numbers, hyphens, and underscores.
        </p>
        <div class="create-form">
          <div class="input-group">
            <ElInput
              v-model="newProfileName"
              placeholder="e.g. lee, ly_0, my-profile"
              class="profile-input"
              @input="nameError = ''"
            />
            <p v-if="nameError" class="name-error">{{ nameError }}</p>
          </div>
          <ElButton type="primary" @click="handleCreateProfile" :disabled="!newProfileName.trim()">
            <Icon icon="mdi:open-in-new" /> Create & Open Setup
          </ElButton>
        </div>
      </div>

      <!-- Existing Profiles -->
      <div class="section">
        <h2 class="section-title">Existing Profiles</h2>
        <div v-if="loading" class="loading-state">Loading...</div>
        <ElTable v-else :data="profiles.map(p => ({ name: p }))" stripe size="default">
          <ElTableColumn prop="name" label="Profile Name" />
          <ElTableColumn label="Actions" width="120" align="right">
            <template #default="{ row }">
              <ElPopconfirm
                :title="`Delete profile '${row.name}'? This cannot be undone.`"
                confirm-button-text="Delete"
                cancel-button-text="Cancel"
                @confirm="handleDeleteProfile(row.name)"
              >
                <template #reference>
                  <ElButton type="danger" size="small" :disabled="row.name === 'admin'">
                    <Icon icon="mdi:delete" /> Delete
                  </ElButton>
                </template>
              </ElPopconfirm>
            </template>
          </ElTableColumn>
        </ElTable>
      </div>

      <!-- Reconfigure -->
      <div class="section">
        <h2 class="section-title">Reconfigure</h2>
        <p class="section-desc">
          Reset the setup status and re-run the initial configuration wizard.
          This does NOT delete any profiles or data.
        </p>
        <ElButton type="warning" @click="showReconfigureConfirm = true">
          <Icon icon="mdi:refresh" /> Reconfigure from Scratch
        </ElButton>
      </div>

      <!-- Danger Zone -->
      <div class="section danger-zone">
        <h2 class="section-title">
          <Icon icon="mdi:alert-octagon-outline" class="section-icon danger-icon" /> Danger Zone
        </h2>
        <p class="section-desc">
          Permanently delete data for the <strong>{{ props.profile }}</strong> profile.
          These actions cannot be undone. The profile itself and server-wide settings are kept.
        </p>

        <div v-if="!isOwnProfile" class="loading-state">
          Sign in as “{{ props.profile }}” to clean this profile's data.
        </div>

        <template v-else>
          <!-- 1. Clean specific components -->
          <h3 class="danger-subtitle">Clean specific data</h3>
          <ElCheckboxGroup v-model="selectedComponents" class="clean-groups">
            <div v-for="g in cleanGroups" :key="g.title" class="clean-group">
              <p class="clean-group-title">{{ g.title }}</p>
              <div class="clean-group-items">
                <ElCheckbox v-for="c in g.components" :key="c.key" :value="c.key" :label="c.label" />
              </div>
            </div>
          </ElCheckboxGroup>
          <ElButton
            type="danger" plain :loading="cleaning"
            :disabled="!selectedComponents.length"
            @click="handleCleanSelected"
          >
            <Icon icon="mdi:broom" style="margin-right: 6px;" />
            Clean selected ({{ selectedComponents.length }})
          </ElButton>

          <!-- 2 & 3. Presets -->
          <h3 class="danger-subtitle" style="margin-top: 24px;">Reset presets</h3>
          <div class="danger-presets">
            <ElButton type="warning" @click="showWorkingConfirm = true">
              <Icon icon="mdi:refresh" style="margin-right: 6px;" /> Working-data reset
            </ElButton>
            <ElButton type="danger" @click="showFactoryConfirm = true">
              <Icon icon="mdi:nuke" style="margin-right: 6px;" /> Full factory reset
            </ElButton>
          </div>
          <p class="section-desc" style="margin-top: 8px;">
            <strong>Working-data reset</strong> clears runtime data (conversations, memory, usage,
            events, uploads) but keeps your configuration. <strong>Full factory reset</strong> also
            strips LLM keys, OAuth logins, tools, skills, documents and settings back to a fresh
            baseline — the Setup Wizard will not re-run.
          </p>
        </template>
      </div>
    </div>

    <!-- Reconfigure Confirmation -->
    <ElDialog v-model="showReconfigureConfirm" title="Confirm Reconfigure" width="400px">
      <ElAlert type="warning" :closable="false" show-icon>
        This will reset the setup wizard. You will need to re-enter your LLM provider keys and tool configurations.
        Existing profiles, conversations, and data will NOT be deleted.
      </ElAlert>
      <template #footer>
        <ElButton @click="showReconfigureConfirm = false">Cancel</ElButton>
        <ElButton type="warning" :loading="reconfiguring" @click="handleReconfigure">
          Confirm Reconfigure
        </ElButton>
      </template>
    </ElDialog>

    <!-- Working-data reset confirmation -->
    <ElDialog v-model="showWorkingConfirm" title="Working-data reset" width="460px">
      <ElAlert type="warning" :closable="false" show-icon>
        This deletes conversations, long-term memory, usage records, event-runs,
        schedules, file watchers, skill-events, uploads and plan files for
        “{{ props.profile }}”. All configuration and credentials are kept.
        This cannot be undone.
      </ElAlert>
      <template #footer>
        <ElButton @click="showWorkingConfirm = false">Cancel</ElButton>
        <ElButton type="warning" :loading="cleaning" @click="handleWorkingReset">
          Wipe runtime data
        </ElButton>
      </template>
    </ElDialog>

    <!-- Full factory reset confirmation (type-to-confirm) -->
    <ElDialog v-model="showFactoryConfirm" title="Full factory reset" width="480px">
      <ElAlert type="error" :closable="false" show-icon>
        This wipes ALL runtime data AND every post-setup customization for
        “{{ props.profile }}”: LLM keys, OAuth logins, tools/MCP, skills,
        documents, browser login and app settings all return to a fresh baseline
        (no LLM configured, default persona, default skills, only the “main”
        channel). The profile and the Setup Wizard status are kept — the wizard
        will NOT re-run. This cannot be undone.
      </ElAlert>
      <p class="section-desc" style="margin-top: 14px;">
        Type <strong>{{ props.profile }}</strong> to confirm:
      </p>
      <ElInput v-model="factoryConfirmText" :placeholder="props.profile" />
      <template #footer>
        <ElButton @click="showFactoryConfirm = false; factoryConfirmText = ''">Cancel</ElButton>
        <ElButton
          type="danger" :loading="cleaning"
          :disabled="factoryConfirmText !== props.profile"
          @click="handleFactoryReset"
        >
          Factory reset this profile
        </ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.profile-settings-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.page-container { max-width: 720px; margin: 0 auto; }
.page-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none;
  border: none; color: var(--text-secondary); cursor: pointer;
  font-size: 0.875rem; padding: 4px 0; margin-bottom: 12px;
}
.back-btn:hover { color: var(--primary-color); }
.page-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.page-subtitle { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
.section { margin-bottom: 32px; }
.section-title {
  font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 4px 0;
  display: flex; align-items: center; gap: 8px;
}
.section-icon { font-size: 20px; color: var(--primary-color); }
.section-desc { color: var(--text-secondary); font-size: 0.825rem; margin: 0 0 12px 0; line-height: 1.5; }
.loading-state { text-align: center; padding: 20px; color: var(--text-secondary); }
.create-form { display: flex; gap: 12px; align-items: flex-start; max-width: 520px; }
.input-group { flex: 1; }
.profile-input { width: 100%; }
.name-error { color: var(--el-color-danger); font-size: 0.75rem; margin: 4px 0 0 0; }

/* Danger Zone */
.danger-zone {
  border: 1px solid var(--el-color-danger);
  border-radius: 8px;
  padding: 16px 20px;
}
.danger-icon { color: var(--el-color-danger); }
.danger-subtitle {
  font-size: 0.95rem; font-weight: 600; color: var(--text-primary);
  margin: 8px 0 10px 0;
}
.clean-groups { display: block; margin-bottom: 16px; }
.clean-group { margin-bottom: 12px; }
.clean-group-title {
  font-size: 0.8rem; font-weight: 600; color: var(--text-secondary);
  margin: 0 0 4px 0;
}
.clean-group-items { display: flex; flex-wrap: wrap; gap: 4px 20px; }
.danger-presets { display: flex; gap: 12px; flex-wrap: wrap; }
</style>
