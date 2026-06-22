import { ref } from 'vue';
import { copyTextToClipboard } from '../utils/clipboard';

// Wraps the robust clipboard helper with a transient "copied" indicator so each
// consumer doesn't re-implement the ref + reset-timeout boilerplate. A key lets a
// single component drive several independent copy buttons (e.g. URL + code) without
// their indicators flipping together.
export function useCopyToClipboard(resetMs = 2000) {
  const copiedKey = ref<string | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;

  async function copy(text: string, key = '_default'): Promise<boolean> {
    const ok = await copyTextToClipboard(text);
    if (ok) {
      copiedKey.value = key;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => { copiedKey.value = null; }, resetMs);
    }
    return ok;
  }

  const isCopied = (key = '_default') => copiedKey.value === key;

  return { copy, isCopied };
}
