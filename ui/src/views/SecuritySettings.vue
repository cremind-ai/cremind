<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { ElCheckbox, ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';

import CaTrustPanel from '../components/shared/CaTrustPanel.vue';
import DeploymentSteps from '../components/shared/DeploymentSteps.vue';
import HttpsRecoveryHelp from '../components/shared/HttpsRecoveryHelp.vue';
import { useHttpsPivot } from '../composables/useHttpsPivot';
import {
  cancelHttps,
  fetchServiceCapabilities,
  fetchTlsStatus,
  isTransientTlsFailure,
  prepareHttps,
  TLS_STATUS_TIMEOUT_MS,
  TlsApiError,
  type ServiceCapabilitiesResponse,
  type TlsRuntimeStatus,
  type TlsStatus,
  type TlsTransition,
} from '../services/configApi';
import {
  handleHttpsTransition,
  httpsTransitionState,
  retryHttpsTransition,
  savedHttpsTransition,
} from '../services/httpsTransition';
import { tunnelCommand } from '../services/httpsReadiness';
import { migrationReadiness } from '../services/migrationReadiness';
import { useSettingsStore } from '../stores/settings';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settingsStore = useSettingsStore();

/** Same threshold as the waiting loops: long enough that "still starting" has
 *  stopped being the likely explanation. */
const RECOVERY_HELP_AFTER_MS = 45_000;

const loading = ref(true);
const working = ref(false);
const loadError = ref<string | null>(null);
const revertError = ref<string | null>(null);
const runtime = ref<TlsRuntimeStatus | null>(null);
const tls = ref<TlsStatus | null>(null);
const installMode = ref<string | null>(null);
const trustConfirmed = ref(false);
const commandsVisible = ref(false);
/** Set when this plaintext page can no longer read its own status — it went
 *  recovery-only (426) or the server is unreachable — while a switch is known.
 *  The page then explains the switch instead of dead-ending on an error. */
const recovery = ref<{ transition: TlsTransition; recoveryOnly: boolean } | null>(null);
const refreshing = ref(false);
const slowSwitch = ref(false);
let slowTimer: ReturnType<typeof setTimeout> | null = null;
const pivot = useHttpsPivot();
const {
  phase: pivotPhase,
  error: pivotError,
  forwardHint,
  readiness: pivotReadiness,
  recoveryUrl: pivotRecoveryUrl,
} = pivot;
// Top-level bindings: the template only unwraps refs it can see directly.
const backgroundReason = httpsTransitionState.reason;
const backgroundReadiness = httpsTransitionState.readiness;
const backgroundRecoveryUrl = httpsTransitionState.recoveryUrl;
const backgroundInstallMode = httpsTransitionState.installMode;
const backgroundPortForward = httpsTransitionState.portForward;
const migrationReady = migrationReadiness.ready;
const pendingUploads = migrationReadiness.pendingUploads;

const transition = computed(() => runtime.value?.transition ?? null);
/** A switch that already put itself back, and why. */
const autoReverted = computed(() => transition.value?.auto_reverted ?? null);
/** An APP_URL the switch had to correct before it could derive the new origin
 *  from it — the address the agent card and the OAuth callbacks now advertise. */
const appUrlRepaired = computed(() => transition.value?.app_url_repaired ?? null);
/** Ticks only while a confirmation deadline is outstanding, so the countdown
 *  below moves without the page polling anything. */
const nowSeconds = ref(Date.now() / 1000);
let countdownTimer: ReturnType<typeof setInterval> | undefined;
/** How long is left before the switch undoes itself, as "9m 40s", or null when
 *  no deadline applies — a deployment-managed switch, an older server, or one
 *  that has already been confirmed. */
const confirmationCountdown = computed(() => {
  const deadline = transition.value?.confirmation_deadline;
  if (typeof deadline !== 'number' || transition.value?.phase !== 'activating') return null;
  const remaining = Math.max(0, Math.round(deadline - nowSeconds.value));
  if (remaining <= 0) return 'less than a minute';
  const minutes = Math.floor(remaining / 60);
  return minutes ? `${minutes}m ${remaining % 60}s` : `${remaining}s`;
});
watch(confirmationCountdown, (value) => {
  if (value && countdownTimer === undefined) {
    countdownTimer = setInterval(() => { nowSeconds.value = Date.now() / 1000; }, 1000);
  } else if (!value && countdownTimer !== undefined) {
    clearInterval(countdownTimer);
    countdownTimer = undefined;
  }
}, { immediate: true });
onBeforeUnmount(() => {
  if (countdownTimer !== undefined) clearInterval(countdownTimer);
});
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
/** Why the activate button is not pressable, in the user's terms. A disabled
 *  button that explains nothing is indistinguishable from a broken one — which
 *  is exactly how an unfinished upload in another tab used to read. */
const activateBlockedBy = computed(() => {
  if (working.value) return null;
  if (!trustConfirmed.value) {
    return usesLocalCertificate.value
      ? 'Confirm above that you trusted this CA on this device.'
      : 'Confirm above that you verified this certificate on this device.';
  }
  if (!migrationReady.value) {
    return `Waiting for ${pendingUploads.value} file upload(s) in this tab to finish.`;
  }
  return null;
});

// The server decides what to show and in what order, and marks each line a note
// or a command; only commands get a copy button (see DeploymentSteps). There is
// no client-side fallback list any more — guessing at deployment commands here
// was how prose and commands ended up mixed in the first place.
const deploymentSteps = computed(() => runtime.value?.steps ?? []);

// ── recovery guidance ─────────────────────────────────────────────────────
//
// After 45 seconds of an activating switch — whether this page is running the
// restart (the pivot's own hint) or only showing the deployment runbook, where
// nothing on this page is polling — the same help appears: trust, reconnect
// the port-forward, check again, or open the secure address directly.

watch(
  () => (transition.value?.phase === 'activating' ? transition.value.id : null),
  (activatingId) => {
    if (slowTimer) clearTimeout(slowTimer);
    slowTimer = null;
    slowSwitch.value = false;
    if (activatingId) {
      slowTimer = setTimeout(() => { slowSwitch.value = true; }, RECOVERY_HELP_AFTER_MS);
    }
  },
  { immediate: true },
);
onBeforeUnmount(() => { if (slowTimer) clearTimeout(slowTimer); });

const showRecoveryHelp = computed(() => forwardHint.value || slowSwitch.value);
/** The pivot's verdict while it runs; otherwise the background coordinator's. */
const recoveryReadiness = computed(() => pivotReadiness.value ?? backgroundReadiness.value);
const recoveryReason = computed(() => {
  if (pivotReadiness.value) return pivotReadiness.value.ready ? null : pivotReadiness.value.reason;
  return backgroundReason.value ?? (recovery.value ? 'unreachable' : null);
});
const recoveryMessage = computed(() => (
  recoveryReadiness.value?.responded ? recoveryReadiness.value.message : null
));
const recoveryUrl = computed(() => pivotRecoveryUrl.value || backgroundRecoveryUrl.value);
const recoveryCaUrl = computed(() => `${settingsStore.agentUrl.replace(/\/+$/, '')}/ca.pem`);
const recoveryShowsTrust = computed(() => (
  (recovery.value?.transition ?? transition.value)?.certificate_kind ?? runtime.value?.certificate_kind
) === 'local');
const recoveryPortForward = computed(() => (
  tunnelCommand(runtime.value) ?? backgroundPortForward.value ?? null
));
/** Tri-state for the component: unknown still mentions a tunnel, neutrally. */
const recoveryKubernetes = computed(() => {
  const mode = installMode.value ?? runtime.value?.install_mode ?? backgroundInstallMode.value;
  return mode ? mode === 'kubernetes' : null;
});

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/** Read status as the signed-in admin, riding out a rollout blip. */
async function readStatus(): Promise<TlsRuntimeStatus> {
  let failure: unknown = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      // Ask as the signed-in admin. /api/tls/status answers without a token
      // too (the recovery page needs that), but it discloses the Kubernetes
      // identity — and the runbook naming the real namespace, release and
      // Deployment — only to an admin. Polling it anonymously from here is
      // what put the placeholder commands back in front of the one person who
      // shouldn't see them.
      return await fetchTlsStatus(settingsStore.agentUrl, settingsStore.authToken);
    } catch (e) {
      failure = e;
      if (!isTransientTlsFailure(e)) throw e;
      if (attempt < 2) await sleep(500 * (attempt + 1));
    }
  }
  throw failure;
}

/** The switch this browser knows about, when the server cannot say itself. */
function knownTransition(error: unknown): TlsTransition | null {
  const known = httpsTransitionState.transition.value ?? savedHttpsTransition();
  if (!known || known.phase === 'cancelled') return null;
  // 426 is plaintext announcing it became recovery-only: the switch crossed
  // over whatever phase this browser last recorded. Merely unreachable only
  // counts once the switch was already activating.
  if (error instanceof TlsApiError && error.status === 426) return known;
  return isTransientTlsFailure(error) && ['activating', 'active'].includes(known.phase)
    ? known : null;
}

async function load(quiet = false) {
  if (!quiet) loading.value = true;
  loadError.value = null;
  try {
    // A capabilities failure only costs the trust shortcut; never the page.
    // Bounded like one status read, and a timeout counts as such a failure:
    // a server that accepts and never answers would otherwise hold this page
    // on its spinner (or "Check now" disabled) past the status read, so the
    // recovery view never appeared.
    const capsAbort = new AbortController();
    const capsTimer = setTimeout(() => capsAbort.abort(), TLS_STATUS_TIMEOUT_MS);
    const [statusResult, capsResult] = await Promise.allSettled([
      readStatus(),
      fetchServiceCapabilities(settingsStore.agentUrl, settingsStore.authToken, {
        signal: capsAbort.signal,
      }),
    ]);
    clearTimeout(capsTimer);
    if (statusResult.status === 'rejected') {
      const known = knownTransition(statusResult.reason);
      if (!known) throw statusResult.reason;
      recovery.value = {
        transition: known,
        recoveryOnly: statusResult.reason instanceof TlsApiError && statusResult.reason.status === 426,
      };
      // Make sure this tab is (still) looking for the secure address; the
      // coordinator moves it there, with its session, once it answers.
      retryHttpsTransition();
      return;
    }
    recovery.value = null;
    const status = statusResult.value;
    const caps: ServiceCapabilitiesResponse | null = capsResult.status === 'fulfilled'
      ? capsResult.value : null;
    runtime.value = status;
    if (caps) {
      tls.value = caps.tls ?? null;
      trustConfirmed.value = Boolean(caps.tls?.local_trust?.already_trusted);
    }
    installMode.value = caps?.install_mode ?? status.install_mode ?? installMode.value;
    if (status.transition && ['prepared', 'quiescing'].includes(status.transition.phase)) {
      await handleHttpsTransition(status.transition, settingsStore.agentUrl, settingsStore.authToken);
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : 'Failed to load HTTPS status';
  } finally {
    loading.value = false;
  }
}

/** "Check now": wake whichever wait is running, and re-read this page. */
async function retryRecovery() {
  pivot.retryNow();
  retryHttpsTransition();
  if (pivotPhase.value !== 'idle' && pivotPhase.value !== 'failed') return;
  refreshing.value = true;
  try {
    await load(true);
  } finally {
    refreshing.value = false;
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
    // Reached on every exit, including a run superseded by another click or by
    // leaving the page — neither of which reports an outcome.
    onSettled: () => {
      working.value = false;
    },
    // The message is not stored here: the card renders ``pivotError``, which
    // already holds it. ``loadError`` would show the same text a second time,
    // above the page's opening section — off screen for anyone reading the step
    // they just acted on.
    onFailure: () => {
      working.value = false;
      // Same reason as in load(): a failed activation is exactly when the
      // operator needs the runbook to name their own cluster.
      void fetchTlsStatus(settingsStore.agentUrl, settingsStore.authToken).then((status) => {
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
  revertError.value = null;
  try {
    runtime.value = await cancelHttps(
      settingsStore.agentUrl,
      settingsStore.authToken,
      transition.value.id,
    );
    await window.cremind?.server?.releaseHttpsMigration?.();
    trustConfirmed.value = false;
    commandsVisible.value = false;
    // A cancel that could not put the previous settings back is still a
    // cancel, but the operator has to know before the next restart.
    revertError.value = runtime.value.revert_error ?? null;
    ElMessage.success('HTTPS switch cancelled. Cremind stays on HTTP.');
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    working.value = false;
  }
}

onMounted(() => { void load(); });
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
      <!-- The status read failed, but this browser knows a switch is under way:
           plaintext went recovery-only, or the server is mid-restart. Explain
           and offer the way across instead of a dead-end error card. -->
      <section v-else-if="recovery" class="state-card">
        <h2>{{ recovery.recoveryOnly
          ? 'Cremind has moved to HTTPS'
          : 'Cremind is not answering on this address right now' }}</h2>
        <p v-if="recovery.recoveryOnly">
          This plaintext address now serves only the recovery page, so this page cannot
          read its settings here any more. Continue on the secure address — this tab moves
          there on its own, keeping its page and session, as soon as this browser can
          reach it.
        </p>
        <p v-else>
          The server did not answer this page. That is expected for a moment while it
          restarts into HTTPS or a replaced pod comes up. This tab moves to the secure
          address on its own, keeping its page and session, once it answers.
        </p>
        <HttpsRecoveryHelp
          :https-url="recoveryUrl"
          :ca-url="recoveryCaUrl"
          :show-trust="recoveryShowsTrust"
          :port-forward="recoveryPortForward"
          :kubernetes="recoveryKubernetes"
          :reason="recoveryReason"
          :message="recoveryMessage"
          :busy="refreshing"
          @retry="retryRecovery"
        />
      </section>
      <div v-else-if="loadError && !runtime" class="state-card error-card">
        <h2>Could not read HTTPS status</h2>
        <p>{{ loadError }}</p>
        <button class="secondary-btn" @click="load()">Try again</button>
      </div>

      <template v-else>
        <!-- The switch already put itself back. Without this the page shows the
             ordinary "switch to HTTPS" wizard, which reads as though the attempt
             never happened and invites the user straight back into it. -->
        <section v-if="autoReverted" class="state-card reverted-card">
          <h2>HTTPS was switched off automatically</h2>
          <p>{{ autoReverted.reason }}</p>
          <p v-if="autoReverted.restored === false" class="inline-error">
            The previous settings could not be restored. Check CREMIND_SSL and APP_URL in
            the system directory's .env before restarting this server.
          </p>
          <p v-else>
            Cremind is serving HTTP again and nothing was lost — no sessions were
            invalidated. You can fix the cause and try the switch again below.
          </p>
        </section>

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
        <!-- Above the card branches on purpose: once the server serves HTTPS the
             success card wins, and a switch that could not finish would have
             nowhere left to report itself. -->
        <div v-if="runtime?.activation_error" class="inline-error">{{ runtime.activation_error }}</div>
        <div v-if="revertError" class="inline-error">{{ revertError }}</div>

        <section v-if="hasCertificateError" class="state-card error-card">
          <Icon icon="mdi:certificate-alert-outline" class="large-icon" />
          <div>
            <h2>HTTPS certificate needs attention</h2>
            <p>{{ runtime?.certificate_error || 'The configured certificate is not ready for this address.' }}</p>
            <p>
              Keep the plaintext recovery page open and replace or renew the certificate so
              it covers this hostname, then follow the steps below and verify the HTTPS
              address again.
            </p>
            <DeploymentSteps :steps="deploymentSteps" />
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
            <p v-if="activateBlockedBy" class="upload-wait">{{ activateBlockedBy }}</p>
            <!-- The wait before the first request (other tabs finishing their
                 uploads and saving their sessions) is silent on the network, so
                 it has to be loud here: without it this card looks unchanged and
                 the button looks broken. -->
            <p v-else-if="working" class="upload-wait">
              {{ pivotPhase === 'preparing'
                ? 'Letting the other Cremind tabs in this browser finish up…'
                : 'Preparing the switch…' }}
            </p>
            <p v-if="pivotError" class="inline-error">{{ pivotError }}</p>
            <div class="actions">
              <button class="secondary-btn" :disabled="working" @click="cancel">Cancel</button>
              <button class="primary-btn" :disabled="!canActivate" @click="activate">
                <Icon :icon="working ? 'mdi:loading' : 'mdi:lock-outline'" :class="{ spin: working }" />
                {{ working ? 'Working…' : (isExternal ? 'Show deployment commands' : 'Activate HTTPS') }}
              </button>
            </div>
          </section>

          <section v-else class="state-card">
            <!-- Outside both panes on purpose. A self-applied switch is native
                 and restart-scheduled, so it renders the spinner pane below,
                 never the runbook pane — and this notice is the only thing that
                 tells its administrator the wait has a deadline and an
                 automatic way back. It self-gates on the deadline. -->
            <p v-if="confirmationCountdown" class="notice">
              Nothing has been switched over yet — your sessions are untouched and this
              page still works. Open <strong>{{ recoveryUrl }}</strong> and sign in to
              finish the switch. If no browser reaches it in
              {{ confirmationCountdown }}, Cremind restores the previous settings and
              returns to HTTP on its own.
            </p>
            <!-- Also outside both panes: the value the agent card, the Google
                 redirect and the Atlassian callback now advertise is not the one
                 this installation was configured with, and nothing else says so. -->
            <p v-if="appUrlRepaired" class="notice">
              <template v-if="appUrlRepaired.from">
                The configured address <strong>{{ appUrlRepaired.from }}</strong> named the
                internal API port, which listens on this machine only and no browser can
                open.
              </template>
              <template v-else>This deployment had no public address configured.</template>
              Cremind used <strong>{{ appUrlRepaired.to }}</strong> instead — that is the
              public address now advertised by account linking and the agent card.
              Cancelling this switch puts the old value back.
            </p>
            <template v-if="isExternal || commandsVisible || runtime?.restart_supported === false">
              <h2>{{ isExternal ? 'Apply HTTPS to the deployment' : 'Restart Cremind to finish' }}</h2>
              <p v-if="isExternal">
                HTTPS is prepared and this server is waiting for the deployment change. Read
                the notes in order, then run each command from the deployment host; only the
                highlighted command boxes are meant to be pasted into a terminal.
              </p>
              <p v-else>
                HTTPS settings and session handoffs are saved. Follow the steps below from
                this installation; keep this page open while the secure listener starts.
              </p>
              <!-- Manual activation never reaches the 'waiting' phase — enterManualMode
                   leaves it at 'manual' — and after a reload the pivot is idle, which is
                   exactly the case this page most needs to explain. -->
              <p
                v-if="transition?.awaiting_operator || pivotPhase === 'waiting' || pivotPhase === 'manual'"
                class="notice"
              >
                Cremind checks the HTTPS address in the background, so you can leave this
                page and keep using Cremind meanwhile. Every browser tab and Electron
                window moves on its own once the secure server answers; a session older
                than ten minutes signs in again there. To stay on HTTP instead, cancel the
                switch below.
              </p>
              <p v-if="pivotError" class="inline-error">{{ pivotError }}</p>
              <DeploymentSteps :steps="deploymentSteps" />
              <HttpsRecoveryHelp
                v-if="showRecoveryHelp"
                :https-url="recoveryUrl"
                :ca-url="recoveryCaUrl"
                :show-trust="recoveryShowsTrust"
                :port-forward="recoveryPortForward"
                :kubernetes="recoveryKubernetes"
                :reason="recoveryReason"
                :message="recoveryMessage"
                :busy="refreshing"
                @retry="retryRecovery"
              />
              <div v-if="runtime?.can_cancel" class="actions">
                <button class="secondary-btn" :disabled="working" @click="cancel">
                  Cancel HTTPS switch
                </button>
              </div>
            </template>
            <template v-else>
              <div class="spinner"></div>
              <h2>{{ transition?.phase === 'quiescing'
                ? 'Waiting for open tabs…'
                : pivotPhase === 'manual' ? 'Restart Cremind to finish' : 'Switching to HTTPS…' }}</h2>
              <p>{{ pivotError || (transition?.phase === 'quiescing'
                ? `${runtime?.quiesce_pending ?? 'Some'} tab(s) are finishing uploads and saving private session handoffs before the restart.`
                : 'Every open Cremind tab will reopen at its current page after the secure listener is verified.') }}</p>
              <HttpsRecoveryHelp
                v-if="showRecoveryHelp && transition?.phase !== 'quiescing'"
                :https-url="recoveryUrl"
                :ca-url="recoveryCaUrl"
                :show-trust="recoveryShowsTrust"
                :port-forward="recoveryPortForward"
                :kubernetes="recoveryKubernetes"
                :reason="recoveryReason"
                :message="recoveryMessage"
                :busy="refreshing"
                @retry="retryRecovery"
              />
              <button
                v-if="runtime?.can_cancel"
                class="secondary-btn"
                :disabled="working"
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
/* App tokens with an explicit colour: the Element Plus warning shades this
   used before are never redeclared for the dark theme and stayed light there. */
.inline-error, .upload-wait, .notice { padding: 10px 14px; border-radius: 6px; background: var(--hover-bg); color: var(--text-primary); border-left: 3px solid var(--warning-color); margin-bottom: 14px; }
.error-card, .inline-error { border-color: var(--danger-color); }
/* A reversal is not an error — the installation recovered itself — so it gets
   the warning edge rather than the danger one, and sits above the wizard it
   would otherwise be silently inviting the user back into. */
.reverted-card { border-color: var(--warning-color); }
code { background: var(--hover-bg); padding: 2px 5px; border-radius: 4px; }
.fingerprint { word-break: break-all; }
.spinner { width: 30px; height: 30px; border: 3px solid var(--border-color); border-top-color: var(--primary-color); border-radius: 50%; animation: spin .9s linear infinite; }
.spin { animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (max-width: 700px) { .steps { grid-template-columns: 1fr; }.benefits-card { display: block; }.benefit-icon { margin-bottom: 8px; } }
</style>
