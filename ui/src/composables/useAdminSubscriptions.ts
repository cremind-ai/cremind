import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useSettingsStore } from '../stores/settings';
import {
  subscribeSkillEventsAdmin,
  subscribeFileWatchersAdmin,
  subscribeScheduleEventsAdmin,
  type AdminEventsSubHandle,
} from '../services/adminEventsStream';
import type { ListenerStatus, SkillEventSubscription } from '../services/skillEventsApi';
import type { FileWatcherSubscription } from '../services/fileWatchersApi';
import type { ScheduleEventSubscription } from '../services/calendarApi';

/**
 * Subscribes to all three admin subscription frames (skill events + listeners,
 * file watchers, schedules) so a view can render the full set of event rules —
 * e.g. the Tasks board's "Upcoming" column.
 *
 * These ride the SAME multiplexed, ref-counted admin SSE the Events page already
 * holds (adminEventsStream.ts): adding subscribers costs no extra connection and
 * replays the last snapshot immediately. The board relies on the page keeping its
 * own handles open in both view modes so the shared connection is never torn down
 * mid-switch.
 */
export function useAdminSubscriptions() {
  const settings = useSettingsStore();

  const skillSubs = ref<SkillEventSubscription[]>([]);
  const listeners = ref<Record<string, ListenerStatus>>({});
  const fileWatchers = ref<FileWatcherSubscription[]>([]);
  const schedules = ref<ScheduleEventSubscription[]>([]);
  const schedulesEnabled = ref(true);
  const loading = ref(true);

  let skillHandle: AdminEventsSubHandle | null = null;
  let fileHandle: AdminEventsSubHandle | null = null;
  let scheduleHandle: AdminEventsSubHandle | null = null;

  function start() {
    stop();
    if (!settings.agentUrl || !settings.authToken) return;
    loading.value = true;
    skillHandle = subscribeSkillEventsAdmin(settings.agentUrl, settings.authToken, (snap) => {
      skillSubs.value = snap.subscriptions;
      listeners.value = snap.listeners;
      loading.value = false;
    });
    fileHandle = subscribeFileWatchersAdmin(settings.agentUrl, settings.authToken, (snap) => {
      fileWatchers.value = snap.subscriptions;
    });
    scheduleHandle = subscribeScheduleEventsAdmin(settings.agentUrl, settings.authToken, (snap) => {
      schedules.value = snap.subscriptions;
      schedulesEnabled.value = snap.enabled;
    });
  }

  function stop() {
    skillHandle?.close();
    fileHandle?.close();
    scheduleHandle?.close();
    skillHandle = fileHandle = scheduleHandle = null;
  }

  onMounted(start);
  watch(
    () => settings.authToken,
    (token, prev) => {
      if (token && !prev) start();
    },
  );
  onBeforeUnmount(stop);

  return { skillSubs, listeners, fileWatchers, schedules, schedulesEnabled, loading };
}
