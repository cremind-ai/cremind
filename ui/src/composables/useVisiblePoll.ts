/**
 * Poll while the page is on screen, and only then.
 *
 * Runs `fn` every `intervalMs` (read before each wait, so it can change —
 * faster while something is in flight) as long as the document is visible; a
 * hidden tab skips its ticks, and becoming visible again runs `fn` at once.
 * Ticks never overlap: the next wait starts after `fn` settles. Stops on
 * unmount. A plain REST poll, deliberately: a new EventSource would spend one
 * of the origin's ~6 HTTP/1.1 connections.
 */
import { onBeforeUnmount, onMounted, toValue, type MaybeRefOrGetter } from 'vue';

export function useVisiblePoll(
  fn: () => unknown | Promise<unknown>,
  intervalMs: MaybeRefOrGetter<number>,
) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let running = false;
  let stopped = false;

  const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden';

  function schedule() {
    if (stopped) return;
    if (timer) clearTimeout(timer);
    timer = setTimeout(tick, Math.max(1000, toValue(intervalMs)));
  }

  async function tick() {
    timer = null;
    if (stopped) return;
    if (!visible() || running) { schedule(); return; }
    running = true;
    try {
      await fn();
    } catch {
      // The caller reports its own errors; a failed tick just waits for the next.
    } finally {
      running = false;
      schedule();
    }
  }

  function onVisibility() {
    if (visible()) void tick();
  }

  onMounted(() => {
    stopped = false;
    schedule();
    document.addEventListener('visibilitychange', onVisibility);
  });

  onBeforeUnmount(() => {
    stopped = true;
    if (timer) clearTimeout(timer);
    timer = null;
    document.removeEventListener('visibilitychange', onVisibility);
  });

  return {
    /** Run now (e.g. after an action) and restart the wait. */
    trigger: () => { if (timer) clearTimeout(timer); void tick(); },
  };
}
