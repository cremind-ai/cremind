/**
 * Citation items for answers that arrived without them.
 *
 * An answer normally carries its verified citations — `metadata.citations` on
 * reload, the `citations` stream frame live (stores/chat.ts). Some do not: an
 * answer saved before the feature, one whose finalisation failed, and the
 * earlier bubbles of a mid-turn split (only the last segment keeps the row's
 * metadata). For those the bubble asks this store, which resolves the tokens
 * with POST /api/userdocs/citations/resolve and caches the answers.
 *
 * Requests made in the same tick are batched per conversation — opening a long
 * history mounts every cited bubble at once, and that should be one request,
 * not one per bubble. Each token is asked about once per conversation: a token
 * the server does not answer for, or a failed request, is not retried until
 * the page reloads, so a broken engine cannot turn scrolling into a request
 * loop.
 *
 * The cache is keyed by profile as well as conversation. The server already
 * refuses another profile's tokens; the key makes sure a profile switch in the
 * same tab can never show the previous profile's answers either.
 */

import { defineStore } from 'pinia';
import { resolveCitations } from '../services/userdocsCitationsApi';
import type { CitationItem } from '../utils/citations';
import { useSettingsStore } from './settings';

interface Pending {
  profile: string;
  conversationId: string | null;
  tokens: Set<string>;
}

// Non-reactive bookkeeping, outside the store (mirrors chat.ts's streamScratch).
const attempted = new Set<string>();
let queue = new Map<string, Pending>();
// The flush the current queue will go out with. flush() takes the queue
// synchronously, so anything queued after that joins the next one.
let nextFlush: Promise<void> | null = null;

function scopeKey(profile: string, conversationId: string | null | undefined): string {
  return `${profile}\u0000${conversationId ?? ''}`;
}

export const useCitationsStore = defineStore('citations', {
  state: () => ({
    byScope: {} as Record<string, Record<string, CitationItem>>,
  }),

  getters: {
    /** The resolved item for a token cited in a conversation, if known yet. */
    itemFor(state) {
      return (conversationId: string | null | undefined, token: string): CitationItem | undefined => {
        const profile = useSettingsStore().profileId;
        return state.byScope[scopeKey(profile, conversationId)]?.[token];
      };
    },
  },

  actions: {
    /**
     * Make sure these tokens get resolved for this conversation. Resolves once
     * the batch they joined has been answered (or failed — failure is logged,
     * not thrown: a citation that cannot be resolved just stays a plain chip).
     */
    ensure(conversationId: string | null | undefined, tokens: string[]): Promise<void> {
      const settings = useSettingsStore();
      const profile = settings.profileId;
      if (!settings.authToken) return Promise.resolve();
      const key = scopeKey(profile, conversationId);
      let entry = queue.get(key);
      for (const token of tokens) {
        const id = `${key}\u0000${token}`;
        if (attempted.has(id)) continue;
        attempted.add(id);
        if (!entry) {
          entry = { profile, conversationId: conversationId ?? null, tokens: new Set() };
          queue.set(key, entry);
        }
        entry.tokens.add(token);
      }
      if (!queue.size) return nextFlush ?? Promise.resolve();
      if (!nextFlush) {
        nextFlush = Promise.resolve().then(() => {
          nextFlush = null;
          return this.flush();
        });
      }
      return nextFlush;
    },

    async flush() {
      const batch = queue;
      queue = new Map();
      const settings = useSettingsStore();
      await Promise.all(Array.from(batch, async ([key, pending]) => {
        // A profile switch between ensure() and now: the token in hand is the
        // new profile's, which must not be used to ask about the old one's.
        if (pending.profile !== settings.profileId || !pending.tokens.size) return;
        try {
          const items = await resolveCitations(
            settings.agentUrl, settings.authToken, Array.from(pending.tokens), pending.conversationId,
          );
          this.byScope[key] = { ...(this.byScope[key] ?? {}), ...items };
        } catch (e) {
          console.warn('[citations] resolve failed:', e);
        }
      }));
    },
  },
});
