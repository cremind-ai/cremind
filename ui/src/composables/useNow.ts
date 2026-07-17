import { onBeforeUnmount, onMounted, ref } from 'vue';

// A single app-wide "current time" ref driven by one setInterval, ref-counted so
// the timer only runs while at least one component is consuming it. Used by the
// Tasks board so live duration tickers and countdowns all update in lock-step off
// one timer rather than one interval per card.

const now = ref(Date.now());
let consumers = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function start() {
  if (timer) return;
  now.value = Date.now();
  timer = setInterval(() => {
    now.value = Date.now();
  }, 1000);
}

function stop() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

/**
 * Returns a reactive `now` (epoch ms) that ticks every second. Only components
 * that read `now.value` re-render on each tick, so mount it wherever you need a
 * live clock and let Vue's dependency tracking limit the churn.
 */
export function useNow() {
  onMounted(() => {
    consumers += 1;
    start();
  });
  onBeforeUnmount(() => {
    consumers -= 1;
    if (consumers <= 0) {
      consumers = 0;
      stop();
    }
  });
  return { now };
}
