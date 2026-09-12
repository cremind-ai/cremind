// One definition of "the HTTPS address is ready for this switch", shared by
// the page that started the switch (Settings / first-run setup, through
// useHttpsPivot) and every other tab that only follows it (httpsTransition).
//
// Readiness is identity first: the answer must come from the same Cremind
// installation, for the same transition, at the same source/target origins and
// public port, with a certificate this browser was asked to trust. Only then
// does the phase decide between "move now", "still finishing", and "answering
// but broken".
//
// Why a generated certificate is pinned by its CA and not its leaf
// ----------------------------------------------------------------------
// Cremind's generated ("local") leaf is disposable by design. `ensure_local_tls`
// re-issues it — under the same, already-trusted CA — whenever the serving
// hostname or pod IP is missing from its SAN set, and a replaced Kubernetes pod
// or Docker container always has a new one. `mark_active` then republishes the
// transition with the renewed leaf fingerprint. A browser that had pinned the
// leaf it saw at preparation time therefore rejected the very server it was
// waiting for, forever, on the most ordinary rollout there is. What the user
// actually trusted is the CA, so the CA is the identity to pin; the TLS
// handshake itself already proves the leaf chains to a trusted root.
//
// A supplied ("custom") certificate stays pinned by its leaf: there is no
// Cremind CA behind it, the operator compared that exact fingerprint before
// activating, and it only changes when the operator changes it — which should
// be re-verified, not silently followed. An edge-terminated ("external") HTTPS
// address has no fingerprint to pin at all; its public chain is the browser's
// business.

import {
  probeTlsStatus,
  TlsApiError,
  TlsTimeoutError,
  type TlsRuntimeStatus,
  type TlsTransition,
} from './configApi';

export type HttpsReadinessReason =
  | 'ready'
  | 'unreachable'
  | 'timeout'
  | 'not-serving-https'
  | 'certificate-invalid'
  | 'activation-failed'
  | 'activation-pending'
  | 'transition-mismatch'
  | 'certificate-mismatch';

export interface HttpsReadiness {
  ready: boolean;
  reason: HttpsReadinessReason;
  /** The secure address answered. False means fetch never got a response:
   *  an untrusted certificate, a server still starting, a dead port-forward —
   *  indistinguishable from a page, and exactly what recovery guidance is for. */
  responded: boolean;
  /** Why it is not ready, in showable words (the server's own where it gave one). */
  message: string | null;
  /** The target's transition as reported (localized), when it answered. */
  transition?: TlsTransition | null;
}

type Localize = (transition: TlsTransition) => TlsTransition;
const asIs: Localize = transition => transition;

/** Whether `target` presents the certificate identity `pinned` was trusted by.
 *
 * See the header: generated certificates match by CA (a renewed leaf under the
 * same CA is the same trust), supplied certificates by their exact leaf, and
 * edge-managed HTTPS by nothing. Any other kind — including a transition from
 * a backend too old to state one — keeps the original strict comparison. */
export function sameCertificateAuthority(pinned: TlsTransition, target: TlsTransition): boolean {
  switch (pinned.certificate_kind) {
    case 'local':
      if (pinned.ca_sha256) return target.ca_sha256 === pinned.ca_sha256;
      // A local switch always has a CA; without one, fall back to the leaf.
      return target.certificate_sha256 === pinned.certificate_sha256
        && target.ca_sha256 === pinned.ca_sha256;
    case 'custom':
      return Boolean(pinned.certificate_sha256)
        && target.certificate_sha256 === pinned.certificate_sha256;
    case 'external':
      return true;
    default:
      return target.certificate_sha256 === pinned.certificate_sha256
        && target.ca_sha256 === pinned.ca_sha256;
  }
}

function notReady(
  reason: HttpsReadinessReason,
  message: string | null,
  transition: TlsTransition | null,
  responded = true,
): HttpsReadiness {
  return { ready: false, reason, responded, message, transition };
}

/** Judge one `/api/tls/status` answer from the target origin against the
 * transition this browser pinned. `localize` maps announced origins onto the
 * hostname/port this browser uses (see httpsTransition's `localTransition`);
 * both sides are passed through it so a pinned value is compared like-for-like. */
export function evaluateHttpsReadiness(
  pinned: TlsTransition,
  status: TlsRuntimeStatus,
  localize: Localize = asIs,
): HttpsReadiness {
  const expected = localize(pinned);
  const target = status.transition ? localize(status.transition) : null;
  if (!status.serving_https) {
    return notReady('not-serving-https',
      'The secure address answered, but not as the HTTPS server.', target);
  }
  if (status.instance_id !== expected.instance_id || !target
    || target.instance_id !== expected.instance_id || target.id !== expected.id) {
    return notReady('transition-mismatch',
      'The secure address answers for a different Cremind installation or HTTPS switch.', target);
  }
  if (target.source_origin !== expected.source_origin
    || target.target_origin !== expected.target_origin
    || target.same_public_port !== expected.same_public_port
    || target.public_port !== expected.public_port) {
    return notReady('transition-mismatch',
      'The secure address answers, but for a different address or port than this switch prepared.', target);
  }
  if (target.certificate_kind !== expected.certificate_kind
    || !sameCertificateAuthority(expected, target)) {
    return notReady('certificate-mismatch',
      'The secure server presents a different certificate than the one this switch prepared.', target);
  }
  if (target.phase === 'active') {
    if (status.ready === false) {
      return notReady('certificate-invalid',
        status.certificate_error || 'The secure server\'s certificate is not valid for this address.', target);
    }
    return { ready: true, reason: 'ready', responded: true, message: null, transition: target };
  }
  if (target.phase === 'activating') {
    const failure = target.activation_error || status.activation_error;
    if (failure) return notReady('activation-failed', failure, target);
    // A self-applied switch deliberately holds the credential boundary until a
    // real client reaches HTTPS, so it stays `activating` until one does — and
    // moving this tab there (redeeming its ticket, or signing in) is precisely
    // what produces the confirmation. Waiting for `active` here would wait for
    // something only arriving can cause, and the switch would then revert at
    // its deadline with every tab still sitting on the old origin.
    //
    // This probe is the proof the server cannot gather for itself: our own
    // fetch to the target completed, so this browser reached the address and
    // trusted the certificate. `confirmation_deadline` is present only in that
    // state, so a deployment-managed switch keeps `activation-pending`.
    if (typeof target.confirmation_deadline === 'number') {
      if (status.ready === false) {
        return notReady('certificate-invalid',
          status.certificate_error || 'The secure server\'s certificate is not valid for this address.', target);
      }
      return { ready: true, reason: 'ready', responded: true, message: null, transition: target };
    }
    return notReady('activation-pending',
      'The secure server answers and is finishing the switch.', target);
  }
  return notReady('transition-mismatch',
    target.phase === 'cancelled'
      ? 'This HTTPS switch was cancelled.'
      : 'The secure server has not started this HTTPS switch.', target);
}

export interface HttpsReadinessProbeOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
  localize?: Localize;
}

/** Probe the pinned target origin once and judge the answer.
 *
 * Never throws: a request that fails or runs out of time is itself a
 * readiness result (`unreachable` / `timeout`), which is what the waiting
 * loops need in order to explain themselves after 45 seconds. No credential
 * is sent — the target is learned from transition metadata. */
export async function probeHttpsReadiness(
  pinned: TlsTransition,
  options: HttpsReadinessProbeOptions = {},
): Promise<HttpsReadiness> {
  const localize = options.localize ?? asIs;
  try {
    const status = await probeTlsStatus(localize(pinned).target_origin, {
      signal: options.signal,
      timeoutMs: options.timeoutMs,
    });
    return evaluateHttpsReadiness(pinned, status, localize);
  } catch (error) {
    if (error instanceof TlsTimeoutError) {
      return notReady('timeout', 'The secure address did not answer in time.', null, false);
    }
    return notReady(
      'unreachable',
      error instanceof TlsApiError ? error.message : null,
      null,
      false,
    );
  }
}

/** The tunnel line to show someone whose port-forward died with the old pod.
 *
 * The runbook's own port-forward step comes first: it keeps the local port this
 * browser actually uses (a tab on https://localhost:8080 is told 8080:80),
 * matching the cremind.appUrl the same runbook sets. The identity block's
 * ``port_forward`` always names the documented 1515, so it is only the
 * fallback. A step still carrying <namespace>/<release> placeholders is not
 * runnable, so it never wins over a filled-in identity line. */
export function tunnelCommand(status: TlsRuntimeStatus | null | undefined): string | null {
  const step = status?.steps?.find(item => (
    item.kind === 'command' && / port-forward /.test(item.text) && !item.text.includes('<')
  ));
  return step?.text ?? status?.kubernetes?.port_forward ?? null;
}

/** Short guidance for a tab that has waited too long, per reason. */
export function httpsReadinessAdvice(readiness: HttpsReadiness | null): string {
  switch (readiness?.reason) {
    case 'activation-failed':
      return `The secure server answers, but activation failed: ${readiness.message ?? 'see the server log.'}`;
    case 'certificate-invalid':
      return readiness.message ?? 'The secure server\'s certificate is not valid for this address.';
    case 'activation-pending':
      return 'The secure server answers and is finishing the switch. This tab moves as soon as it is done.';
    case 'certificate-mismatch':
    case 'transition-mismatch':
    case 'not-serving-https':
      return readiness.message ?? 'The secure address answers for something other than this HTTPS switch.';
    default:
      return 'The secure address is not answering from this browser yet. Trust the Cremind certificate on this device, reopen the Kubernetes port-forward if you use one, or check that the rollout finished.';
  }
}
