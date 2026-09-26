/**
 * When Settings → My Documents can be reached.
 *
 * My Documents needs Vector Embedding on — the admin turns it on under
 * Settings → Vector Embedding. While it is off, every way in is closed for
 * every profile: the card on the Settings list, the NavRail sync chip and the
 * route itself (a bookmark lands on the Settings list), and a page that is
 * open when it goes off leaves. What the agent does meanwhile is the
 * server's business (a kept index stays searchable by keyword).
 *
 * The embedding store's `enabled` reads false until its first snapshot, so
 * every rule here takes the "known yet" flag too and never reads that default
 * as "off". Pure, so node:test covers it (tests/my-documents-access.test.mjs).
 */

/** What the embedding store knows (`useEmbeddingStatusStore`). */
export interface EmbeddingKnowledge {
  /** A snapshot has landed. */
  known: boolean;
  /** Vector Embedding is on (meaningful only once `known`). */
  enabled: boolean;
}

/** Whether My Documents is open: true or false once the embedding state is
 *  known, null before. */
export function myDocumentsOpen(embedding: EmbeddingKnowledge): boolean | null {
  return embedding.known ? embedding.enabled : null;
}

/** The Settings list, where a closed My Documents sends people. */
export function settingsListPath(profile: string): string {
  return `/${profile}/settings`;
}

/**
 * The route guard's answer for My Documents, given what the embedding store's
 * `whenKnown()` resolved to. Only a known "off" redirects: when the state did
 * not arrive in time the page opens, and leaves on its own once it arrives
 * and says off.
 */
export function myDocumentsRouteDecision(
  enabled: boolean | null,
  profile: string,
): true | { path: string; replace: true } {
  return enabled === false ? { path: settingsListPath(profile), replace: true } : true;
}

/** Why the page closed (or never opened). `wasOpen`: the viewer had it open
 *  with Vector Embedding on, so it was just turned off. */
export function myDocumentsClosedMessage(wasOpen: boolean): string {
  return wasOpen
    ? 'My Documents needs Vector Embedding, which was turned off.'
    : 'My Documents needs Vector Embedding, which is off.';
}

/** Who may see a card on the Settings list. */
export interface SettingsCardRule {
  /** Only the admin profile. */
  adminOnly?: boolean;
  /** Only while Vector Embedding is on (My Documents). */
  requiresEmbedding?: boolean;
}

/** The Settings list's cards for this viewer. A card that needs embedding
 *  stays hidden until the state is known, so it never shows and then vanishes. */
export function visibleSettingsCards<T extends SettingsCardRule>(
  cards: readonly T[],
  viewer: { isAdmin: boolean; embedding: EmbeddingKnowledge },
): T[] {
  const embeddingOn = myDocumentsOpen(viewer.embedding) === true;
  return cards.filter(card => (!card.adminOnly || viewer.isAdmin)
    && (!card.requiresEmbedding || embeddingOn));
}

/** The NavRail sync chip: something to show, and a page to open it on. */
export function documentsChipVisible(
  embedding: EmbeddingKnowledge,
  sync: { isActive: boolean; needsAttention: boolean },
): boolean {
  return myDocumentsOpen(embedding) === true && (sync.isActive || sync.needsAttention);
}
