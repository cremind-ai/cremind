<script setup lang="ts">
import { ref } from 'vue';
import { ElButton, ElAlert, ElSelect, ElOption } from 'element-plus';
import { Icon } from '@iconify/vue';

defineProps<{
  token: string;
  submitting: boolean;
  profileName: string;
  configSaved: boolean;
}>();

const emit = defineEmits<{
  generate: [];
  download: [format: 'md' | 'json' | 'env'];
}>();

const exportFormat = ref<'md' | 'json' | 'env'>('md');
</script>

<template>
  <div class="step-profile-create">
    <h3 class="step-title">Profile & Token</h3>
    <p class="step-description">
      Your profile will be created and a JWT token generated.
      The token is also saved on the server for recovery.
    </p>

    <div class="profile-info">
      <div class="info-row">
        <span class="info-label">Profile Name:</span>
        <span class="info-value">{{ profileName }}</span>
      </div>
    </div>

    <div v-if="!token" class="generate-section">
      <p class="step-description">
        Click the button below to save configuration and generate your authentication token.
      </p>
    </div>

    <div v-else class="token-section">
      <ElAlert type="success" :closable="false" show-icon class="token-alert">
        <template #title>Setup Complete!</template>
        Download your configuration below. The token inside is also saved at
        <code>~/.cremind/tokens/{{ profileName }}.token</code> on the server.
      </ElAlert>

      <ElAlert
        v-if="!configSaved"
        type="warning"
        :closable="false"
        show-icon
        class="save-required-alert"
      >
        <template #title>Save the configuration to continue</template>
        Download the file below before clicking <strong>Start Using Cremind</strong>.
        It bundles the JWT and every connection detail you'd otherwise have
        to dig out of the server.
      </ElAlert>
      <ElAlert
        v-else
        type="success"
        :closable="false"
        show-icon
        class="save-required-alert"
      >
        <template #title>Configuration saved</template>
        You can now click <strong>Start Using Cremind</strong>. Re-download
        anytime to grab a different format.
      </ElAlert>

      <div class="download-controls">
        <div class="format-row">
          <label class="format-label">Format</label>
          <ElSelect v-model="exportFormat" class="format-select">
            <ElOption label="Markdown (.md)" value="md" />
            <ElOption label="JSON (.json)" value="json" />
            <ElOption label="Env file (.env)" value="env" />
          </ElSelect>
        </div>
        <ElButton
          type="primary"
          class="download-btn"
          @click="emit('download', exportFormat)"
        >
          <Icon icon="mdi:download" />
          {{ configSaved ? 'Download again' : 'Download configuration' }}
        </ElButton>
      </div>

      <div class="info-box">
        The export bundles your token, project paths, database and vector-store
        parameters, embedding settings, channels, and the VNC password when
        running under Docker. If you lose the token later, recover it from
        <code>~/.cremind/tokens/{{ profileName }}.token</code> on the server.
      </div>
    </div>
  </div>
</template>

<style scoped>
.step-profile-create { padding: 8px 0; }
.step-title { font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0; }
.step-description { color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 20px 0; line-height: 1.5; }
.profile-info { padding: 16px; background: var(--hover-bg); border-radius: 8px; margin-bottom: 24px; }
.info-row { display: flex; align-items: center; gap: 8px; }
.info-label { font-weight: 600; font-size: 0.875rem; color: var(--text-primary); }
.info-value { font-family: monospace; font-size: 0.95rem; color: var(--primary-color); font-weight: 600; }
.token-alert { margin-bottom: 16px; }
.token-alert code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.75rem; }
.save-required-alert { margin-bottom: 16px; }
.download-controls {
  display: flex; align-items: flex-end; gap: 12px;
  margin-bottom: 16px; flex-wrap: wrap;
}
.format-row { display: flex; flex-direction: column; gap: 4px; }
.format-label { font-size: 0.825rem; color: var(--text-secondary); }
.format-select { width: 200px; }
.download-btn { align-self: flex-end; }
.info-box {
  padding: 12px 16px; background: var(--hover-bg); border-radius: 8px;
  font-size: 0.825rem; color: var(--text-secondary); line-height: 1.5;
}
.info-box code { background: var(--surface-color); padding: 1px 4px; border-radius: 3px; font-size: 0.75rem; }
</style>
