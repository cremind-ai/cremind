// The installer's "I already trusted this CA" hand-off.
//
// A Docker or Kubernetes install's server lives in the container, so it can
// only ever read the container's trust store — never the host store the
// browser consults (``_trust_environment_error`` in app/api/tls.py refuses
// outright there, which forces ``local_trust.already_trusted`` to null). The
// installer runs ON the host and writes that store itself, so it is the only
// witness. It reports what it did by opening the wizard at
// ``#/setup?ca_trusted=<sha256>``.
//
// The hint names a specific CA rather than saying a bare "yes" so that a
// stale link, or a re-install that regenerated the CA, cannot vouch for a
// certificate this server no longer serves. It is not a credential and not a
// security boundary — the checkbox it stands in for is a consent moment, not
// a control — but it must never assert something false, so the comparison is
// exact: the whole digest, or nothing.
//
// ``resolveTrustEvidence`` below is the wider rule this hint feeds into.

import type { TrustEvidence } from './configApi';

/** Hex-only, lowercase. ``ca_sha256`` arrives colon-separated and uppercase
 *  (app/config/tls_auto.py), the installers emit it colon-free; normalising
 *  both sides means neither has to care. A non-string — a repeated query
 *  param reaches Vue Router as ``string[]`` — normalises to '' and is
 *  refused by the length check below. */
function hexOnly(value: unknown): string {
  return typeof value === 'string' ? value.replace(/[^0-9a-fA-F]/g, '').toLowerCase() : '';
}

/** SHA-256, as hex characters. */
const DIGEST_CHARS = 64;

/**
 * Why this device is already covered, or ``null`` to ask the user.
 *
 * One rule in one place, evaluated by whoever owns the gate rather than by
 * the panel that renders it: the panel is a child, so anything it discovered
 * during its own setup would arrive a render too late and flash the "trust is
 * required" alert on a step that is in fact already satisfied.
 *
 * Ordered strongest first. Note what is NOT here: none of these proves that
 * every *browser* on the device trusts the CA — Firefox keeps its own store —
 * so the manual commands are always still one click away.
 */
export function resolveTrustEvidence(input: {
  /** A ``POST /api/tls/trust`` succeeded in this session. */
  justTrusted?: boolean;
  /** ``local_trust.already_trusted``: the server looked in this machine's
   *  store and found it. Definitive only on Windows; ``null`` elsewhere
   *  means unknown, never "no". */
  serverSaysTrusted?: boolean | null;
  /** The installer that opened this page named this exact CA. */
  installerTrusted?: boolean;
  /** This is the Electron renderer, which is handed the CA directly. */
  isElectron?: boolean;
  /** ``INSTALL_MODE``. */
  installMode?: string | null;
  /** ``tls.mode`` — 'custom' means an operator supplied the certificate and
   *  there is no local CA in play. */
  tlsMode?: string | null;
}): TrustEvidence | null {
  if (input.justTrusted) return 'just-trusted';
  if (input.serverSaysTrusted === true) return 'already';
  if (input.installerTrusted) return 'installer';
  // Chromium verifies the chain in-process for the desktop window
  // (``setCertificateVerifyProc`` in ui/electron/main.ts). Mirror that
  // function's bail conditions exactly, or this vouches for a window that is
  // in fact still warning.
  const container = ['docker', 'kubernetes'].includes((input.installMode ?? '').toLowerCase());
  if (input.isElectron && !container && input.tlsMode !== 'custom') return 'electron';
  return null;
}

/**
 * Does the installer's ``?ca_trusted=`` hint name the CA this server serves?
 *
 * Requires the full digest on both sides. A prefix match would be cheaper to
 * produce but would leave "how many characters is enough" as a judgement call
 * in a certificate flow, and there is nothing to gain: the installer has the
 * whole thing in hand at the moment it writes the store.
 */
export function matchesCaTrustHint(
  hint: unknown,
  caSha256: string | null | undefined,
): boolean {
  const given = hexOnly(hint);
  const actual = hexOnly(caSha256);
  if (actual.length !== DIGEST_CHARS) return false;
  return given === actual;
}
