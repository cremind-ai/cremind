/**
 * What counts as an unsaved change on a settings page: a difference from the
 * saved state, compared by value — not a record of which fields were touched.
 * A field typed back to its saved value is no change, and two equal forms whose
 * keys were set in another order are equal.
 *
 * Vue only, no router or Element Plus: the leave guard that uses it lives in
 * useUnsavedChanges, which re-exports these.
 */
import { computed, shallowRef, type ComputedRef } from 'vue';

/** JSON with every object's keys in sorted order, so two equal states read the
 *  same whatever order their keys were set in. */
export function stableStringify(value: unknown): string {
  return JSON.stringify(value, (_key, v) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(Object.keys(v).sort().map(k => [k, (v as Record<string, unknown>)[k]]))
      : v) ?? '';
}

export interface SavedSnapshot<T> {
  /** The form differs from its saved state. */
  dirty: ComputedRef<boolean>;
  /** Take what the form holds now as saved: after it loads, and after a save. */
  commit(): void;
  /** A fresh copy of the saved state, for Discard. */
  saved(): T;
}

/** The saved state of the form `read` returns, to compare the live form with. */
export function useSavedSnapshot<T>(read: () => T): SavedSnapshot<T> {
  const saved = shallowRef(stableStringify(read()));
  return {
    dirty: computed(() => stableStringify(read()) !== saved.value),
    commit: () => { saved.value = stableStringify(read()); },
    saved: () => JSON.parse(saved.value || 'null') as T,
  };
}
