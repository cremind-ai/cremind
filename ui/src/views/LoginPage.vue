<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useRouter } from 'vue-router';
import { ElInput, ElButton, ElAlert } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import { checkSetupStatus, resetOrphanedSetup } from '../services/configApi';
import { fetchMe } from '../services/agentApi';

const props = defineProps<{
  profile: string;
}>();

const router = useRouter();
const settingsStore = useSettingsStore();

const token = ref('');
const loading = ref(false);
const profileExists = ref(true);
const hasOtherProfiles = ref(false);
const resetting = ref(false);
const error = ref('');

onMounted(async () => {
  // Check if profile exists
  try {
    const status = await checkSetupStatus(settingsStore.agentUrl, props.profile);
    // If first-time setup hasn't run yet, there's no profile to log into —
    // send the user to the setup wizard instead of the orphaned-setup UI.
    if (!status.setup_complete) {
      router.replace('/setup');
      return;
    }
    if (!status.profile_exists) {
      profileExists.value = false;
      // has_profiles reflects visible profiles across the install; used to
      // pick the right recovery path when this specific profile is missing.
      hasOtherProfiles.value = status.has_profiles === true;
    }
  } catch {
    error.value = 'Cannot connect to server';
  }
});

async function handleLogin() {
  if (!token.value.trim()) return;
  loading.value = true;
  error.value = '';
  try {
    // Verify token by calling /api/me
    const me = await fetchMe(settingsStore.agentUrl, token.value.trim());
    if (me.profile !== props.profile) {
      error.value = `This token belongs to profile '${me.profile}', not '${props.profile}'`;
      return;
    }
    // Save token per-profile, activate, and navigate to chat
    settingsStore.setTokenForProfile(props.profile, token.value.trim());
    settingsStore.activateProfile(props.profile);
    router.push(`/${props.profile}`);
  } catch (e) {
    error.value = 'Invalid or expired token';
  } finally {
    loading.value = false;
  }
}

async function handleResetSetup() {
  resetting.value = true;
  try {
    // Two recovery paths:
    //  - No visible profiles at all → truly orphaned; clear setup_complete and
    //    run the first-setup wizard from scratch.
    //  - Other profiles exist → only the admin profile is missing; skip the
    //    reset (which would 403) and recreate admin via the subsequent-profile
    //    flow in SetupWizard.
    if (hasOtherProfiles.value) {
      settingsStore.removeTokenForProfile(props.profile);
      router.replace(`/setup/${props.profile}`);
      return;
    }
    await resetOrphanedSetup(settingsStore.agentUrl);
    settingsStore.removeTokenForProfile(props.profile);
    router.replace('/setup');
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to reset setup status';
  } finally {
    resetting.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-container">
      <h1 class="login-title">Login</h1>
      <p class="login-profile">Profile: <strong>{{ profile }}</strong></p>

      <ElAlert v-if="!profileExists" type="error" :closable="false" show-icon>
        <template #title>Profile '{{ profile }}' does not exist</template>
        <template v-if="profile === 'admin'" #default>
          <p style="margin: 8px 0">The admin profile has been removed. You can re-run the setup wizard to create it again.</p>
          <ElButton type="primary" size="small" :loading="resetting" @click="handleResetSetup">
            <Icon icon="mdi:refresh" /> Re-run Setup
          </ElButton>
        </template>
        <template v-else #default>
          Please check the URL or contact an administrator.
        </template>
      </ElAlert>

      <template v-else>
        <ElAlert v-if="error" type="error" :closable="true" show-icon class="error-alert" @close="error = ''">
          {{ error }}
        </ElAlert>

        <div class="token-input-section">
          <p class="input-label">Enter your authentication token:</p>
          <ElInput
            v-model="token"
            type="textarea"
            :rows="3"
            placeholder="Paste your JWT token here"
            class="token-input"
          />
          <div class="login-actions">
            <ElButton type="primary" :loading="loading" @click="handleLogin" :disabled="!token.trim()">
              <Icon icon="mdi:login" /> Login
            </ElButton>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  width: 100%; height: 100vh; display: flex; align-items: center;
  justify-content: center; background: var(--bg-color); padding: 24px; box-sizing: border-box;
}
.login-container {
  width: 100%; max-width: 480px; background: var(--surface-color);
  border-radius: 12px; border: 1px solid var(--border-color);
  padding: 32px; box-shadow: 0 4px 24px rgba(0, 0, 0, 0.08);
}
.login-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px 0; }
.login-profile { color: var(--text-secondary); font-size: 0.9rem; margin: 0 0 24px 0; }
.error-alert { margin-bottom: 16px; }
.input-label { font-size: 0.875rem; color: var(--text-primary); margin: 0 0 8px 0; }
.token-input { margin-bottom: 16px; }
.token-input :deep(textarea) { font-family: monospace; font-size: 0.8rem; }
.login-actions { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
