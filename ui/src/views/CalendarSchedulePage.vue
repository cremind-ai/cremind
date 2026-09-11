<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import {
  ElButton, ElMessage, ElMessageBox, ElRadioButton, ElRadioGroup, ElSwitch,
  ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  connectGoogleCalendar, disconnectGoogleCalendar, getCalendarSettings,
  listCalendarEvents, setCalendarEnabled, type CalendarOccurrence,
} from '../services/calendarApi';
import CalendarMonthView from '../components/calendar/CalendarMonthView.vue';
import CalendarTimeGridView from '../components/calendar/CalendarTimeGridView.vue';
import CalendarAgendaView from '../components/calendar/CalendarAgendaView.vue';
import CalendarYearView from '../components/calendar/CalendarYearView.vue';
import ScheduleEventDialog from '../components/ScheduleEventDialog.vue';
import {
  type CalView, viewRange, titleFor, navigate, weekDays, startOfDay,
} from '../components/calendar/calendarUtils';
import { unlinkGoogleSkill } from '../services/googleApi';
import {
  onOAuthReturn, type OAuthReturnDelivery, type OAuthReturnNotice,
} from '../services/oauthReturn';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settings = useSettingsStore();

// ── feature + provider status ──────────────────────────────────────────────
const enabled = ref(false);
const googleConnected = ref(false);
const googleEmail = ref<string | null>(null);
// 'skill' = linked in chat via the gcalendar skill, which owns that credential:
// this page reports it and stays out of the way (no connect, no disconnect).
const googleSource = ref<'skill' | 'app' | null>(null);
const skillManaged = computed(() => googleSource.value === 'skill');
const connecting = ref(false);
const settingsLoaded = ref(false);
const busy = ref(false);
const errorMessage = ref('');

// ── calendar view state ─────────────────────────────────────────────────────
const view = ref<CalView>('month');
const anchor = ref<Date>(startOfDay(new Date()));
const events = ref<CalendarOccurrence[]>([]);

const title = computed(() => titleFor(view.value, anchor.value));
const weekCols = computed(() => weekDays(anchor.value));
const dayCols = computed(() => [startOfDay(anchor.value)]);

// ── data loading ─────────────────────────────────────────────────────────
async function loadSettings() {
  if (!settings.agentUrl || !settings.authToken) return;
  try {
    const s = await getCalendarSettings(settings.agentUrl, settings.authToken);
    enabled.value = s.enabled;
    googleConnected.value = s.google_connected;
    googleEmail.value = s.google_email ?? null;
    googleSource.value = s.google_source ?? null;
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err);
  } finally {
    settingsLoaded.value = true;
  }
}

async function loadEvents() {
  if (!enabled.value || !settings.agentUrl || !settings.authToken) { events.value = []; return; }
  const { from, to } = viewRange(view.value, anchor.value);
  try {
    const res = await listCalendarEvents(settings.agentUrl, settings.authToken, from, to);
    events.value = res.events;
  } catch (err) {
    errorMessage.value = err instanceof Error ? err.message : String(err);
  }
}

async function onToggleEnabled(val: boolean) {
  busy.value = true; errorMessage.value = '';
  try {
    const s = await setCalendarEnabled(settings.agentUrl, settings.authToken, val);
    enabled.value = s.enabled;
    ElMessage.success(val ? 'Calendar & Schedule enabled' : 'Calendar & Schedule disabled');
    await loadEvents();
  } catch (err) {
    enabled.value = !val;
    ElMessage.error(err instanceof Error ? err.message : String(err));
  } finally { busy.value = false; }
}

function setView(v: CalView) { view.value = v; }
function prev() { anchor.value = navigate(view.value, anchor.value, -1); }
function next() { anchor.value = navigate(view.value, anchor.value, 1); }
function goToday() { anchor.value = startOfDay(new Date()); }
function openDay(day: Date) { anchor.value = startOfDay(day); view.value = 'day'; }
function openMonth(month: Date) { anchor.value = startOfDay(month); view.value = 'month'; }

watch([view, anchor, enabled], () => { loadEvents(); });

onMounted(async () => {
  if (!settings.authToken && props.profile) settings.activateProfile(props.profile);
  await loadSettings();
  await loadEvents();
});
watch(() => settings.authToken, async (t, p) => { if (t && !p) { await loadSettings(); await loadEvents(); } });

function goBack() { goBackToChat(router, props.profile); }

// ── Google connect ───────────────────────────────────────────────────────
// The consent popup lands on the OAuth return page (views/OAuthReturn.vue),
// which tells this page the moment Google has answered — by then the server has
// already exchanged the code, so one settings reload shows the result. The poll
// stays as the fallback for a popup that never gets that far (closed early, or
// a consent finished in another browser).
const CONNECT_POLL_MS = 2500;
const CONNECT_TIMEOUT_MS = 150000;
let connectPoll: number | undefined;
let stopReturnListener: (() => void) | null = null;

function stopConnectWait() {
  if (connectPoll !== undefined) {
    window.clearInterval(connectPoll);
    connectPoll = undefined;
  }
  stopReturnListener?.();
  stopReturnListener = null;
  connecting.value = false;
}

/** Reload settings; finish the wait once Google shows as connected. */
async function checkConnected() {
  await loadSettings();
  // ``connecting`` is re-checked AFTER the await: the poll and the return notice
  // can race here, and only the first one to see "connected" may announce it.
  if (!connecting.value || !googleConnected.value) return;
  stopConnectWait();
  ElMessage.success('Connected Google Calendar');
  await loadEvents();
}

/**
 * A denial leaves nothing server-side for this page to check, so a failure
 * notice may end the wait — but only one from this page's OWN consent popup, for
 * this profile. The BroadcastChannel carries every tab's and every profile's
 * calendar returns: another profile cancelling its own consent must not cancel
 * ours. Everything else, ``received`` included, just re-reads this profile's
 * settings, which is harmless whoever the notice was for.
 */
function onGoogleReturn(notice: OAuthReturnNotice, { fromPopup }: OAuthReturnDelivery) {
  if (notice.flow !== 'calendar' || !connecting.value) return;
  const ours = fromPopup && (notice.profile === null || notice.profile === props.profile);
  if (notice.outcome === 'received' || !ours) {
    void checkConnected();
    return;
  }
  stopConnectWait();
  if (notice.outcome === 'denied') {
    ElMessage.warning('Google Calendar was not connected — the Google consent was declined or cancelled.');
  } else if (notice.outcome === 'invalid') {
    ElMessage.warning('That Google sign-in request expired. Click Connect Google to start a new one.');
  } else {
    ElMessage.error('Google answered, but Cremind could not finish connecting Google Calendar. Try again.');
  }
}

async function onConnectGoogle() {
  // Open the window FIRST, synchronously — an await before window.open spends the
  // user-gesture token and the browser blocks the popup. The authorize URL does
  // not exist yet, so navigate the blank window once the server answers.
  const authWindow = window.open('about:blank', 'cremind-google-oauth', 'width=520,height=640');
  stopConnectWait();
  connecting.value = true;
  try {
    const res = await connectGoogleCalendar(settings.agentUrl, settings.authToken, {
      returnRoute: router.currentRoute.value.fullPath,
    });
    if (res.error || !res.authorize_url) {
      if (authWindow && !authWindow.closed) authWindow.close();
      connecting.value = false;
      ElMessage.warning(res.message || 'Google Calendar connect is unavailable.');
      return;
    }
    if (authWindow && !authWindow.closed) {
      stopReturnListener = onOAuthReturn(onGoogleReturn, { popup: authWindow });
      authWindow.location.href = res.authorize_url;
    } else {
      connecting.value = false;
      ElMessage.warning(
        'The browser blocked the Google window — allow pop-ups for this site and try again.',
      );
      return;
    }
    const started = Date.now();
    connectPoll = window.setInterval(() => {
      if (Date.now() - started > CONNECT_TIMEOUT_MS) {
        stopConnectWait();
        return;
      }
      void checkConnected();
    }, CONNECT_POLL_MS);
  } catch (err) {
    stopConnectWait();
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}
// Leaving the page mid-consent must not leave a poll (and a message listener)
// running against a view that no longer exists.
onUnmounted(stopConnectWait);

/**
 * The badge is what a user clicks after noticing the *wrong* Google account, so it
 * has to offer the fix rather than describe it. Re-linking still belongs in chat
 * (the skill owns the consent flow), but unlinking can happen right here.
 */
async function onSkillManagedClick() {
  const who = googleEmail.value ? ` as ${googleEmail.value}` : '';
  try {
    await ElMessageBox.confirm(
      `This calendar is linked through the gcalendar skill${who}.\n\n` +
      'Unlinking revokes Cremind\'s access at Google and deletes the stored ' +
      'credentials. The calendar falls back to the Google account connected on this ' +
      'page, or to the built-in system calendar if there is none — your scheduled ' +
      'events keep firing either way, and events already mirrored into Google stay ' +
      'there. The gcalendar listener stops and is deregistered.\n\n' +
      'To use a different account, unlink here and then ask in chat to link the ' +
      'gcalendar skill again.',
      'Unlink the gcalendar skill?',
      {
        confirmButtonText: 'Unlink the gcalendar skill',
        cancelButtonText: 'Close',
        type: 'warning',
      },
    );
  } catch { return; }
  try {
    const result = await unlinkGoogleSkill(settings.agentUrl, settings.authToken, 'gcalendar');
    // A failed revoke or a surviving credential file needs to outlive a toast.
    if (result.still_linked || (result.revoke_attempted && !result.revoked)) {
      ElMessageBox.alert(result.message, 'Google Calendar: action needed', {
        confirmButtonText: 'OK',
        type: result.still_linked ? 'error' : 'warning',
      });
    } else if (result.unlinked) {
      ElMessage.success('Unlinked the gcalendar skill');
    }
    await loadSettings();
    await loadEvents();
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : String(err));
  }
}

async function onDisconnectGoogle() {
  try {
    await ElMessageBox.confirm(
      'Disconnect Google Calendar? The calendar falls back to the built-in system calendar. Your scheduled events keep firing.',
      'Disconnect Google', { confirmButtonText: 'Disconnect', cancelButtonText: 'Cancel', type: 'warning' },
    );
  } catch { return; }
  try {
    await disconnectGoogleCalendar(settings.agentUrl, settings.authToken);
    googleConnected.value = false; googleEmail.value = null; googleSource.value = null;
    ElMessage.success('Disconnected Google Calendar');
    await loadEvents();
  } catch (err) { ElMessage.error(err instanceof Error ? err.message : String(err)); }
}

// ── create / edit dialog ───────────────────────────────────────────────────
// The dialog + form live in the shared ScheduleEventDialog component (reused by
// the Events page). These thin wrappers keep the existing template bindings.
const scheduleDialog = ref<InstanceType<typeof ScheduleEventDialog> | null>(null);
function openCreate(dtstart?: string) { scheduleDialog.value?.openCreate(dtstart); }
function openEdit(ev: CalendarOccurrence) { scheduleDialog.value?.openEdit(ev); }
</script>

<template>
  <div class="page">
    <header class="page-header">
      <button class="icon-button" @click="goBack" title="Back"><Icon icon="mdi:arrow-left" /></button>
      <h2>Calendar &amp; Schedule</h2>
      <div class="spacer" />
      <div class="header-actions">
        <ElTooltip
          v-if="skillManaged"
          :content="(googleEmail || 'Connected') + ' — linked through the gcalendar skill, which this calendar follows. Re-link or disable that skill to change accounts.'"
          placement="bottom"
        >
          <ElButton type="success" plain @click="onSkillManagedClick">
            <Icon icon="mdi:google" class="btn-ic" />{{ googleEmail || 'Connected' }}
            <span class="via-skill">· via gcalendar skill</span>
          </ElButton>
        </ElTooltip>
        <ElTooltip
          v-else-if="!googleConnected"
          content="Connect Google Calendar (Calendar access only) so the calendar reads your Google events and your Cremind reminders appear there too. Until then, the built-in system calendar is fully functional."
          placement="bottom"
        >
          <ElButton plain :loading="connecting" @click="onConnectGoogle">
            <Icon icon="mdi:google" class="btn-ic" />{{ connecting ? 'Connecting…' : 'Connect Google' }}
          </ElButton>
        </ElTooltip>
        <ElTooltip v-else :content="(googleEmail || 'Connected') + ' — click to disconnect'" placement="bottom">
          <ElButton type="success" plain @click="onDisconnectGoogle">
            <Icon icon="mdi:google" class="btn-ic" />{{ googleEmail || 'Connected' }}<Icon icon="mdi:close" class="btn-ic-r" />
          </ElButton>
        </ElTooltip>
        <div class="switch-wrap">
          <span class="switch-label">Enabled</span>
          <ElSwitch :model-value="enabled" :loading="busy" @change="(v: any) => onToggleEnabled(v === true)" />
        </div>
      </div>
    </header>

    <p v-if="errorMessage" class="error-banner">{{ errorMessage }}</p>

    <div v-if="settingsLoaded && !enabled" class="disabled-card">
      <Icon icon="mdi:calendar-blank-outline" class="disabled-icon" />
      <h3>Calendar &amp; Schedule is off</h3>
      <p>While off, the <code>scheduler</code> tool only normalizes time expressions for other
        tools — no calendar, reminders, or scheduled events fire. Turn it on with the switch above.</p>
    </div>

    <template v-else-if="enabled">
      <div class="toolbar">
        <div class="nav">
          <ElButton circle size="small" @click="prev"><Icon icon="mdi:chevron-left" /></ElButton>
          <ElButton size="small" @click="goToday">Today</ElButton>
          <ElButton circle size="small" @click="next"><Icon icon="mdi:chevron-right" /></ElButton>
          <span class="cal-title">{{ title }}</span>
        </div>
        <div class="toolbar-right">
          <ElRadioGroup :model-value="view" @change="(v: any) => setView(v)" size="small">
            <ElRadioButton value="year">Year</ElRadioButton>
            <ElRadioButton value="month">Month</ElRadioButton>
            <ElRadioButton value="week">Week</ElRadioButton>
            <ElRadioButton value="day">Day</ElRadioButton>
            <ElRadioButton value="agenda">Agenda</ElRadioButton>
          </ElRadioGroup>
          <ElButton type="primary" size="small" @click="openCreate()"><Icon icon="mdi:plus" /> New event</ElButton>
        </div>
      </div>

      <CalendarMonthView
        v-if="view === 'month'" :anchor="anchor" :events="events"
        @select="openEdit" @create="(d) => openCreate(d)" @open-day="openDay"
      />
      <CalendarTimeGridView
        v-else-if="view === 'week'" :days="weekCols" :events="events"
        @select="openEdit" @create="(dt) => openCreate(dt)"
      />
      <CalendarTimeGridView
        v-else-if="view === 'day'" :days="dayCols" :events="events"
        @select="openEdit" @create="(dt) => openCreate(dt)"
      />
      <CalendarYearView
        v-else-if="view === 'year'" :anchor="anchor" :events="events"
        @open-day="openDay" @open-month="openMonth"
      />
      <CalendarAgendaView v-else :events="events" @select="openEdit" />
    </template>

    <!-- create / edit dialog (shared component) -->
    <ScheduleEventDialog ref="scheduleDialog" @saved="loadEvents" @deleted="loadEvents" />
  </div>
</template>

<style scoped>
.page { padding: 24px; display: flex; flex-direction: column; gap: 16px; height: 100%; overflow-y: auto; background: var(--bg-color); }
.page-header { display: flex; align-items: center; gap: 12px; }
.page-header h2 { margin: 0; color: var(--text-primary); }
.spacer { flex: 1; }
.header-actions { display: flex; align-items: center; gap: 16px; }
.btn-ic { margin-right: 6px; }
.btn-ic-r { margin-left: 6px; opacity: .7; }
.via-skill { margin-left: 6px; opacity: .75; font-size: .8125rem; }
.switch-wrap { display: flex; align-items: center; gap: 8px; }
.switch-label { color: var(--text-secondary); font-size: .875rem; }
.icon-button { background: none; border: none; cursor: pointer; font-size: 1.25rem; color: var(--text-primary); display: flex; align-items: center; }

.error-banner { background: color-mix(in srgb, var(--danger-color) 14%, transparent); color: var(--danger-color); padding: 8px 12px; border-radius: 6px; margin: 0; font-size: .875rem; }

.disabled-card { border: 1px dashed var(--border-color); border-radius: 12px; padding: 32px; text-align: center; color: var(--text-secondary); max-width: 620px; margin: 24px auto; background: var(--surface-color); }
.disabled-icon { font-size: 3rem; color: var(--text-tertiary); }
.disabled-card h3 { margin: 12px 0; color: var(--text-primary); }

.toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.nav { display: flex; align-items: center; gap: 8px; }
.cal-title { font-weight: 600; font-size: 1.1rem; color: var(--text-primary); margin-left: 8px; }
.toolbar-right { display: flex; align-items: center; gap: 12px; }
</style>
