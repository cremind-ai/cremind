<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import {
  type CalEvent, packDay, weekSegments, isMultiDayOrAllDay, parseLocal,
  sameDay, isToday, minutesOfDay, timeLabel, pad, isoDate, type DaySegment,
} from './calendarUtils';

const props = defineProps<{ days: Date[]; events: CalEvent[] }>();
const emit = defineEmits<{
  (e: 'select', ev: CalEvent): void;
  (e: 'create', dtstart: string): void;
}>();

const HOUR_H = 48;          // px per hour (24px per 30-min)
const HOURS = Array.from({ length: 24 }, (_, h) => h);
const bodyRef = ref<HTMLElement | null>(null);

const allDaySegments = computed<DaySegment[]>(() => weekSegments(props.days, props.events));
const allDayLanes = computed(() => allDaySegments.value.reduce((m, s) => Math.max(m, s.lane + 1), 0));

function timedFor(day: Date) {
  const evs = props.events.filter((ev) => !isMultiDayOrAllDay(ev) && sameDay(parseLocal(ev.start), day));
  return packDay(evs);
}

function blockStyle(p: ReturnType<typeof packDay>[number]) {
  const top = (p.startMin / 60) * HOUR_H;
  const height = Math.max(18, ((p.endMin - p.startMin) / 60) * HOUR_H - 2);
  const widthPct = 100 / p.cols;
  return {
    top: `${top}px`,
    height: `${height}px`,
    left: `calc(${p.col * widthPct}% + 2px)`,
    width: `calc(${widthPct}% - 4px)`,
  };
}

function nowTop(): number { const n = new Date(); return (minutesOfDay(n) / 60) * HOUR_H; }

function onSlotClick(day: Date, hour: number) {
  emit('create', `${isoDate(day)}T${pad(hour)}:00:00`);
}
function colLabel(day: Date) {
  return { dow: day.toLocaleDateString(undefined, { weekday: 'short' }), num: day.getDate() };
}
function segStyle(seg: DaySegment) {
  return {
    left: `calc(${(seg.startCol / props.days.length) * 100}% + 2px)`,
    width: `calc(${(seg.span / props.days.length) * 100}% - 4px)`,
    top: `${seg.lane * 22}px`,
  };
}

onMounted(() => { if (bodyRef.value) bodyRef.value.scrollTop = 7 * HOUR_H; }); // ~07:00
</script>

<template>
  <div class="tg" :style="{ '--cols': days.length, '--hour-h': `${HOUR_H}px` }">
    <!-- header: day columns -->
    <div class="tg-head">
      <div class="tg-corner" />
      <div
        v-for="day in days"
        :key="day.toISOString()"
        class="tg-colhead"
        :class="{ today: isToday(day) }"
      >
        <span class="dow">{{ colLabel(day).dow }}</span>
        <span class="num" :class="{ 'is-today': isToday(day) }">{{ colLabel(day).num }}</span>
      </div>
    </div>

    <!-- all-day row -->
    <div v-if="allDaySegments.length" class="tg-allday" :style="{ height: `${allDayLanes * 22 + 6}px` }">
      <div class="tg-allday-gutter">all-day</div>
      <div class="tg-allday-lanes">
        <button
          v-for="seg in allDaySegments"
          :key="seg.ev.subscription_id + seg.ev.start + seg.lane"
          class="tg-allday-bar"
          :class="{ done: seg.ev.status !== 'active' }"
          :style="segStyle(seg)"
          @click.stop="emit('select', seg.ev)"
        >{{ seg.ev.title }}</button>
      </div>
    </div>

    <!-- scrollable time grid -->
    <div ref="bodyRef" class="tg-body">
      <div class="tg-gutter">
        <div v-for="h in HOURS" :key="h" class="tg-gutter-row" :style="{ height: `${HOUR_H}px` }">
          <span v-if="h > 0">{{ pad(h) }}:00</span>
        </div>
      </div>
      <div
        v-for="day in days"
        :key="day.toISOString()"
        class="tg-lane"
        :class="{ today: isToday(day) }"
      >
        <div
          v-for="h in HOURS"
          :key="h"
          class="tg-slot"
          :style="{ height: `${HOUR_H}px` }"
          @click="onSlotClick(day, h)"
        />
        <button
          v-for="p in timedFor(day)"
          :key="p.ev.subscription_id + p.ev.start"
          class="tg-block"
          :class="{ done: p.ev.status !== 'active' }"
          :style="blockStyle(p)"
          @click.stop="emit('select', p.ev)"
        >
          <span class="b-time">{{ timeLabel(p.ev.start) }}</span>
          <span class="b-title">{{ p.ev.title }}</span>
        </button>
        <div v-if="isToday(day)" class="tg-now" :style="{ top: `${nowTop()}px` }" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.tg { display: flex; flex-direction: column; border: 1px solid var(--border-color); border-radius: 12px; overflow: hidden; background: var(--surface-color); }

.tg-head, .tg-allday { display: grid; grid-template-columns: 64px repeat(var(--cols), 1fr); }
.tg-head { border-bottom: 1px solid var(--border-color); background: var(--hover-bg); }
.tg-corner { border-right: 1px solid var(--border-color); }
.tg-colhead { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 8px 4px; border-right: 1px solid var(--border-color); }
.tg-colhead:last-child { border-right: none; }
.tg-colhead .dow { font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--text-tertiary); }
.tg-colhead .num { font-size: 1rem; font-weight: 600; color: var(--text-secondary); width: 28px; height: 28px; display: grid; place-items: center; border-radius: 50%; }
.tg-colhead .num.is-today { background: var(--primary-color); color: #fff; }

.tg-allday { border-bottom: 1px solid var(--border-color); }
.tg-allday-gutter { font-size: .64rem; color: var(--text-tertiary); padding: 4px 6px; text-align: right; border-right: 1px solid var(--border-color); }
.tg-allday-lanes { position: relative; grid-column: 2 / -1; }
.tg-allday-bar {
  position: absolute; height: 19px; line-height: 19px;
  border: none; cursor: pointer; background: var(--primary-color); color: #fff;
  border-radius: 4px; padding: 0 8px; font-size: .72rem; font-weight: 500;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tg-allday-bar.done { opacity: .55; }

.tg-body { display: grid; grid-template-columns: 64px repeat(var(--cols), 1fr); overflow-y: auto; max-height: 62vh; position: relative; }
.tg-gutter { position: sticky; left: 0; }
.tg-gutter-row { position: relative; border-bottom: 1px solid transparent; text-align: right; padding-right: 6px; }
.tg-gutter-row span { font-size: .66rem; color: var(--text-tertiary); position: relative; top: -7px; }

.tg-lane { position: relative; border-left: 1px solid var(--border-color); }
.tg-lane.today { background: color-mix(in srgb, var(--primary-color) 5%, transparent); }
.tg-slot { border-bottom: 1px solid var(--border-color); cursor: pointer; box-sizing: border-box; }
.tg-slot:hover { background: var(--surface-hover); }

.tg-block {
  position: absolute; z-index: 2; overflow: hidden;
  border: none; border-left: 3px solid color-mix(in srgb, var(--primary-color) 60%, #000);
  background: var(--primary-color); color: #fff;
  border-radius: 5px; padding: 2px 6px; text-align: left; cursor: pointer;
  display: flex; flex-direction: column; gap: 1px;
}
.tg-block.done { opacity: .55; }
.tg-block .b-time { font-size: .64rem; opacity: .9; }
.tg-block .b-title { font-size: .74rem; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.tg-now { position: absolute; left: 0; right: 0; height: 0; border-top: 2px solid var(--danger-color); z-index: 3; pointer-events: none; }
.tg-now::before { content: ''; position: absolute; left: -4px; top: -4px; width: 8px; height: 8px; border-radius: 50%; background: var(--danger-color); }
</style>
