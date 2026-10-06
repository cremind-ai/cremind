import type { ServiceCapabilitiesResponse } from './configApi';

/**
 * Which optional features this server's computer can't install, read from
 * `/api/services/capabilities` (`unavailable_features`).
 *
 * Vector Embedding is the case that needs it: both models run on PyTorch,
 * which publishes no build for an Intel Mac (or Windows on ARM). Offering the
 * switch there only led to a pip install that could not succeed, and on the
 * Setup Wizard it failed every other feature with it. Kept out of the form
 * component so the rule can be tested without mounting it.
 */

/** The embedding models the Vector Embedding form offers, as provider ids. */
export const EMBEDDING_PROVIDERS = ['me5', 'gemma'] as const;

/** Why this server can't install embedding model `provider`, or null if it can
 *  (or the server predates the field). */
export function embeddingProviderUnavailable(
  caps: ServiceCapabilitiesResponse | null | undefined,
  provider: string,
): string | null {
  return caps?.unavailable_features?.[`embedding.${provider}`] ?? null;
}

/** Why Vector Embedding can't be turned on at all here — every model it offers
 *  is unavailable — or null while any one of them can be installed. */
export function embeddingUnavailable(
  caps: ServiceCapabilitiesResponse | null | undefined,
): string | null {
  const reasons = EMBEDDING_PROVIDERS.map((provider) => embeddingProviderUnavailable(caps, provider));
  if (reasons.some((reason) => reason === null)) return null;
  // Both models share one cause today; say it once.
  return [...new Set(reasons)].join(' ');
}
