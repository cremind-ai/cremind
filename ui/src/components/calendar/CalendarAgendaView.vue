<script setup lang="ts">
import { computed } from 'vue';
import { ElEmpty, ElTag } from 'element-plus';
import { Icon } from '@iconify/vue';
import { type CalEvent, parseLocal, isoDate, timeLabel, isMultiDayOrAllDay } from './calendarUtils';

const props = defineProps<{ events: CalEvent[] }>();
const emit = defineEmits<{ (e: 'select', ev: CalEvent): void }>();

const groups = computed(() => {
  const byDay = new Map<string, CalEvent[]>();
  for (const ev of [...props.events].sort((a, b) => a.start.localeCompare(b.start))) {
    const key = isoDate(parseLocal(ev.start));
    (byDay.get(key) ?? byDay.set(key, []).get(key)!).push(ev);
  }
  return [...byDay.entries()].map(([key, items]) => ({
    key,
    label: parseLocal(`${key}T00:00:00`).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' }),
    items,
  }));
});
</script>

<template>
  <div class="agenda">
    <ElEmpty v-if="groups.length === 0" description="Nothing scheduled in this window." />
    <div v-for="g in groups" :key="g.key" class="agenda-day">
      <div class="agenda-date">{{ g.label }}</div>
      <button
        v-for="ev in g.items"
        :key="ev.subscription_id + ev.start"
        class="agenda-row"
        @click="emit('select', ev)"
      >
        <Icon
          :icon="ev.status === 'completed' ? 'mdi:check-circle'
            : ev.is_reminder_only ? 'mdi:bell-outline' : 'mdi:lightning-bolt-outline'"
          class="agenda-icon"
          :class="{ done: ev.status === 'completed', reminder: ev.is_reminder_only }"
        />
        <span class="agenda-time">{{ isMultiDayOrAllDay(ev) ? 'All day' : timeLabel(ev.start) }}</span>
        <span class="agenda-title">{{ ev.title }}</span>
        <ElTag v-if="ev.is_recurring" size="small" type="success" effect="plain">recurring</ElTag>
        <ElTag v-if="ev.status !== 'active'" size="small" type="info" effect="plain">{{ ev.status }}</ElTag>
        <span v-if="ev.source === 'google'" class="agenda-src"><Icon icon="mdi:google" /></span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.agenda { display: flex; flex-direction: column; gap: 18px; }
.agenda-day { display: flex; flex-direction: column; gap: 4px; }
.agenda-date { font-size: .8rem; font-weight: 600; color: var(--text-secondary); padding: 0 2px 4px; border-bottom: 1px solid var(--border-color); }
.agenda-row {
  display: flex; align-items: center; gap: 12px; width: 100%; text-align: left;
  border: 1px solid var(--border-color); border-radius: 10px; background: var(--surface-color);
  padding: 10px 14px; cursor: pointer; transition: background .12s, border-color .12s;
}
.agenda-row:hover { background: var(--surface-hover); border-color: var(--border-hover); }
.agenda-icon { font-size: 1.15rem; color: var(--primary-color); flex: none; }
.agenda-icon.reminder { color: var(--warning-color); }
.agenda-icon.done { color: var(--success-color); }
.agenda-time { font-variant-numeric: tabular-nums; color: var(--text-secondary); font-size: .82rem; min-width: 58px; }
.agenda-title { flex: 1; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agenda-src { color: var(--text-tertiary); display: inline-flex; }
</style>
