<script setup lang="ts">
// "Secure this install" — the wizard step that gets the local CA trusted
// BEFORE the server flips to HTTPS (``CREMIND_SSL=after-setup``).
//
// The whole point of the flip happening *after* setup is that this step can
// run over plain HTTP: the browser can download ``/ca.pem`` and the user can
// install it while nothing is yet being served behind the untrusted chain. By
// the time the wizard restarts the server into HTTPS, the device already
// trusts the issuer and no certificate warning is ever shown.
//
// The trust mechanics themselves live in the shared ``CaTrustPanel`` (Settings
// → HTTPS & Certificate shows the same panel for users who skip this step).
// What stays here is the wizard's own framing: this is optional, and here is
// what the restart does next.
//
// The step is deliberately skippable. A user who skips it still lands on the
// HTTPS origin at the end — they just get their browser's interstitial once,
// which is the honest signal that they have not trusted the CA yet.
import { computed } from 'vue';
import { ElAlert, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useCopyToClipboard } from '../../composables/useCopyToClipboard';
import CaTrustPanel from '../shared/CaTrustPanel.vue';
import type { TlsStatus } from '../../services/configApi';

const props = defineProps<{
  agentUrl: string;
  tls: TlsStatus | null;
  /** INSTALL_MODE, for deployment-specific copy. On Kubernetes the restart
   *  drops the user's `kubectl port-forward`, and forewarning that here is
   *  what keeps it a non-event instead of a "connection lost" scare. */
  installMode?: string | null;
}>();

const { copy, isCopied } = useCopyToClipboard();
async function copyValue(text: string, key: string) {
  if (!(await copy(text, key))) ElMessage.error('Failed to copy');
}

const httpsUrl = computed(() => props.tls?.https_url ?? null);
const restartSupported = computed(() => Boolean(props.tls?.restart_supported));
const isKubernetes = computed(
  () => (props.installMode ?? '').toLowerCase() === 'kubernetes',
);
</script>

<template>
  <div class="step-secure-install">
    <h3 class="step-title">Secure this install</h3>
    <p class="step-description">
      When you finish setup, Cremind restarts and starts serving over
      <strong>HTTPS</strong> using a certificate signed by a private
      certificate authority it generated for this install. Trust that CA now,
      while you are still on plain HTTP, and the switch happens without a
      single certificate warning.
    </p>

    <ElAlert type="info" :closable="false" show-icon class="skip-alert">
      <template #title>This step is optional</template>
      Skipping it is safe — you will simply have to click through your
      browser's "your connection is not private" warning the first time you
      open the HTTPS address, and again on every other device you use. Trusting
      the CA is a one-off per device, and you can come back to it later under
      <em>Settings → HTTPS &amp; Certificate</em>.
    </ElAlert>

    <!-- No auth token: the wizard runs inside the pre-setup bootstrap window,
         where no JWT exists and the backend skips its admin gate. -->
    <CaTrustPanel
      variant="wizard"
      :agent-url="agentUrl"
      :tls="tls"
      :install-mode="installMode"
    />

    <div class="info-box next-box">
      <strong>What happens next</strong>
      <p v-if="restartSupported">
        When you click <strong>Start Using Cremind</strong> on the final step,
        Cremind restarts itself and this page moves to
        <code v-if="httpsUrl">{{ httpsUrl }}</code><span v-else>its HTTPS address</span>.
        You stay signed in — your session is carried across.
      </p>
      <p v-if="restartSupported && isKubernetes">
        The restart takes 20&ndash;40 seconds and this page waits it out on its
        own. Your <code>kubectl port-forward</code> normally survives it. If
        the page is still waiting after a minute, re-run the same port-forward
        command in your terminal — on some setups the tunnel does end with the
        restart — and everything continues automatically from there.
      </p>
      <p v-else>
        When you click <strong>Start Using Cremind</strong> on the final step,
        you'll be asked to restart the server yourself (nothing supervises this
        process, so Cremind can't restart itself without staying down). Once it
        is back up, this page moves to
        <code v-if="httpsUrl">{{ httpsUrl }}</code><span v-else>its HTTPS address</span>
        automatically, still signed in.
      </p>
      <p v-if="httpsUrl" class="hint">
        Bookmark <code>{{ httpsUrl }}</code><button
          type="button"
          class="copy-icon-btn"
          :class="{ copied: isCopied('https-url') }"
          :title="isCopied('https-url') ? 'Copied!' : 'Copy URL'"
          @click="copyValue(httpsUrl ?? '', 'https-url')"
        ><Icon :icon="isCopied('https-url') ? 'mdi:check' : 'mdi:content-copy'" /></button> —
        the plain-HTTP address stops answering after the restart.
      </p>
    </div>
  </div>
</template>

<style scoped>
.step-secure-install { padding: 8px 0; }
.step-title { font-size: 1.1rem; font-weight: 600; color: var(--text-primary); margin: 0 0 8px 0; }
.step-description { color: var(--text-secondary); font-size: 0.875rem; margin: 0 0 20px 0; line-height: 1.5; }
.step-description strong { color: var(--text-primary); }
.skip-alert { margin-bottom: 24px; }

.copy-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  vertical-align: middle;
  background: none;
  border: none;
  cursor: pointer;
  padding: 1px;
  margin-left: 4px;
  border-radius: 4px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  transition: color 0.15s ease;
}
.copy-icon-btn:hover { color: var(--primary-color); }
.copy-icon-btn.copied { color: var(--success-color); }

.info-box {
  padding: 12px 16px; background: var(--hover-bg); border-radius: 8px;
  font-size: 0.825rem; color: var(--text-secondary); line-height: 1.5;
  margin-bottom: 12px;
}
.info-box strong { color: var(--text-primary); }
.info-box p { margin: 6px 0 0; }
.info-box code {
  background: var(--surface-color); padding: 1px 4px;
  border-radius: 3px; font-size: 0.75rem; word-break: break-all;
}
.next-box .hint { margin-top: 10px; font-size: 0.78rem; }
</style>
