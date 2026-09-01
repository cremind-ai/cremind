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
// Two ways to get there:
//
// - **One click**, when the server says it can do it itself
//   (``tls.local_trust.supported`` — a native install answering its own
//   machine's browser). ``POST /api/tls/trust`` hands the CA to the OS trust
//   store server-side; the OS still shows its own confirmation (Windows'
//   dialog, macOS' password prompt), which is the real consent moment.
// - **Manually**, everywhere else (Docker, Kubernetes, a server reached from
//   another device): download the CA and run the per-OS command. When the
//   one-click path exists these instructions collapse into a fallback.
//
// The step is deliberately skippable. A user who skips it still lands on the
// HTTPS origin at the end — they just get their browser's interstitial once,
// which is the honest signal that they have not trusted the CA yet.
import { computed, ref } from 'vue';
import { ElAlert, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';
import { useCopyToClipboard } from '../../composables/useCopyToClipboard';
import { trustLocalCa } from '../../services/configApi';
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

// ``/ca.pem`` is public and sets its own Content-Disposition, so the download
// is a bare anchor — no bearer token, no fetch/blob dance, and the file lands
// as ``cremind-local-ca.pem`` which every command below refers to by name.
const caUrl = computed(() => `${props.agentUrl}/ca.pem`);

const fingerprint = computed(() => props.tls?.ca_sha256 ?? null);
const httpsUrl = computed(() => props.tls?.https_url ?? null);
const restartSupported = computed(() => Boolean(props.tls?.restart_supported));
const isKubernetes = computed(
  () => (props.installMode ?? '').toLowerCase() === 'kubernetes',
);
const isDocker = computed(
  () => (props.installMode ?? '').toLowerCase() === 'docker',
);

// ── one-click trust ──
const localTrust = computed(() => props.tls?.local_trust ?? null);
// The fingerprint gate matters: the POST must echo it, so without one there
// is nothing to click. (No CA generated ⇒ the server refuses anyway.)
const autoSupported = computed(
  () => Boolean(localTrust.value?.supported) && Boolean(fingerprint.value),
);

type TrustState = 'idle' | 'working' | 'done' | 'already' | 'failed';
const trustState = ref<TrustState>(
  localTrust.value?.already_trusted === true ? 'already' : 'idle',
);
const trustError = ref<string | null>(null);
const trustDone = computed(
  () => trustState.value === 'done' || trustState.value === 'already',
);

// Forewarn about the OS-side prompt: the request blocks on it, and on
// Windows the dialog can open behind the browser window.
const osPromptNote = computed(() => {
  switch (localTrust.value?.os_prompt) {
    case 'windows':
      return 'Windows shows its own confirmation dialog before the '
        + 'certificate lands — approve it there. If it does not pop to the '
        + 'front, check the taskbar.';
    case 'macos':
      return 'macOS may ask for your login password to change the keychain.';
    default:
      return null;
  }
});

async function runAutoTrust() {
  if (!fingerprint.value || trustState.value === 'working') return;
  trustState.value = 'working';
  trustError.value = null;
  try {
    const result = await trustLocalCa(props.agentUrl, fingerprint.value);
    if (result.trusted) {
      trustState.value = result.already_trusted ? 'already' : 'done';
    } else {
      trustState.value = 'failed';
      trustError.value = result.error ?? 'The server could not install the certificate.';
    }
  } catch {
    trustState.value = 'failed';
    trustError.value = 'The trust request never reached the server.';
  }
}

// Manual instructions: the only path when one-click is unavailable; a
// collapsed fallback when it is.
const showManual = ref(false);
const manualVisible = computed(() => !autoSupported.value || showManual.value);

// Per-OS trust commands. These mirror ``_platform_commands`` in
// app/cli/commands/tls.py — the CLI decides the same thing at runtime for the
// machine it runs on; here we cannot know the user's OS for certain, so we
// show all of them and merely emphasise the detected one. Keep the two in
// step if either changes.
interface TrustTarget {
  key: string;
  label: string;
  store: string;
  command: string;
  note?: string;
}

const TRUST_TARGETS: TrustTarget[] = [
  {
    key: 'windows',
    label: 'Windows',
    store: "the current user's Trusted Root store",
    command: 'certutil -addstore -user Root cremind-local-ca.pem',
    note: 'No admin shell needed. Windows shows a confirmation dialog before it lands.',
  },
  {
    key: 'macos',
    label: 'macOS',
    store: 'the System keychain',
    command:
      'sudo security add-trusted-cert -d -r trustRoot '
      + '-k /Library/Keychains/System.keychain cremind-local-ca.pem',
  },
  {
    key: 'debian',
    label: 'Linux — Debian / Ubuntu',
    store: '/usr/local/share/ca-certificates',
    command:
      'sudo cp cremind-local-ca.pem /usr/local/share/ca-certificates/cremind-local-ca.crt '
      + '&& sudo update-ca-certificates',
  },
  {
    key: 'rhel',
    label: 'Linux — RHEL / Fedora',
    store: '/etc/pki/ca-trust/source/anchors',
    command:
      'sudo cp cremind-local-ca.pem /etc/pki/ca-trust/source/anchors/cremind-local-ca.crt '
      + '&& sudo update-ca-trust extract',
  },
];

/** Best-effort OS detection, only ever used to re-order and emphasise. */
function detectPlatformKey(): string | null {
  const nav = navigator as Navigator & { userAgentData?: { platform?: string } };
  const raw = (nav.userAgentData?.platform ?? navigator.platform ?? '').toLowerCase();
  if (!raw) return null;
  if (raw.includes('win')) return 'windows';
  if (raw.includes('mac') || raw.includes('darwin')) return 'macos';
  // Both Linux families report the same string; Debian/Ubuntu is the far more
  // common desktop case, and the RHEL row stays visible right below it.
  if (raw.includes('linux') || raw.includes('x11')) return 'debian';
  return null;
}

const detectedKey = detectPlatformKey();

// Detected OS first, everything else after it in declaration order — the
// user always sees every option, so a wrong guess costs nothing.
const orderedTargets = computed(() => {
  if (!detectedKey) return TRUST_TARGETS;
  const hit = TRUST_TARGETS.filter((t) => t.key === detectedKey);
  const rest = TRUST_TARGETS.filter((t) => t.key !== detectedKey);
  return [...hit, ...rest];
});
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
      the CA is a one-off per device.
    </ElAlert>

    <!-- ── one click, when the server can do it itself ── -->
    <div v-if="autoSupported" class="section auto-trust">
      <h4 class="section-title">Trust it on this device</h4>
      <p class="section-hint">
        Cremind runs on this computer, so it can hand the certificate to your
        operating system for you<template v-if="localTrust?.store"> —
        it goes into {{ localTrust.store }}</template>.
      </p>

      <div v-if="fingerprint" class="fingerprint-box">
        <code class="fingerprint">{{ fingerprint }}</code>
        <button
          type="button"
          class="copy-icon-btn"
          :class="{ copied: isCopied('fingerprint') }"
          :title="isCopied('fingerprint') ? 'Copied!' : 'Copy fingerprint'"
          @click="copyValue(fingerprint ?? '', 'fingerprint')"
        ><Icon :icon="isCopied('fingerprint') ? 'mdi:check' : 'mdi:content-copy'" /></button>
      </div>
      <p class="section-hint fingerprint-hint">
        This SHA-256 identifies the certificate — it must match what your OS
        shows in its confirmation dialog.
      </p>

      <ElAlert
        v-if="trustState === 'already'"
        type="success"
        :closable="false"
        show-icon
      >
        <template #title>Already trusted</template>
        This device already trusts the Cremind certificate authority — there
        is nothing to do here.
      </ElAlert>
      <ElAlert
        v-else-if="trustState === 'done'"
        type="success"
        :closable="false"
        show-icon
      >
        <template #title>Certificate trusted</template>
        The switch to HTTPS will happen without a warning on this device.
      </ElAlert>
      <template v-else>
        <button
          type="button"
          class="trust-btn"
          :disabled="trustState === 'working'"
          @click="runAutoTrust"
        >
          <Icon
            :icon="trustState === 'working' ? 'mdi:loading' : 'mdi:shield-check-outline'"
            :class="{ spin: trustState === 'working' }"
          />
          {{ trustState === 'working'
            ? 'Waiting for your confirmation…'
            : 'Trust it on this device' }}
        </button>
        <p v-if="trustState === 'working' && osPromptNote" class="trust-note">
          {{ osPromptNote }}
        </p>
        <ElAlert
          v-if="trustState === 'failed'"
          type="error"
          :closable="false"
          show-icon
          class="trust-error"
        >
          <template #title>Could not trust the certificate</template>
          {{ trustError }} You can run the manual command below instead — or
          skip this step and accept the one-time browser warning.
        </ElAlert>
      </template>
    </div>

    <button
      v-if="autoSupported"
      type="button"
      class="manual-toggle"
      @click="showManual = !showManual"
    >
      <Icon :icon="showManual ? 'mdi:chevron-down' : 'mdi:chevron-right'" />
      {{ trustDone
        ? 'Trusting another device? Do it manually there'
        : 'Prefer to do it manually?' }}
    </button>

    <template v-if="manualVisible">
      <div v-if="isDocker" class="info-box">
        <strong>Installed with the Cremind installer on this machine?</strong>
        It offered to trust this certificate during installation — if you
        accepted there, this step is already done and you can continue.
      </div>

      <div class="section">
        <h4 class="section-title">1. Download the certificate</h4>
        <p class="section-hint">
          Save it next to wherever you will run the command below — the commands
          assume the file is in your current directory.
        </p>
        <!-- A bare anchor, not a fetch+blob: ``/ca.pem`` is public (no bearer
             header to attach) and the server already sets the filename via
             Content-Disposition. -->
        <a :href="caUrl" download class="download-link">
          <Icon icon="mdi:certificate-outline" />
          Download cremind-local-ca.pem
        </a>
      </div>

      <!-- With the one-click panel open, the fingerprint is already on
           screen above — do not show it twice. -->
      <div v-if="fingerprint && !autoSupported" class="section">
        <h4 class="section-title">2. Check the fingerprint</h4>
        <p class="section-hint">
          You are about to give this certificate root authority on your device,
          so confirm it is the right one. This SHA-256 must match what your OS
          shows in its confirmation dialog, and what your browser's certificate
          viewer shows afterwards.
        </p>
        <div class="fingerprint-box">
          <code class="fingerprint">{{ fingerprint }}</code>
          <button
            type="button"
            class="copy-icon-btn"
            :class="{ copied: isCopied('fingerprint-manual') }"
            :title="isCopied('fingerprint-manual') ? 'Copied!' : 'Copy fingerprint'"
            @click="copyValue(fingerprint ?? '', 'fingerprint-manual')"
          ><Icon :icon="isCopied('fingerprint-manual') ? 'mdi:check' : 'mdi:content-copy'" /></button>
        </div>
      </div>

      <div class="section">
        <h4 class="section-title">
          {{ fingerprint && !autoSupported ? '3.' : '2.' }} Trust it on this device
        </h4>
        <p class="section-hint">
          Run the command for your operating system from the folder holding the
          downloaded file.
        </p>
        <div
          v-for="target in orderedTargets"
          :key="target.key"
          class="trust-row"
          :class="{ 'is-detected': target.key === detectedKey }"
        >
          <div class="trust-head">
            <span class="trust-os">{{ target.label }}</span>
            <span v-if="target.key === detectedKey" class="trust-badge">detected</span>
            <span class="trust-store">→ {{ target.store }}</span>
          </div>
          <div class="command-box">
            <code class="command">{{ target.command }}</code>
            <button
              type="button"
              class="copy-icon-btn"
              :class="{ copied: isCopied(target.key) }"
              :title="isCopied(target.key) ? 'Copied!' : 'Copy command'"
              @click="copyValue(target.command, target.key)"
            ><Icon :icon="isCopied(target.key) ? 'mdi:check' : 'mdi:content-copy'" /></button>
          </div>
          <p v-if="target.note" class="trust-note">{{ target.note }}</p>
        </div>
      </div>
    </template>

    <div class="info-box">
      <strong>Using Firefox?</strong> Firefox keeps its own trust store and
      ignores the system one. Import the same file under
      <em>Settings → Privacy &amp; Security → Certificates → View Certificates
      → Authorities</em>.
    </div>

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

.section { margin-bottom: 24px; }
.section-title {
  font-size: 0.925rem; font-weight: 600; color: var(--text-primary);
  margin: 0 0 6px 0;
}
.section-hint {
  color: var(--text-secondary); font-size: 0.825rem;
  margin: 0 0 12px 0; line-height: 1.5;
}

/* ── one-click trust ── */
.auto-trust .fingerprint-box { margin-bottom: 6px; }
.fingerprint-hint { margin-bottom: 14px; }
.trust-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 10px 20px;
  border: none;
  border-radius: 4px;
  background: var(--primary-color, var(--el-color-primary));
  color: #fff;
  font-size: 0.9rem;
  font-weight: 500;
  cursor: pointer;
  transition: filter 0.15s ease;
}
.trust-btn:hover:not(:disabled) { filter: brightness(1.1); }
.trust-btn:disabled { opacity: 0.75; cursor: default; }
.trust-btn .spin { animation: trust-spin 1s linear infinite; }
@keyframes trust-spin { to { transform: rotate(360deg); } }
.trust-error { margin-top: 12px; }

.manual-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 0 0 20px;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  font-size: 0.825rem;
  color: var(--text-secondary);
}
.manual-toggle:hover { color: var(--primary-color); }

/* Styled as a primary button, but kept a real <a download> so the browser
   handles the transfer and honours the server's filename. */
.download-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 18px;
  border-radius: 4px;
  background: var(--primary-color, var(--el-color-primary));
  color: #fff;
  font-size: 0.875rem;
  font-weight: 500;
  text-decoration: none;
  transition: filter 0.15s ease;
}
.download-link:hover { filter: brightness(1.1); }

.fingerprint-box, .command-box {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 10px 12px; background: var(--hover-bg);
  border-radius: 6px; border: 1px solid var(--border-color);
}
.fingerprint, .command {
  flex: 1; font-family: monospace; font-size: 0.75rem;
  line-height: 1.5; color: var(--text-primary);
  word-break: break-all; background: none; padding: 0;
}

.trust-row { margin-bottom: 14px; }
.trust-row.is-detected .command-box {
  border-color: var(--primary-color);
  background: var(--el-color-primary-light-9, rgba(64, 158, 255, 0.08));
}
.trust-head {
  display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; margin-bottom: 6px;
}
.trust-os { font-size: 0.825rem; font-weight: 600; color: var(--text-primary); }
.trust-badge {
  padding: 1px 6px; font-size: 11px; border-radius: 9px;
  background: var(--el-color-success-light-9, rgba(103, 194, 58, 0.15));
  color: var(--el-color-success, #67c23a);
}
.trust-store { font-size: 0.75rem; color: var(--text-secondary); font-family: monospace; }
.trust-note {
  margin: 6px 0 0; font-size: 0.75rem; color: var(--text-secondary); line-height: 1.45;
}

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
