<script setup lang="ts">
import { computed } from 'vue';
import {
  DOW_LABELS, type CalEvent, monthGrid, weekSegments, isMultiDayOrAllDay,
  parseLocal, sameDay, isToday, timeLabel, isoDate, type DaySegment,
} from './calendarUtils';

const props = defineProps<{ anchor: Date; events: CalEvent[] }>();
const emit = defineEmits<{
  (e: 'select', ev: CalEvent): void;
  (e: 'create', dateIso: string): void;
  (e: 'open-day', day: Date): void;
}>();

const LANE_H = 20;       // px per multi-day bar lane
const DAYNUM_H = 24;     // px reserved for the day-number row
const MAX_CHIPS = 3;

const weeks = computed(() => {
  const grid = monthGrid(props.anchor);
  const out: { days: Date[]; segments: DaySegment[]; lanes: number }[] = [];
  for (let i = 0; i < 42; i += 7) {
    const days = grid.slice(i, i + 7);
    const segments = weekSegments(days, props.events);
    const lanes = segments.reduce((m, s) => Math.max(m, s.lane + 1), 0);
    out.push({ days, segments, lanes });
  }
  return out;
});

const curMonth = computed(() => props.anchor.getMonth());

function timedFor(day: Date): CalEvent[] {
  return props.events
    .filter((ev) => !isMultiDayOrAllDay(ev) && sameDay(parseLocal(ev.start), day))
    .sort((a, b) => a.start.localeCompare(b.start));
}

function barStyle(seg: DaySegment) {
  return {
    left: `calc(${(seg.startCol / 7) * 100}% + 2px)`,
    width: `calc(${(seg.span / 7) * 100}% - 4px)`,
    top: `${DAYNUM_H + seg.lane * LANE_H}px`,
  };
}
function barClass(seg: DaySegment) {
  return {
    reminder: seg.ev.is_reminder_only,
    'continues-left': seg.continuesLeft,
    'continues-right': seg.continuesRight,
    done: seg.ev.status === 'completed' || seg.ev.status === 'cancelled',
  };
}
</script>

<template>
  <div class="cal-month">
    <div class="cal-dow-row">
      <div v-for="d in DOW_LABELS" :key="d" class="cal-dow">{{ d }}</div>
    </div>
    <div class="cal-grid">
      <div
        v-for="(week, wi) in weeks"
        :key="wi"
        class="cal-week"
        :style="{ '--bars-space': `${week.lanes * LANE_H}px` }"
      >
        <div
          v-for="day in week.days"
          :key="day.toISOString()"
          class="cal-day"
          :class="{ outside: day.getMonth() !== curMonth, today: isToday(day) }"
          @click="emit('create', isoDate(day))"
        >
          <div class="cal-daynum"><span :class="{ 'is-today': isToday(day) }">{{ day.getDate() }}</span></div>
          <div class="cal-bars-space" />
          <div class="cal-chips">
            <button
              v-for="ev in timedFor(day).slice(0, MAX_CHIPS)"
              :key="ev.subscription_id + ev.start"
              class="cal-chip"
              :class="{ reminder: ev.is_reminder_only, done: ev.status !== 'active' }"
              @click.stop="emit('select', ev)"
            >
              <span class="dot" />
              <span class="t">{{ timeLabel(ev.start) }}</span>
              <span class="title">{{ ev.title }}</span>
            </button>
            <button
              v-if="timedFor(day).length > MAX_CHIPS"
              class="cal-more"
              @click.stop="emit('open-day', day)"
            >+{{ timedFor(day).length - MAX_CHIPS }} more</button>
          </div>
        </div>

        <!-- multi-day / all-day spanning bars overlaid on the week -->
        <div class="cal-bars">
          <button
            v-for="seg in week.segments"
            :key="seg.ev.subscription_id + seg.ev.start + seg.lane"
            class="cal-mbar"
            :class="barClass(seg)"
            :style="barStyle(seg)"
            @click.stop="emit('select', seg.ev)"
          >
            <span class="mbar-title">{{ seg.ev.title }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cal-month { display: flex; flex-direction: column; border: 1px solid var(--border-color); border-radius: 12px; overflow: hidden; background: var(--surface-color); }
.cal-dow-row { display: grid; grid-template-columns: repeat(7, 1fr); background: var(--hover-bg); }
.cal-dow { padding: 8px; text-align: center; font-size: 0.72rem; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--text-tertiary); }
.cal-grid { display: flex; flex-direction: column; }

.cal-week { position: relative; display: grid; grid-template-columns: repeat(7, 1fr); }
.cal-week:not(:last-child) { border-bottom: 1px solid var(--border-color); }

.cal-day {
  position: relative;
  min-height: 116px;
  padding: 4px 4px 6px;
  border-right: 1px solid var(--border-color);
  display: flex; flex-direction: column;
  cursor: pointer;
  transition: background .12s ease;
}
.cal-day:last-child { border-right: none; }
.cal-day:hover { background: var(--surface-hover); }
.cal-day.outside { background: color-mix(in srgb, var(--bg-color) 60%, transparent); }
.cal-day.outside .cal-daynum { opacity: .45; }
.cal-day.today { background: color-mix(in srgb, var(--primary-color) 7%, transparent); }

.cal-daynum { height: 24px; display: flex; justify-content: flex-end; padding: 0 2px; }
.cal-daynum span { font-size: .78rem; color: var(--text-secondary); width: 22px; height: 22px; display: grid; place-items: center; border-radius: 50%; }
.cal-daynum span.is-today { background: var(--primary-color); color: #fff; font-weight: 600; }

.cal-bars-space { height: var(--bars-space, 0); flex: none; }

.cal-chips { display: flex; flex-direction: column; gap: 2px; overflow: hidden; }
.cal-chip {
  display: flex; align-items: center; gap: 5px;
  border: none; background: transparent; cursor: pointer;
  padding: 1px 4px; border-radius: 4px; width: 100%; text-align: left;
  font-size: .72rem; color: var(--text-primary); overflow: hidden;
}
.cal-chip:hover { background: var(--hover-bg); }
.cal-chip .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--primary-color); flex: none; }
.cal-chip.reminder .dot { background: var(--warning-color); }
.cal-chip.done { opacity: .5; }
.cal-chip .t { color: var(--text-tertiary); font-variant-numeric: tabular-nums; flex: none; }
.cal-chip .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cal-more { border: none; background: transparent; color: var(--text-tertiary); font-size: .7rem; cursor: pointer; text-align: left; padding: 1px 4px; }
.cal-more:hover { color: var(--primary-color); }

.cal-bars { position: absolute; inset: 0; pointer-events: none; }
.cal-mbar {
  position: absolute; height: 18px; pointer-events: auto;
  border: none; cursor: pointer;
  background: var(--primary-color); color: #fff;
  border-radius: 4px; padding: 0 8px;
  font-size: .72rem; font-weight: 500; line-height: 18px;
  display: flex; align-items: center; overflow: hidden;
}
.cal-mbar.reminder { background: var(--warning-color); }
.cal-mbar.done { opacity: .55; }
.cal-mbar.continues-left { border-top-left-radius: 0; border-bottom-left-radius: 0; }
.cal-mbar.continues-right { border-top-right-radius: 0; border-bottom-right-radius: 0; }
.mbar-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
