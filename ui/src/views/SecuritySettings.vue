<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElCheckbox, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';

import CaTrustPanel from '../components/shared/CaTrustPanel.vue';
import { useHttpsPivot } from '../composables/useHttpsPivot';
import {
  cancelHttps,
  fetchServiceCapabilities,
  fetchTlsStatus,
  prepareHttps,
  type TlsRuntimeStatus,
  type TlsStatus,
} from '../services/configApi';
import { handleHttpsTransition } from '../services/httpsTransition';
import { migrationReadiness } from '../services/migrationReadiness';
import { useSettingsStore } from '../stores/settings';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

const loading = ref(true);
const working = ref(false);
const loadError = ref<string | null>(null);
const runtime = ref<TlsRuntimeStatus | null>(null);
const tls = ref<TlsStatus | null>(null);
const installMode = ref<string | null>(null);
const trustConfirmed = ref(false);
const commandsVisible = ref(false);
const copied = ref<string | null>(null);
const pivot = useHttpsPivot();
const { phase: pivotPhase, error: pivotError, forwardHint } = pivot;
const migrationReady = migrationReadiness.ready;
const pendingUploads = migrationReadiness.pendingUploads;

const transition = computed(() => runtime.value?.transition ?? null);
const isExternal = computed(() => runtime.value?.management === 'external');
const isElectron = computed(() => runtime.value?.management === 'electron');
const isPrepared = computed(() => transition.value?.phase === 'prepared');
const isActivating = computed(() => ['quiescing', 'activating'].includes(transition.value?.phase ?? ''));
const certificateKind = computed(() =>
  transition.value?.certificate_kind ?? runtime.value?.certificate_kind ?? 'none',
);
const hasCertificateError = computed(() => Boolean(
  runtime.value?.serving_https
  && (runtime.value.ready === false || runtime.value.certificate_error),
));
const isHttps = computed(() => Boolean(
  runtime.value
    ? runtime.value.serving_https && runtime.value.ready !== false && !runtime.value.certificate_error
    : tls.value?.serving_https,
));
const usesLocalCertificate = computed(() => Boolean(
  certificateKind.value === 'local'
  || (!transition.value?.certificate_kind && transition.value?.ca_sha256),
));
const canActivate = computed(() =>
  isPrepared.value && trustConfirmed.value && migrationReady.value && !working.value,
);

const instructions = computed(() => {
  if (runtime.value?.instructions?.length) return runtime.value.instructions;
  const mode = (installMode.value ?? '').toLowerCase();
  if (mode === 'kubernetes') {
    return [
      'helm upgrade <release> <chart> --reuse-values --set cremind.ssl=auto',
      'kubectl rollout status deployment/<deployment> --timeout=5m',
      'kubectl port-forward svc/<service> 1515:80',
    ];
  }
  if (mode === 'docker') {
    return [
      'Set CREMIND_SSL=auto in the Docker .env file.',
      'docker compose up -d --force-recreate cremind',
    ];
  }
  return ['Restart Cremind with: cremind serve'];
});

async function load() {
  loading.value = true;
  loadError.value = null;
  try {
    const [status, caps] = await Promise.all([
      fetchTlsStatus(settingsStore.agentUrl),
      fetchServiceCapabilities(settingsStore.agentUrl, settingsStore.authToken),
    ]);
    runtime.value = status;
    tls.value = caps.tls ?? null;
    installMode.value = caps.install_mode ?? status.install_mode ?? null;
    trustConfirmed.value = Boolean(caps.tls?.local_trust?.already_trusted);
    if (status.transition && ['prepared', 'quiescing'].includes(status.transition.phase)) {
      await handleHttpsTransition(status.transition, settingsStore.agentUrl, settingsStore.authToken);
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Failed to load HTTPS status';
  } finally {
    loading.value = false;
  }
}

async function prepare() {
  working.value = true;
  loadError.value = null;
  try {
    runtime.value = await prepareHttps(
      settingsStore.agentUrl,
      settingsStore.authToken,
      window.location.origin,
    );
    await handleHttpsTransition(
      runtime.value.transition!,
      settingsStore.agentUrl,
      settingsStore.authToken,
    );
    const caps = await fetchServiceCapabilities(settingsStore.agentUrl, settingsStore.authToken);
    tls.value = caps.tls ?? null;
    installMode.value = caps.install_mode ?? runtime.value.install_mode ?? null;
    trustConfirmed.value = Boolean(caps.tls?.local_trust?.already_trusted);
    ElMessage.success(
      runtime.value.transition?.certificate_kind === 'external'
        ? 'HTTPS handoff prepared. Verify the edge certificate before activation.'
        : 'HTTPS certificate prepared. Trust it before activation.',
    );
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    working.value = false;
  }
}

async function activate() {
  if (!transition.value || (isPrepared.value && !canActivate.value)) return;
  const options = {
    agentUrl: settingsStore.agentUrl,
    restartToken: settingsStore.authToken,
    nextOrigin: transition.value.target_origin,
    profile: props.profile,
    profileToken: settingsStore.authToken,
    installMode: installMode.value,
    management: runtime.value?.management,
    transition: transition.value,
    resumeStatus: runtime.value,
    onQuiescing: (status: TlsRuntimeStatus) => {
      runtime.value = status;
    },
    onActivated: (status: TlsRuntimeStatus) => {
      runtime.value = status;
      commandsVisible.value = isExternal.value || status.restart_supported === false
        || Boolean(status.restart_error);
      working.value = false;
    },
    onFailure: (message: string) => {
      loadError.value = message;
      working.value = false;
      void fetchTlsStatus(settingsStore.agentUrl).then((status) => {
        runtime.value = status;
      }).catch(() => { /* keep the actionable activation error */ });
    },
  };
  working.value = true;
  if (isExternal.value) pivot.enterManualMode(options);
  else if (runtime.value?.restart_supported || isElectron.value) await pivot.run(options);
  else pivot.enterManualMode(options);
}

async function cancel() {
  if (!transition.value) return;
  pivot.cancelManualProbe();
  working.value = true;
  try {
    runtime.value = await cancelHttps(
      settingsStore.agentUrl,
      settingsStore.authToken,
      transition.value.id,
    );
    await window.cremind?.server?.releaseHttpsMigration?.();
    trustConfirmed.value = false;
    commandsVisible.value = false;
    ElMessage.success('HTTPS preparation cancelled. Cremind remains on HTTP.');
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    working.value = false;
  }
}

async function copyCommand(command: string, key: string) {
  try {
    await navigator.clipboard.writeText(command);
    copied.value = key;
    setTimeout(() => { if (copied.value === key) copied.value = null; }, 1500);
  } catch {
    ElMessage.error('Could not copy the command');
  }
}

onMounted(load);
</script>

<template>
  <div class="security-page">
    <div class="security-container">
      <button class="back-btn" @click="router.push(`/${props.profile}/settings`)">
        <Icon icon="mdi:arrow-left" /> Back to Settings
      </button>
      <h1>HTTPS &amp; Certificate</h1>
      <p class="subtitle">Protect the connection between every browser or app window and Cremind.</p>

      <div v-if="loading" class="state-card">Loading HTTPS status…</div>
      <div v-else-if="loadError && !runtime" class="state-card error-card">
        <h2>Could not read HTTPS status</h2>
        <p>{{ loadError }}</p>
        <button class="secondary-btn" @click="load">Try again</button>
      </div>

      <template v-else>
        <section class="benefits-card">
          <div class="benefit-icon"><Icon icon="mdi:shield-lock-outline" /></div>
          <div>
            <h2>Why switch to HTTPS?</h2>
            <p>
              HTTPS encrypts profile tokens, messages, files, and settings while they travel
              over your network. It also proves that the page came from your Cremind server,
              which prevents another device from silently changing traffic. Cremind enables
              HTTP/2 with HTTPS, so several tabs can stream without the browser's small
              HTTP/1.1 connection limit.
            </p>
            <p>
              Cremind's generated certificate uses a private certificate authority. Trust
              that CA once on every device that connects; a public-domain certificate or
              TLS-terminating Ingress uses its normal public trust chain instead.
            </p>
          </div>
        </section>

        <div v-if="loadError" class="inline-error">{{ loadError }}</div>

        <section v-if="hasCertificateError" class="state-card error-card">
          <Icon icon="mdi:certificate-alert-outline" class="large-icon" />
          <div>
            <h2>HTTPS certificate needs attention</h2>
            <p>{{ runtime?.certificate_error || 'The configured certificate is not ready for this address.' }}</p>
            <p>
              Keep the plaintext recovery page open, replace or renew the certificate so it
              covers this hostname, then restart Cremind and verify the HTTPS address again.
            </p>
            <div v-for="(command, index) in instructions" :key="index" class="command-row">
              <code>{{ command }}</code>
              <button @click="copyCommand(command, `error-${index}`)">
                <Icon :icon="copied === `error-${index}` ? 'mdi:check' : 'mdi:content-copy'" />
              </button>
            </div>
          </div>
        </section>

        <section v-else-if="isHttps" class="state-card success-card">
          <Icon icon="mdi:lock-check-outline" class="large-icon" />
          <div>
            <h2>HTTPS is active</h2>
            <p>This server is securely available at <code>{{ runtime?.https_url }}</code>.</p>
            <p v-if="runtime?.certificate_kind === 'local' && runtime?.ca_sha256">
              Local CA fingerprint: <code class="fingerprint">{{ runtime.ca_sha256 }}</code>
            </p>
            <p v-else-if="runtime?.certificate_kind === 'custom'">
              Custom server certificate fingerprint:
              <code class="fingerprint">{{ runtime.certificate_sha256 }}</code>.
              Its normal certificate chain must be trusted on each connecting device.
            </p>
            <p v-else>The certificate is managed externally; no Cremind CA is required.</p>
          </div>
        </section>

        <template v-else>
          <ol class="steps" aria-label="HTTPS activation progress">
            <li class="done"><span>1</span>Understand</li>
            <li :class="{ active: !isPrepared && !isActivating, done: isPrepared || isActivating }"><span>2</span>Prepare</li>
            <li :class="{ active: isPrepared, done: isActivating }"><span>3</span>Trust</li>
            <li :class="{ active: isActivating }"><span>4</span>Activate</li>
            <li><span>5</span>Verify</li>
          </ol>

          <section v-if="!isPrepared && !isActivating" class="state-card">
            <h2>Prepare HTTPS</h2>
            <p v-if="certificateKind === 'external'">
              Cremind will prepare a handoff to the HTTPS address managed by your Ingress
              or reverse proxy. It will not create a private CA; configure a valid certificate
              for the exact public hostname while HTTP remains available.
            </p>
            <p v-else-if="certificateKind === 'custom'">
              Cremind will validate the configured certificate, private key, validity period,
              and this browser's hostname while HTTP remains available.
            </p>
            <p v-else>
              Cremind will generate a local CA and server certificate without stopping HTTP.
              You can inspect and trust it before any tab changes address.
            </p>
            <button class="primary-btn" :disabled="working" @click="prepare">
              <Icon :icon="working ? 'mdi:loading' : 'mdi:certificate-outline'" :class="{ spin: working }" />
              {{ working ? 'Preparing…' : 'Prepare HTTPS certificate' }}
            </button>
          </section>

          <section v-else-if="isPrepared" class="state-card">
            <h2>Trust the certificate, then activate</h2>
            <p v-if="usesLocalCertificate">
              Compare the fingerprint before granting trust. On another computer, repeat
              these instructions on that computer before opening the HTTPS address.
            </p>
            <p v-else-if="certificateKind === 'custom'">
              Verify that the shown server-certificate fingerprint matches your configured
              certificate and that its issuer is trusted on every connecting device.
            </p>
            <p v-else>
              Confirm that the Ingress or proxy certificate covers the exact public hostname
              and that each connecting device trusts its normal certificate chain.
            </p>
            <p v-if="certificateKind === 'custom' && transition?.certificate_sha256">
              SHA-256: <code class="fingerprint">{{ transition.certificate_sha256 }}</code>
            </p>
            <CaTrustPanel
              v-if="usesLocalCertificate"
              :agent-url="settingsStore.agentUrl"
              :tls="tls"
              :install-mode="installMode"
              :auth-token="settingsStore.authToken"
              variant="settings"
            />
            <ElCheckbox v-model="trustConfirmed" class="trust-confirm">
              {{ usesLocalCertificate
                ? 'I trusted this exact CA on this device.'
                : certificateKind === 'custom'
                  ? 'I verified this exact server certificate and trust chain on this device.'
                  : 'I have a valid edge certificate for this hostname and trust its issuer on this device.' }}
            </ElCheckbox>
            <p v-if="!migrationReady" class="upload-wait">
              Waiting for {{ pendingUploads }} file upload(s) to finish
              before reloading tabs.
            </p>
            <div class="actions">
              <button class="secondary-btn" :disabled="working" @click="cancel">Cancel</button>
              <button class="primary-btn" :disabled="!canActivate" @click="activate">
                <Icon icon="mdi:lock-outline" />
                {{ isExternal ? 'Show deployment commands' : 'Activate HTTPS' }}
              </button>
            </div>
          </section>

          <section v-else class="state-card">
            <template v-if="isExternal || commandsVisible || runtime?.restart_supported === false">
              <h2>{{ isExternal ? 'Apply HTTPS to the deployment' : 'Restart Cremind to finish' }}</h2>
              <p v-if="isExternal">
                Certificate preparation is complete. Run these commands from the deployment
                host. Kubernetes must update its proxy, Service, and probes together; simply
                changing the container environment will break routing.
              </p>
              <p v-else>
                HTTPS settings and session handoffs are saved. Run the command below from
                this installation; keep this page open while the secure listener starts.
              </p>
              <p v-if="pivotPhase === 'waiting'" class="notice">
                Cremind is checking the HTTPS address. Keep this page open while you apply
                the deployment change; every Electron window or browser tab will move after
                the expected secure server is reachable.
              </p>
              <p v-if="pivotError" class="inline-error">{{ pivotError }}</p>
              <div v-for="(command, index) in instructions" :key="index" class="command-row">
                <code>{{ command }}</code>
                <button @click="copyCommand(command, String(index))">
                  <Icon :icon="copied === String(index) ? 'mdi:check' : 'mdi:content-copy'" />
                </button>
              </div>
              <p v-if="installMode === 'kubernetes'" class="notice">
                The rollout replaces the pod and may close kubectl port-forward. Run the shown
                port-forward command again; this page will continue when HTTPS becomes reachable.
              </p>
            </template>
            <template v-else>
              <div class="spinner"></div>
              <h2>{{ transition?.phase === 'quiescing'
                ? 'Waiting for open tabs…'
                : pivotPhase === 'manual' ? 'Restart Cremind to finish' : 'Switching to HTTPS…' }}</h2>
              <p>{{ pivotError || (transition?.phase === 'quiescing'
                ? `${runtime?.quiesce_pending ?? 'Some'} tab(s) are finishing uploads and saving private session handoffs before the restart.`
                : 'Every open Cremind tab will reopen at its current page after the secure listener is verified.') }}</p>
              <p v-if="forwardHint">Rerun your Kubernetes port-forward and confirm this device trusts the CA.</p>
              <button
                v-if="transition?.phase === 'quiescing'"
                class="secondary-btn"
                @click="cancel"
              >Cancel HTTPS switch</button>
              <button
                v-if="pivotError && isActivating"
                class="primary-btn"
                :disabled="working"
                @click="activate"
              >
                Retry HTTPS restart
              </button>
            </template>
          </section>
        </template>
      </template>
    </div>
  </div>
</template>

<style scoped>
.security-page { width: 100%; height: 100%; overflow-y: auto; background: var(--bg-color); padding: 24px; box-sizing: border-box; }
.security-container { max-width: 900px; margin: 0 auto 64px; color: var(--text-primary); }
.security-container h1 { margin: 0 0 4px; font-size: 1.65rem; }
.subtitle { margin: 0 0 24px; color: var(--text-secondary); }
.back-btn { display: flex; gap: 6px; align-items: center; border: 0; background: none; color: var(--text-secondary); cursor: pointer; padding: 4px 0; margin-bottom: 16px; }
.benefits-card, .state-card { border: 1px solid var(--border-color); border-radius: 10px; background: var(--surface-color); padding: 22px 24px; margin-bottom: 20px; }
.benefits-card { display: flex; gap: 18px; }
.benefit-icon, .large-icon { color: var(--primary-color); font-size: 2rem; flex: none; }
h2 { margin: 0 0 9px; font-size: 1.05rem; }
p { color: var(--text-secondary); line-height: 1.6; margin: 7px 0; }
.success-card { display: flex; gap: 16px; border-color: var(--success-color); }
.steps { display: grid; grid-template-columns: repeat(5, 1fr); padding: 0; margin: 24px 0; list-style: none; gap: 8px; }
.steps li { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: .8rem; }
.steps span { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--border-color); }
.steps .active { color: var(--primary-color); font-weight: 600; }
.steps .active span { border-color: var(--primary-color); }
.steps .done span { background: var(--primary-color); color: white; border-color: var(--primary-color); }
.primary-btn, .secondary-btn { display: inline-flex; align-items: center; gap: 7px; border-radius: 6px; padding: 9px 16px; cursor: pointer; font-weight: 600; }
.primary-btn { border: 1px solid var(--primary-color); background: var(--primary-color); color: white; }
.secondary-btn { border: 1px solid var(--border-color); background: transparent; color: var(--text-primary); }
button:disabled { opacity: .55; cursor: not-allowed; }
.actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.trust-confirm { margin-top: 16px; white-space: normal; }
.inline-error, .upload-wait, .notice { padding: 10px 14px; border-radius: 6px; background: var(--el-color-warning-light-9); color: var(--el-color-warning-dark-2); margin-bottom: 14px; }
.error-card, .inline-error { border-color: var(--el-color-danger); }
.command-row { display: flex; align-items: flex-start; gap: 8px; margin: 10px 0; padding: 10px 12px; background: var(--hover-bg); border-radius: 6px; }
.command-row code { flex: 1; white-space: pre-wrap; word-break: break-word; }
.command-row button { border: 0; background: none; color: var(--text-secondary); cursor: pointer; }
code { background: var(--hover-bg); padding: 2px 5px; border-radius: 4px; }
.fingerprint { word-break: break-all; }
.spinner { width: 30px; height: 30px; border: 3px solid var(--border-color); border-top-color: var(--primary-color); border-radius: 50%; animation: spin .9s linear infinite; }
.spin { animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 700px) { .steps { grid-template-columns: 1fr; }.benefits-card { display: block; }.benefit-icon { margin-bottom: 8px; } }
</style>
