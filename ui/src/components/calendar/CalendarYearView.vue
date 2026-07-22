<script setup lang="ts">
import { computed } from 'vue';
import {
  MONTHS, type CalEvent, monthGrid, countByDay, isToday, isoDate,
} from './calendarUtils';

const props = defineProps<{ anchor: Date; events: CalEvent[] }>();
const emit = defineEmits<{
  (e: 'open-day', day: Date): void;
  (e: 'open-month', month: Date): void;
}>();

// Single-letter Monday-first weekday headers.
const DOW_INITIALS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];

const year = computed(() => props.anchor.getFullYear());
const dayCounts = computed(() => countByDay(props.events));

// The twelve months of the anchored year, each with its 42-cell Monday-first grid.
const months = computed(() =>
  Array.from({ length: 12 }, (_, m) => {
    const first = new Date(year.value, m, 1);
    return { month: m, first, days: monthGrid(first) };
  }),
);

function countFor(day: Date): number {
  return dayCounts.value.get(isoDate(day)) ?? 0;
}
</script>

<template>
  <div class="cal-year">
    <section
      v-for="mo in months"
      :key="mo.month"
      class="mini-month"
    >
      <button class="mini-title" @click="emit('open-month', mo.first)">
        {{ MONTHS[mo.month] }}
      </button>
      <div class="mini-dow">
        <span v-for="(d, i) in DOW_INITIALS" :key="i">{{ d }}</span>
      </div>
      <div class="mini-grid">
        <button
          v-for="day in mo.days"
          :key="day.toISOString()"
          class="mini-day"
          :class="{
            outside: day.getMonth() !== mo.month,
            today: isToday(day),
            'has-events': countFor(day) > 0,
          }"
          :title="countFor(day) > 0
            ? `${countFor(day)} event${countFor(day) === 1 ? '' : 's'}`
            : undefined"
          @click="emit('open-day', day)"
        >
          <span class="num">{{ day.getDate() }}</span>
          <span v-if="countFor(day) > 0" class="dot" />
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.cal-year {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 16px;
}

.mini-month {
  border: 1px solid var(--border-color);
  border-radius: 12px;
  background: var(--surface-color);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mini-title {
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
  padding: 2px 4px;
  border-radius: 6px;
  font-size: .95rem;
  font-weight: 600;
  color: var(--text-primary);
}
.mini-title:hover { color: var(--primary-color); background: var(--hover-bg); }

.mini-dow {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
}
.mini-dow span {
  text-align: center;
  font-size: .64rem;
  font-weight: 600;
  letter-spacing: .02em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.mini-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 1px;
}

.mini-day {
  position: relative;
  border: none;
  background: transparent;
  cursor: pointer;
  aspect-ratio: 1 / 1;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  padding: 0;
  color: var(--text-secondary);
  transition: background .12s ease;
}
.mini-day:hover { background: var(--hover-bg); }
.mini-day .num { font-size: .72rem; line-height: 1; }
.mini-day.outside .num { opacity: .35; }
.mini-day.has-events { font-weight: 600; color: var(--text-primary); }

.mini-day.today {
  background: var(--primary-color);
}
.mini-day.today .num { color: #fff; font-weight: 600; }

.mini-day .dot {
  position: absolute;
  bottom: 2px;
  left: 50%;
  transform: translateX(-50%);
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--primary-color);
}
.mini-day.today .dot { background: #fff; }
.mini-day.outside .dot { opacity: .4; }
</style>
