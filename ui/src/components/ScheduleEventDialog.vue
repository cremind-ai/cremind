<script setup lang="ts">
/**
 * Shared create/edit dialog for schedule events. Extracted from
 * CalendarSchedulePage so both the Calendar page (which edits calendar
 * occurrences) and the Events page (which edits raw schedule subscriptions)
 * drive the same form + PATCH/POST /api/calendar/events path.
 *
 * Open it via one of the exposed methods:
 *  - openCreate(dtstart?)               — new event
 *  - openEdit(occurrence)               — from a CalendarOccurrence (calendar page)
 *  - openEditSubscription(subscription) — from a raw schedule row (events page)
 * Emits `saved` / `deleted` so the parent can refresh (the Events page also
 * refreshes live via SSE).
 */
import { ref } from 'vue';
import {
  ElButton, ElDatePicker, ElDialog, ElInput, ElInputNumber, ElMessage,
  ElMessageBox, ElOption, ElSelect, ElSwitch, ElTimePicker,
} from 'element-plus';
import { Icon } from '@iconify/vue';
import { useSettingsStore } from '../stores/settings';
import {
  createCalendarEvent, deleteCalendarEvent, updateCalendarEvent,
  type CalendarOccurrence, type ScheduleEventSubscription,
} from '../services/calendarApi';
import { isoDate, parseLocal, addDays } from './calendar/calendarUtils';

const settings = useSettingsStore();
const emit = defineEmits<{ (e: 'saved'): void; (e: 'deleted'): void }>();

const dialogOpen = ref(false);
const editingId = ref<string | null>(null);
const readOnly = ref(false); // a pure Google event (not Cremind-managed)
const busy = ref(false);

type Repeat = 'none' | 'daily' | 'weekdays' | 'weekly' | 'monthly' | 'custom';
const form = ref({
  title: '', all_day: false, date: isoDate(new Date()), time: '09:00',
  end_date: isoDate(new Date()), duration_minutes: 30,
  repeat: 'none' as Repeat, end_type: 'never' as 'never' | 'count' | 'until',
  end_count: 5, end_until: '', action: '',
  // For a recurrence that matches no preset (e.g. an agent-created FREQ=HOURLY),
  // "Custom (keep as-is)" preserves the original rrule / end untouched on save.
  customRrule: null as string | null,
  customEndType: null as string | null,
  customEndValue: null as string | null,
});

function resetForm(date?: string, time?: string) {
  const d = date || isoDate(new Date());
  form.value = {
    title: '', all_day: false, date: d, time: time || '09:00',
    end_date: d, duration_minutes: 30, repeat: 'none', end_type: 'never',
    end_count: 5, end_until: '', action: '',
    customRrule: null, customEndType: null, customEndValue: null,
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

function openEditSubscription(sub: ScheduleEventSubscription) {
  editingId.value = sub.id; readOnly.value = false;
  const dt = sub.dtstart || '';
  const date = dt.slice(0, 10) || isoDate(new Date());
  const time = dt.length >= 16 ? dt.slice(11, 16) : '09:00';
  resetForm(date, time);
  form.value.title = sub.title || '';
  form.value.all_day = !!sub.all_day;
  form.value.action = sub.action || '';
  form.value.duration_minutes = sub.duration_minutes || 30;
  if (sub.all_day) {
    const days = Math.max(1, Math.round((sub.duration_minutes || 1440) / 1440));
    form.value.end_date = isoDate(addDays(parseLocal(`${date}T00:00:00`), days - 1));
  }
  const rep = repeatFromRrule(sub.rrule);
  form.value.repeat = rep;
  if (rep === 'custom') {
    form.value.customRrule = sub.rrule;
    form.value.customEndType = sub.recurrence_end_type;
    form.value.customEndValue = sub.recurrence_end_value;
  } else if (rep !== 'none' && sub.recurrence_end_type) {
    form.value.end_type = sub.recurrence_end_type as 'never' | 'count' | 'until';
    if (sub.recurrence_end_type === 'count') {
      form.value.end_count = Number(sub.recurrence_end_value) || 5;
    } else if (sub.recurrence_end_type === 'until') {
      form.value.end_until = (sub.recurrence_end_value || '').slice(0, 10);
    }
  }
  dialogOpen.value = true;
}

const WEEKDAY_CODES = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];
function buildRrule(): string | null {
  if (form.value.repeat === 'custom') return form.value.customRrule;
  const d = parseLocal(`${form.value.date}T00:00:00`);
  switch (form.value.repeat) {
    case 'daily': return 'FREQ=DAILY';
    case 'weekdays': return 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR';
    case 'weekly': return `FREQ=WEEKLY;BYDAY=${WEEKDAY_CODES[(d.getDay() + 6) % 7]}`;
    case 'monthly': return `FREQ=MONTHLY;BYMONTHDAY=${d.getDate()}`;
    default: return null;
  }
}

// Inverse of buildRrule: recognise the presets, else 'custom' (kept as-is).
function repeatFromRrule(rrule: string | null): Repeat {
  if (!rrule) return 'none';
  const up = rrule.toUpperCase().trim();
  if (up === 'FREQ=DAILY') return 'daily';
  if (up === 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR') return 'weekdays';
  if (/^FREQ=WEEKLY;BYDAY=(MO|TU|WE|TH|FR|SA|SU)$/.test(up)) return 'weekly';
  if (/^FREQ=MONTHLY;BYMONTHDAY=\d+$/.test(up)) return 'monthly';
  return 'custom';
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
  if (form.value.repeat === 'custom') {
    endType = form.value.customEndType;
    endValue = form.value.customEndValue;
  } else if (rrule) {
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
    emit('saved');
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
    emit('deleted');
  } catch (err) { ElMessage.error(err instanceof Error ? err.message : String(err)); }
  finally { busy.value = false; }
}

defineExpose({ openCreate, openEdit, openEditSubscription });
</script>

<template>
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
          <ElOption v-if="form.repeat === 'custom'" label="Custom recurrence (keep as-is)" value="custom" />
        </ElSelect>

        <p v-if="form.repeat === 'custom'" class="custom-note">
          <Icon icon="mdi:information-outline" /> This event uses a custom recurrence
          (<code>{{ form.customRrule }}</code>) that has no simple preset. It is kept
          unchanged on save; pick a preset above to replace it.
        </p>

        <template v-else-if="form.repeat !== 'none'">
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
</template>

<style scoped>
.form { display: flex; flex-direction: column; gap: 8px; }
.f-label { font-size: .8125rem; color: var(--text-secondary); }
.f-toggle { display: flex; align-items: center; justify-content: space-between; margin-top: 4px; }
.f-row { display: flex; gap: 12px; }
.f-col { flex: 1; display: flex; flex-direction: column; gap: 4px; }
.ro-note { margin: 0 0 4px; color: var(--text-secondary); font-size: .875rem; display: flex; align-items: center; gap: 6px; }
.custom-note { margin: 4px 0 0; color: var(--text-secondary); font-size: .8125rem; line-height: 1.45; }
.custom-note code { font-family: var(--font-mono, monospace); font-size: .75rem; word-break: break-all; }
.dlg-footer { display: flex; align-items: center; gap: 8px; }
.spacer { flex: 1; }
</style>
