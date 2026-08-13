<script setup lang="ts">
/**
 * Settings -> GSuite: everything about the Google apps Cremind can reach.
 *
 * Two groups, in information order. **Accounts** first — which Google account each
 * skill uses, and the only place to unlink one. Then **Drive file access**, because
 * per-file grants only mean anything once an account is linked.
 *
 * They were two separate settings pages, which split one question ("what does
 * Cremind have access to in my Google account?") across two places and gave gdrive
 * two different Unlink buttons.
 */
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { Icon } from '@iconify/vue';
import GoogleAccountsSection from '../components/gsuite/GoogleAccountsSection.vue';
import DriveAccessSection from '../components/gsuite/DriveAccessSection.vue';

const props = defineProps<{ profile: string }>();
const router = useRouter();

const drive = ref<InstanceType<typeof DriveAccessSection> | null>(null);

function goBack() {
  router.push(`/${props.profile}/settings`);
}

/** An unlink in the Accounts group invalidates the Drive group's cached status. */
function onAccountsChanged() {
  void drive.value?.reload();
}
</script>

<template>
  <div class="gsuite-page">
    <div class="gsuite-container">
      <div class="gsuite-header">
        <button class="back-btn" @click="goBack">
          <Icon icon="mdi:arrow-left" /> Back to Settings
        </button>
        <h1 class="gsuite-title">GSuite</h1>
        <p class="gsuite-subtitle">
          The Google apps and services Cremind can use — Gmail, Calendar, Drive,
          Sheets and Docs — the account behind each one, and what it is allowed to
          reach.
        </p>
      </div>

      <h2 class="group-title">Accounts</h2>
      <GoogleAccountsSection :profile="profile" @changed="onAccountsChanged" />

      <h2 class="group-title">Google Drive file access</h2>
      <DriveAccessSection ref="drive" :profile="profile" />
    </div>
  </div>
</template>

<style scoped>
.gsuite-page {
  width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color);
  padding: 24px; box-sizing: border-box;
}
.gsuite-container { max-width: 860px; margin: 0 auto; }
.gsuite-header { margin-bottom: 24px; }
.back-btn {
  display: flex; align-items: center; gap: 6px; background: none; border: none;
  color: var(--text-secondary); cursor: pointer; font-size: 0.875rem;
  padding: 4px 0; margin-bottom: 16px;
}
.back-btn:hover { color: var(--primary-color); }
.gsuite-title { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); margin: 0 0 4px; }
.gsuite-subtitle {
  color: var(--text-secondary); font-size: 0.875rem; margin: 0; max-width: 640px;
}
.group-title {
  font-size: 1rem; font-weight: 600; color: var(--text-primary);
  margin: 28px 0 10px; padding-bottom: 6px;
  border-bottom: 1px solid var(--border-color);
}
.group-title:first-of-type { margin-top: 8px; }
</style>
