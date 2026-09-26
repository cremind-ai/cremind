/**
 * Object URLs for backend resources that need the Bearer token.
 *
 * The server authenticates with a Bearer header only (no cookie), and an
 * `<img src>` cannot send one, so a thumbnail pointed straight at `/api/...`
 * is a 401 — every image file chip in the chat was a broken image until it
 * fell back to an icon. These composables fetch with the header and hand the
 * element an object URL instead, revoking it when the source changes or the
 * component unmounts (an object URL pins its Blob in memory until revoked).
 *
 * The token goes only to the backend's own origin: a chip whose URI is some
 * other site's image gets that URL back untouched, never a request carrying
 * the profile's credentials.
 */

import { onBeforeUnmount, reactive, ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue';
import { useSettingsStore } from '../stores/settings';

function backendOrigin(agentUrl: string): string | null {
  try {
    return new URL(agentUrl || window.location.origin, window.location.href).origin;
  } catch {
    return null;
  }
}

/** Whether `url` points at the backend, i.e. may be sent the Bearer token. */
export function isBackendUrl(url: string, agentUrl: string): boolean {
  try {
    return new URL(url, window.location.href).origin === backendOrigin(agentUrl);
  } catch {
    return false;
  }
}

/** Fetch `url` with the profile's Bearer token; rejects on a non-2xx status. */
export async function fetchAuthedBlob(
  url: string,
  authToken: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const headers: Record<string, string> = authToken ? { Authorization: `Bearer ${authToken}` } : {};
  const res = await fetch(url, { headers, signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`.trim());
  return res.blob();
}

export interface AuthedBlobUrl {
  /** What to put in `src`: an object URL, a foreign URL as-is, or null while
   *  loading / on failure / with no source. */
  url: Ref<string | null>;
  loading: Ref<boolean>;
  failed: Ref<boolean>;
}

/** One source → one object URL. A null/empty source clears it. */
export function useAuthedBlobUrl(source: MaybeRefOrGetter<string | null | undefined>): AuthedBlobUrl {
  const settings = useSettingsStore();
  const url = ref<string | null>(null);
  const loading = ref(false);
  const failed = ref(false);
  let owned: string | null = null;
  let controller: AbortController | null = null;

  const release = () => {
    controller?.abort();
    controller = null;
    if (owned) URL.revokeObjectURL(owned);
    owned = null;
  };

  watch(
    () => toValue(source) || null,
    async (src) => {
      release();
      url.value = null;
      failed.value = false;
      loading.value = false;
      if (!src) return;
      if (!isBackendUrl(src, settings.agentUrl)) {
        url.value = src;
        return;
      }
      const mine = new AbortController();
      controller = mine;
      loading.value = true;
      try {
        const blob = await fetchAuthedBlob(src, settings.authToken, mine.signal);
        if (controller !== mine) return; // superseded while in flight
        owned = URL.createObjectURL(blob);
        url.value = owned;
      } catch {
        if (controller === mine) failed.value = true;
      } finally {
        if (controller === mine) loading.value = false;
      }
    },
    { immediate: true },
  );

  onBeforeUnmount(release);
  return { url, loading, failed };
}

interface Entry {
  url: string | null;
  failed: boolean;
}

/**
 * Many sources (a bubble's image chips) → object URLs, keyed by source.
 * Sources that leave the list are aborted and revoked.
 */
export function useAuthedBlobUrls(sources: MaybeRefOrGetter<string[]>) {
  const settings = useSettingsStore();
  const entries = reactive(new Map<string, Entry>());
  const owned = new Map<string, string>();
  const inflight = new Map<string, AbortController>();

  const drop = (src: string) => {
    inflight.get(src)?.abort();
    inflight.delete(src);
    const objectUrl = owned.get(src);
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    owned.delete(src);
    entries.delete(src);
  };

  const load = async (src: string) => {
    if (!isBackendUrl(src, settings.agentUrl)) {
      entries.set(src, { url: src, failed: false });
      return;
    }
    entries.set(src, { url: null, failed: false });
    const controller = new AbortController();
    inflight.set(src, controller);
    try {
      const blob = await fetchAuthedBlob(src, settings.authToken, controller.signal);
      if (inflight.get(src) !== controller) return;
      const objectUrl = URL.createObjectURL(blob);
      owned.set(src, objectUrl);
      entries.set(src, { url: objectUrl, failed: false });
    } catch {
      if (inflight.get(src) === controller) entries.set(src, { url: null, failed: true });
    } finally {
      if (inflight.get(src) === controller) inflight.delete(src);
    }
  };

  watch(
    () => Array.from(new Set(toValue(sources).filter(Boolean))),
    (wanted) => {
      for (const src of Array.from(entries.keys())) if (!wanted.includes(src)) drop(src);
      for (const src of wanted) if (!entries.has(src)) void load(src);
    },
    { immediate: true },
  );

  onBeforeUnmount(() => {
    for (const src of Array.from(entries.keys())) drop(src);
  });

  return {
    /** The `src` for a source, or null while it loads or after it failed. */
    urlFor: (src: string): string | null => entries.get(src)?.url ?? null,
    failed: (src: string): boolean => entries.get(src)?.failed === true,
  };
}
