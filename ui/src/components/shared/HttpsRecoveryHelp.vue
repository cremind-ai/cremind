<script setup lang="ts">
// What to do when an HTTPS switch has been waiting too long — shown after 45
// seconds by Settings → HTTPS (both the runbook and the restart spinner), by
// the first-run setup pivot, and by the overlay every other tab raises.
//
// The one distinction that matters to the reader: did the secure address
// answer at all? If it did not, the fix is on this side — trust the CA, bring
// the port-forward back after the pod was replaced, or wait for the rollout —
// and those steps are listed. If it answered and reported that activation
// failed, none of that helps; the server's own reason is shown instead. Either
// way the page keeps checking, "Check again" checks now, and the explicit link
// opens the secure address (carrying this tab's one-use handoff ticket when it
// still has one) for anyone who would rather accept a certificate warning than
// wait.
import { computed } from 'vue';
import { ElMessage } from 'element-plus';
import { Icon } from '@iconify/vue';

import { useCopyToClipboard } from '../../composables/useCopyToClipboard';

const props = withDefaults(defineProps<{
  /** Where the explicit link goes — a handoff or login URL on the HTTPS origin. */
  httpsUrl: string;
  /** The CA download on the plaintext origin (``/ca.pem`` is still served by
   *  the recovery listener after plaintext stops serving the application). */
  caUrl?: string | null;
  /** Only a generated certificate has a Cremind CA to trust. */
  showTrust?: boolean;
  /** Ready-to-paste reconnect line, when the server told an admin its names. */
  portForward?: string | null;
  /** true: a Kubernetes install; false: known not to be one; null/undefined:
   *  unknown, which still mentions a tunnel in neutral words. */
  kubernetes?: boolean | null;
  /** An `HttpsReadiness['reason']` from the last probe. */
  reason?: string | null;
  /** The server's own words, when it answered. */
  message?: string | null;
  busy?: boolean;
}>(), {
  caUrl: null,
  showTrust: false,
  portForward: null,
  kubernetes: null,
  reason: null,
  message: null,
  busy: false,
});

const emit = defineEmits<{ retry: [] }>();

const { copy, isCopied } = useCopyToClipboard();
async function copyForward(text: string) {
  if (!(await copy(text, 'forward'))) ElMessage.error('Could not copy the command');
}

/** The secure address answered: nothing on this side of the connection is
 *  what is wrong, so the trust and tunnel steps would only mislead. */
const answered = computed(() => [
  'activation-failed',
  'activation-pending',
  'certificate-invalid',
  'certificate-mismatch',
  'transition-mismatch',
  'not-serving-https',
].includes(props.reason ?? ''));

const headline = computed(() => {
  switch (props.reason) {
    case 'activation-failed':
      return 'The secure server answers, but activation failed';
    case 'activation-pending':
      return 'The secure server answers and is finishing the switch';
    case 'certificate-invalid':
      return 'The secure server answers, but its certificate is not valid here';
    case 'certificate-mismatch':
      return 'The secure server presents a different certificate';
    case 'transition-mismatch':
    case 'not-serving-https':
      return 'Something else answers on the secure address';
    case 'timeout':
      return 'The secure address is not answering';
    default:
      return 'This browser cannot reach the secure address yet';
  }
});

const detail = computed(() => {
  if (props.message) return props.message;
  if (answered.value) return null;
  return 'A certificate this device does not trust yet, a server that is still starting, and '
    + 'a port-forward that ended when the pod was replaced all look exactly the same from this page.';
});

/** Show the origin only — never the one-use ticket the link may carry. */
const displayUrl = computed(() => {
  try { return new URL(props.httpsUrl).origin; } catch { return props.httpsUrl; }
});

const showForward = computed(() => !answered.value
  && (Boolean(props.portForward) || props.kubernetes !== false));
</script>

<template>
  <div class="https-recovery-help" :class="{ answered }" role="status">
    <p class="headline">
      <Icon :icon="answered ? 'mdi:alert-circle-outline' : 'mdi:lan-disconnect'" class="headline-icon" />
      <span>{{ headline }}</span>
    </p>
    <p v-if="detail" class="detail">{{ detail }}</p>

    <ol class="recovery-steps">
      <li v-if="showTrust && !answered">
        <strong>Trust the Cremind certificate on this device.</strong>
        <template v-if="caUrl">
          Download <a :href="caUrl" download class="recovery-link">cremind-local-ca.pem</a>,
        </template>
        <template v-else>Download the Cremind CA,</template>
        compare its fingerprint with the one Settings showed, add it to this device's
        trusted roots, then restart the browser.
      </li>
      <li v-if="showForward">
        <template v-if="portForward">
          <strong>Reopen the port-forward.</strong>
          Replacing the pod ends it; run it again in your terminal:
          <span class="command-box">
            <code class="command">{{ portForward }}</code>
            <button
              type="button"
              class="copy-icon-btn"
              :class="{ copied: isCopied('forward') }"
              :title="isCopied('forward') ? 'Copied!' : 'Copy command'"
              aria-label="Copy the port-forward command"
              @click="copyForward(portForward)"
            ><Icon :icon="isCopied('forward') ? 'mdi:check' : 'mdi:content-copy'" /></button>
          </span>
        </template>
        <template v-else-if="kubernetes">
          <strong>Reopen the port-forward.</strong>
          The rollout replaced the pod, which ends <code>kubectl port-forward</code>; run
          the same command again in your terminal.
        </template>
        <template v-else>
          <strong>Reconnect any tunnel.</strong>
          If you reach Cremind through <code>kubectl port-forward</code> or another tunnel,
          run it again: replacing the server ends it.
        </template>
      </li>
      <li>
        <strong>Check again.</strong>
        This page keeps checking and moves on its own the moment the secure address
        answers.
        <button type="button" class="recovery-btn" :disabled="busy" @click="emit('retry')">
          <Icon :icon="busy ? 'mdi:loading' : 'mdi:refresh'" :class="{ spin: busy }" />
          Check now
        </button>
      </li>
      <li v-if="httpsUrl">
        <strong>Or open it yourself.</strong>
        <a :href="httpsUrl" target="_self" class="recovery-link">Open {{ displayUrl }}</a>
        <template v-if="!answered">
          and continue past the browser's certificate warning if one appears.
        </template>
      </li>
    </ol>
  </div>
</template>

<style scoped>
/* App tokens only, with explicit text colours: an Element Plus variable this
   app never redeclares would stay on its light default in the dark theme. */
.https-recovery-help {
  margin: 14px 0;
  padding: 14px 16px;
  border: 1px solid var(--border-color);
  border-left: 3px solid var(--warning-color);
  border-radius: 8px;
  background: var(--surface-color);
  color: var(--text-primary);
  text-align: left;
}
.https-recovery-help.answered { border-left-color: var(--danger-color); }
.headline {
  display: flex; align-items: center; gap: 8px;
  margin: 0 0 6px; font-weight: 600; color: var(--text-primary);
}
.headline-icon { flex: none; font-size: 1.15rem; color: var(--warning-color); }
.answered .headline-icon { color: var(--danger-color); }
.detail {
  margin: 0 0 8px; color: var(--text-secondary);
  font-size: 0.875rem; line-height: 1.55; word-break: break-word;
}
.recovery-steps {
  margin: 0; padding-left: 1.25em;
  color: var(--text-secondary); font-size: 0.875rem; line-height: 1.6;
}
.recovery-steps li + li { margin-top: 8px; }
.recovery-steps strong { color: var(--text-primary); font-weight: 600; }
.recovery-link { color: var(--primary-color); font-weight: 600; word-break: break-all; }
.recovery-steps code {
  background: var(--hover-bg); color: var(--text-primary);
  padding: 1px 5px; border-radius: 4px; font-size: 0.85em;
}
.command-box {
  display: flex; align-items: flex-start; gap: 8px;
  margin: 6px 0 2px; padding: 8px 10px;
  background: var(--hover-bg); border: 1px solid var(--border-color); border-radius: 6px;
}
.command-box .command {
  flex: 1; background: none; padding: 0;
  font-family: monospace; font-size: 0.8rem; color: var(--text-primary);
  white-space: pre-wrap; word-break: break-word;
}
.copy-icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  background: none; border: none; cursor: pointer;
  padding: 1px; border-radius: 4px; font-size: 0.85rem;
  color: var(--text-secondary);
}
.copy-icon-btn:hover { color: var(--primary-color); }
.copy-icon-btn.copied { color: var(--success-color); }
.recovery-btn {
  display: inline-flex; align-items: center; gap: 6px;
  margin-left: 6px; padding: 4px 10px;
  border: 1px solid var(--border-color); border-radius: 6px;
  background: transparent; color: var(--text-primary);
  font-weight: 600; font-size: 0.8rem; cursor: pointer;
}
.recovery-btn:hover:not(:disabled) { border-color: var(--primary-color); color: var(--primary-color); }
.recovery-btn:disabled { opacity: .55; cursor: not-allowed; }
.spin { animation: recovery-spin .9s linear infinite; }
@keyframes recovery-spin { to { transform: rotate(360deg); } }
</style>
