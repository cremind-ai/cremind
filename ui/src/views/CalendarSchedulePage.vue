<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { goBackToChat } from '../utils/backToChat';
import {
  ElButton, ElDatePicker, ElDialog, ElInput, ElInputNumber, ElMessage,
  ElMessageBox, ElOption, ElRadioButton, ElRadioGroup, ElSelect, ElSwitch,
  ElTimePicker, ElTooltip,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  connectGoogleCalendar, createCalendarEvent, deleteCalendarEvent,
  disconnectGoogleCalendar, getCalendarSettings, listCalendarEvents,
  setCalendarEnabled, updateCalendarEvent, type CalendarOccurrence,
} from '../services/calendarApi';
import CalendarMonthView from '../components/calendar/CalendarMonthView.vue';
import CalendarTimeGridView from '../components/calendar/CalendarTimeGridView.vue';
import CalendarAgendaView from '../components/calendar/CalendarAgendaView.vue';
import {
  type CalView, viewRange, titleFor, navigate, weekDays, startOfDay, addDays,
  isoDate, parseLocal,
} from '../components/calendar/calendarUtils';

const props = defineProps<{ profile: string }>();
const router = useRouter();
const settings = useSettingsStore();

// ── feature + provider status ──────────────────────────────────────────────
const enabled = ref(false);
const googleConnected = ref(false);
const googleEmail = ref<string | null>(null);
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

watch([view, anchor, enabled], () => { loadEvents(); });

onMounted(async () => {
  if (!settings.authToken && props.profile) settings.activateProfile(props.profile);
  await loadSettings();
  await loadEvents();
});
watch(() => settings.authToken, async (t, p) => { if (t && !p) { await loadSettings(); await loadEvents(); } });

function goBack() { goBackToChat(router, props.profile); }

// ── Google connect ───────────────────────────────────────────────────────
async function onConnectGoogle() {
  connecting.value = true;
  try {
    const res = await connectGoogleCalendar(settings.agentUrl, settings.authToken);
    if (res.error || !res.authorize_url) {
      connecting.value = false;
      ElMessage.warning(res.message || 'Google Calendar connect is unavailable.');
      return;
    }
    window.open(res.authorize_url, 'cremind-google-oauth', 'width=520,height=640');
    const started = Date.now();
    const poll = window.setInterval(async () => {
      await loadSettings();
      if (googleConnected.value) {
        window.clearInterval(poll); connecting.value = false;
        ElMessage.success('Connected Google Calendar');
        await loadEvents();
      } else if (Date.now() - started > 150000) {
        window.clearInterval(poll); connecting.value = false;
      }
    }, 2500);
  } catch (err) {
    connecting.value = false;
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
    googleConnected.value = false; googleEmail.value = null;
    ElMessage.success('Disconnected Google Calendar');
    await loadEvents();
  } catch (err) { ElMessage.error(err instanceof Error ? err.message : String(err)); }
}

// ── create / edit dialog ───────────────────────────────────────────────────
const dialogOpen = ref(false);
const editingId = ref<string | null>(null);
const readOnly = ref(false);  // a pure Google event (not Cremind-managed)
type Repeat = 'none' | 'daily' | 'weekdays' | 'weekly' | 'monthly';
const form = ref({
  title: '', all_day: false, date: isoDate(new Date()), time: '09:00',
  end_date: isoDate(new Date()), duration_minutes: 30,
  repeat: 'none' as Repeat, end_type: 'never' as 'never' | 'count' | 'until',
  end_count: 5, end_until: '', action: '',
});

function resetForm(date?: string, time?: string) {
  const d = date || isoDate(new Date());
  form.value = {
    title: '', all_day: false, date: d, time: time || '09:00',
    end_date: d, duration_minutes: 30, repeat: 'none', end_type: 'never',
    end_count: 5, end_until: '', action: '',
  };
}

function openCreate(dtstart?: string) {
  editingId.value = null; readOnly.value = false;
  if (dtstart && dtstart.includes('T')) resetForm(dtstart.slice(0, 10), dtstart.slice(11, 16));
  else resetForm(dtstart);
  dialogOpen.value = true;
}

function openEdit(ev: CalendarOccurrence) {
  if (!ev.subscription_id) {
    // Pure Google event — read-only detail.
    editingId.value = null; readOnly.value = true;
    resetForm(ev.start.slice(0, 10), ev.start.slice(11, 16));
    form.value.title = ev.title;
    form.value.all_day = ev.all_day;
    dialogOpen.value = true;
    return;
  }
  editingId.value = ev.subscription_id; readOnly.value = false;
  resetForm(ev.start.slice(0, 10), ev.start.slice(11, 16));
  form.value.title = ev.title;
  form.value.all_day = ev.all_day;
  form.value.action = ev.action || '';
  // inclusive end date for an all-day event (end is stored exclusive-ish)
  const endD = parseLocal(ev.end);
  form.value.end_date = isoDate(ev.all_day ? addDays(endD, endD.getHours() === 0 && endD.getMinutes() === 0 ? -1 : 0) : endD);
  if (!ev.all_day) {
    const mins = Math.max(15, Math.round((parseLocal(ev.end).getTime() - parseLocal(ev.start).getTime()) / 60000));
    form.value.duration_minutes = mins;
  }
  dialogOpen.value = true;
}

const WEEKDAY_CODES = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];
function buildRrule(): string | null {
  const d = parseLocal(`${form.value.date}T00:00:00`);
  switch (form.value.repeat) {
    case 'daily': return 'FREQ=DAILY';
    case 'weekdays': return 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR';
    case 'weekly': return `FREQ=WEEKLY;BYDAY=${WEEKDAY_CODES[(d.getDay() + 6) % 7]}`;
    case 'monthly': return `FREQ=MONTHLY;BYMONTHDAY=${d.getDate()}`;
    default: return null;
  }
}

async function submitForm() {
  if (!form.value.title.trim()) { ElMessage.warning('Title is required'); return; }
  const allDay = form.value.all_day;
  const dtstart = allDay ? `${form.value.date}T00:00:00` : `${form.value.date}T${form.value.time}:00`;
  let duration = form.value.duration_minutes;
  if (allDay) {
    const days = Math.max(1, Math.round((parseLocal(`${form.value.end_date}T00:00:00`).getTime()
      - parseLocal(`${form.value.date}T00:00:00`).getTime()) / 86400000) + 1);
    duration = days * 1440;
  }
  const rrule = buildRrule();
  let endType: string | null = null; let endValue: string | null = null;
  if (rrule) {
    endType = form.value.end_type;
    if (endType === 'count') endValue = String(form.value.end_count);
    else if (endType === 'until') endValue = form.value.end_until ? `${form.value.end_until}T23:59:59` : null;
  }
  const payload = {
    title: form.value.title.trim(),
    dtstart,
    action: form.value.action.trim(),
    all_day: allDay,
    duration_minutes: duration,
    rrule,
    recurrence_end_type: endType,
    recurrence_end_value: endValue,
  };
  busy.value = true;
  try {
    if (editingId.value) {
      await updateCalendarEvent(settings.agentUrl, settings.authToken, editingId.value, payload);
      ElMessage.success('Event updated');
    } else {
      await createCalendarEvent(settings.agentUrl, settings.authToken, payload);
      ElMessage.success('Event created');
    }
    dialogOpen.value = false;
    await loadEvents();
  } catch (err) { ElMessage.error(err instanceof Error ? err.message : String(err)); }
  finally { busy.value = false; }
}

async function deleteCurrent() {
  if (!editingId.value) return;
  try {
    await ElMessageBox.confirm(`Delete '${form.value.title}'?`, 'Confirm delete',
      { confirmButtonText: 'Delete', cancelButtonText: 'Cancel', type: 'warning' });
  } catch { return; }
  busy.value = true;
  try {
    await deleteCalendarEvent(settings.agentUrl, settings.authToken, editingId.value);
    ElMessage.success('Deleted');
    dialogOpen.value = false;
    await loadEvents();
  } catch (err) { ElMessage.error(err instanceof Error ? err.message : String(err)); }
  finally { busy.value = false; }
}
</script>

<template>
  <div class="page">
    <header class="page-header">
      <button class="icon-button" @click="goBack" title="Back"><Icon icon="mdi:arrow-left" /></button>
      <h2>Calendar &amp; Schedule</h2>
      <div class="spacer" />
      <div class="header-actions">
        <ElTooltip
          v-if="!googleConnected"
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
      <CalendarAgendaView v-else :events="events" @select="openEdit" />
    </template>

    <!-- create / edit dialog -->
    <ElDialog
      v-model="dialogOpen"
      :title="readOnly ? 'Event' : (editingId ? 'Edit event' : 'New event')"
      width="520px"
    >
      <div class="form">
        <p v-if="readOnly" class="ro-note">
          <Icon icon="mdi:google" /> A Google Calendar event — manage it in Google Calendar.
        </p>
        <label class="f-label">Title</label>
        <ElInput v-model="form.title" :disabled="readOnly" placeholder="e.g. Daily standup" />

        <div class="f-toggle">
          <span class="f-label">All day</span>
          <ElSwitch v-model="form.all_day" :disabled="readOnly" />
        </div>

        <div class="f-row" v-if="!form.all_day">
          <div class="f-col">
            <label class="f-label">Date</label>
            <ElDatePicker v-model="form.date" type="date" value-format="YYYY-MM-DD" :disabled="readOnly" :clearable="false" style="width:100%" />
          </div>
          <div class="f-col">
            <label class="f-label">Time</label>
            <ElTimePicker v-model="form.time" format="HH:mm" value-format="HH:mm" :disabled="readOnly" :clearable="false" style="width:100%" />
          </div>
          <div class="f-col">
            <label class="f-label">Duration (min)</label>
            <ElInputNumber v-model="form.duration_minutes" :min="5" :step="15" :disabled="readOnly" controls-position="right" style="width:100%" />
          </div>
        </div>
        <div class="f-row" v-else>
          <div class="f-col">
            <label class="f-label">Start date</label>
            <ElDatePicker v-model="form.date" type="date" value-format="YYYY-MM-DD" :disabled="readOnly" :clearable="false" style="width:100%" />
          </div>
          <div class="f-col">
            <label class="f-label">End date</label>
            <ElDatePicker v-model="form.end_date" type="date" value-format="YYYY-MM-DD" :disabled="readOnly" :clearable="false" style="width:100%" />
          </div>
        </div>

        <template v-if="!readOnly">
          <label class="f-label">Repeat</label>
          <ElSelect v-model="form.repeat" style="width:100%">
            <ElOption label="Does not repeat" value="none" />
            <ElOption label="Every day" value="daily" />
            <ElOption label="Every weekday (Mon–Fri)" value="weekdays" />
            <ElOption label="Weekly (on this weekday)" value="weekly" />
            <ElOption label="Monthly (on this date)" value="monthly" />
          </ElSelect>

          <template v-if="form.repeat !== 'none'">
            <label class="f-label">Ends</label>
            <ElSelect v-model="form.end_type" style="width:100%">
              <ElOption label="Never" value="never" />
              <ElOption label="After N occurrences" value="count" />
              <ElOption label="On a date" value="until" />
            </ElSelect>
            <ElInputNumber v-if="form.end_type === 'count'" v-model="form.end_count" :min="1" style="margin-top:8px" />
            <ElDatePicker v-if="form.end_type === 'until'" v-model="form.end_until" type="date" value-format="YYYY-MM-DD" style="margin-top:8px;width:100%" />
          </template>

          <label class="f-label">Command to run when it fires</label>
          <ElInput
            v-model="form.action"
            type="textarea"
            :rows="3"
            placeholder="Runs in the Schedule conversation. Leave empty to run the title as the command (e.g. 'tắt đèn hiên')."
          />
        </template>
      </div>
      <template #footer>
        <div class="dlg-footer">
          <ElButton v-if="editingId && !readOnly" type="danger" plain @click="deleteCurrent">
            <Icon icon="mdi:delete-outline" /> Delete
          </ElButton>
          <div class="spacer" />
          <ElButton @click="dialogOpen = false">{{ readOnly ? 'Close' : 'Cancel' }}</ElButton>
          <ElButton v-if="!readOnly" type="primary" :loading="busy" @click="submitForm">
            {{ editingId ? 'Save' : 'Create' }}
          </ElButton>
        </div>
      </template>
    </ElDialog>
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

.form { display: flex; flex-direction: column; gap: 6px; }
.f-label { font-size: .8125rem; color: var(--text-secondary); margin-top: 6px; }
.f-row { display: flex; gap: 12px; }
.f-col { display: flex; flex-direction: column; gap: 4px; flex: 1; }
.f-toggle { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; padding: 6px 0; }
.ro-note { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: .85rem; margin: 0 0 4px; }
.dlg-footer { display: flex; align-items: center; gap: 8px; width: 100%; }
</style>
