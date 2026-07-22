/**
 * Calendar date math + layout helpers (native Date, no extra dependency).
 *
 * Shared by the Month / Week / Day / Agenda views. All datetimes are naive-local
 * ISO strings 'YYYY-MM-DDTHH:MM:SS' (what the backend emits); we parse them as
 * local time and never apply timezone conversion.
 */
import type { CalendarOccurrence } from '../../services/calendarApi';

export type CalView = 'month' | 'week' | 'day' | 'agenda' | 'year';
export type CalEvent = CalendarOccurrence;

export const DOW_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
export const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];

export function pad(n: number): string { return String(n).padStart(2, '0'); }

export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
export function isoDateTime(d: Date): string {
  return `${isoDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Parse a naive-local ISO string (date or datetime) as a local Date. */
export function parseLocal(iso: string): Date {
  if (!iso) return new Date(NaN);
  const [datePart, timePart] = iso.split('T');
  const [y, m, d] = datePart.split('-').map(Number);
  let hh = 0, mm = 0, ss = 0;
  if (timePart) {
    const t = timePart.replace('Z', '').split(/[+\-]/)[0];
    [hh = 0, mm = 0, ss = 0] = t.split(':').map(Number);
  }
  return new Date(y, (m || 1) - 1, d || 1, hh, mm, ss);
}

export function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}
export function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n, d.getHours(), d.getMinutes(), d.getSeconds());
}
export function addMonths(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}
export function addYears(d: Date, n: number): Date {
  return new Date(d.getFullYear() + n, d.getMonth(), 1);
}
export function sameDay(a: Date, b: Date): boolean { return isoDate(a) === isoDate(b); }
export function isToday(d: Date): boolean { return sameDay(d, new Date()); }

/** Monday-first weekday index 0..6. */
export function mondayIdx(d: Date): number { return (d.getDay() + 6) % 7; }

/** 42-cell (6-week) Monday-first month grid containing `anchor`. */
export function monthGrid(anchor: Date): Date[] {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const start = addDays(first, -mondayIdx(first));
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

/** The 7 Monday-first days of the week containing `anchor`. */
export function weekDays(anchor: Date): Date[] {
  const start = addDays(startOfDay(anchor), -mondayIdx(anchor));
  return Array.from({ length: 7 }, (_, i) => addDays(start, i));
}

/** The fetch window (inclusive) for a view, as naive-local ISO datetimes. */
export function viewRange(view: CalView, anchor: Date): { from: string; to: string } {
  if (view === 'day') {
    const s = startOfDay(anchor);
    return { from: isoDateTime(s), to: `${isoDate(s)}T23:59:59` };
  }
  if (view === 'week') {
    const days = weekDays(anchor);
    return { from: isoDateTime(days[0]), to: `${isoDate(days[6])}T23:59:59` };
  }
  if (view === 'agenda') {
    const s = startOfDay(anchor);
    return { from: isoDateTime(s), to: `${isoDate(addDays(s, 60))}T23:59:59` };
  }
  if (view === 'year') {
    const y = anchor.getFullYear();
    return { from: `${y}-01-01T00:00:00`, to: `${y}-12-31T23:59:59` };
  }
  const g = monthGrid(anchor);
  return { from: isoDateTime(g[0]), to: `${isoDate(g[41])}T23:59:59` };
}

export function titleFor(view: CalView, anchor: Date): string {
  if (view === 'day') {
    return anchor.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
  }
  if (view === 'week') {
    const days = weekDays(anchor);
    const a = days[0], b = days[6];
    const sameMonth = a.getMonth() === b.getMonth();
    const left = a.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const right = b.toLocaleDateString(undefined, sameMonth ? { day: 'numeric', year: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
    return `${left} – ${right}`;
  }
  if (view === 'year') return String(anchor.getFullYear());
  return `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`;
}

export function navigate(view: CalView, anchor: Date, delta: number): Date {
  if (view === 'day') return addDays(anchor, delta);
  if (view === 'week') return addDays(anchor, delta * 7);
  if (view === 'agenda') return addDays(anchor, delta * 30);
  if (view === 'year') return addYears(anchor, delta);
  return addMonths(anchor, delta);
}

// ── event classification ─────────────────────────────────────────────────

/** True for an all-day event or one whose span crosses a day boundary. */
export function isMultiDayOrAllDay(ev: CalEvent): boolean {
  if (ev.all_day) return true;
  const s = parseLocal(ev.start), e = parseLocal(ev.end);
  return !sameDay(s, e) && (e.getTime() - s.getTime()) >= 12 * 3600 * 1000;
}

/** Number of events touching each day, keyed by `isoDate`. Timed/single-day
 *  events count on their start day; multi-day/all-day events count on every day
 *  they cover. Used by the year view to place a per-day dot. */
export function countByDay(events: CalEvent[]): Map<string, number> {
  const counts = new Map<string, number>();
  const bump = (d: Date) => { const k = isoDate(d); counts.set(k, (counts.get(k) ?? 0) + 1); };
  for (const ev of events) {
    const s = startOfDay(parseLocal(ev.start));
    if (isMultiDayOrAllDay(ev)) {
      let e = startOfDay(parseLocal(ev.end));
      // a 00:00 end on the following day means the event ends the previous day
      const eRaw = parseLocal(ev.end);
      if (eRaw.getHours() === 0 && eRaw.getMinutes() === 0 && e.getTime() > s.getTime()) {
        e = addDays(e, -1);
      }
      for (let d = s; d.getTime() <= e.getTime(); d = addDays(d, 1)) bump(d);
    } else {
      bump(s);
    }
  }
  return counts;
}

export function minutesOfDay(d: Date): number { return d.getHours() * 60 + d.getMinutes(); }

export function timeLabel(iso: string): string {
  const d = parseLocal(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ── overlap packing for timed events in a single day column ────────────────

export interface Packed { ev: CalEvent; startMin: number; endMin: number; col: number; cols: number; }

/** Google-Calendar-style overlap packing: assign each timed event a column and
 *  the cluster width so concurrent events sit side-by-side. */
export function packDay(events: CalEvent[], dayStartMin = 0, dayEndMin = 24 * 60): Packed[] {
  const items = events
    .map((ev) => {
      const s = parseLocal(ev.start), e = parseLocal(ev.end);
      const startMin = Math.max(dayStartMin, minutesOfDay(s));
      let endMin = minutesOfDay(e);
      if (e.getTime() <= s.getTime()) endMin = startMin + 30;
      // Clamp multi-day timed spillover to the end of the day.
      if (!sameDay(s, e)) endMin = dayEndMin;
      return { ev, startMin, endMin: Math.max(endMin, startMin + 15) };
    })
    .sort((a, b) => a.startMin - b.startMin || a.endMin - b.endMin);

  const packed: Packed[] = [];
  let cluster: typeof items = [];
  let clusterEnd = -1;

  const flush = () => {
    if (!cluster.length) return;
    const cols: number[] = []; // track end-min per column
    const placed: { it: typeof items[number]; col: number }[] = [];
    for (const it of cluster) {
      let col = cols.findIndex((end) => end <= it.startMin);
      if (col === -1) { col = cols.length; cols.push(it.endMin); }
      else cols[col] = it.endMin;
      placed.push({ it, col });
    }
    const total = cols.length;
    for (const p of placed) {
      packed.push({ ev: p.it.ev, startMin: p.it.startMin, endMin: p.it.endMin, col: p.col, cols: total });
    }
    cluster = [];
  };

  for (const it of items) {
    if (cluster.length && it.startMin >= clusterEnd) flush();
    cluster.push(it);
    clusterEnd = Math.max(clusterEnd, it.endMin);
  }
  flush();
  return packed;
}

// ── month multi-day spanning segments (per week row) ───────────────────────

export interface DaySegment { ev: CalEvent; startCol: number; span: number; lane: number; continuesLeft: boolean; continuesRight: boolean; }

/** Lay multi-day/all-day events out as lane-stacked bars clipped to a row of
 *  consecutive days (length-generic: 7 for a month week, N for a time-grid). */
export function weekSegments(week: Date[], events: CalEvent[]): DaySegment[] {
  const lastCol = week.length - 1;
  const weekStart = startOfDay(week[0]);
  const weekEnd = startOfDay(week[lastCol]);
  const spanning = events
    .filter(isMultiDayOrAllDay)
    .map((ev) => {
      const s = startOfDay(parseLocal(ev.start));
      // all-day/multi-day end is inclusive of the last covered day
      let e = startOfDay(parseLocal(ev.end));
      // a 00:00 end of the day after means the event ends the previous day
      const eRaw = parseLocal(ev.end);
      if (eRaw.getHours() === 0 && eRaw.getMinutes() === 0 && e.getTime() > s.getTime()) {
        e = addDays(e, -1);
      }
      return { ev, s, e };
    })
    .filter(({ s, e }) => e.getTime() >= weekStart.getTime() && s.getTime() <= weekEnd.getTime())
    .sort((a, b) => a.s.getTime() - b.s.getTime() || (b.e.getTime() - b.s.getTime()) - (a.e.getTime() - a.s.getTime()));

  const laneEnds: number[] = []; // last occupied col per lane
  const out: DaySegment[] = [];
  for (const { ev, s, e } of spanning) {
    const startCol = Math.max(0, Math.round((s.getTime() - weekStart.getTime()) / 86400000));
    const endCol = Math.min(lastCol, Math.round((e.getTime() - weekStart.getTime()) / 86400000));
    const span = Math.max(1, endCol - startCol + 1);
    let lane = laneEnds.findIndex((end) => end < startCol);
    if (lane === -1) { lane = laneEnds.length; laneEnds.push(endCol); }
    else laneEnds[lane] = endCol;
    out.push({
      ev, startCol, span, lane,
      continuesLeft: s.getTime() < weekStart.getTime(),
      continuesRight: e.getTime() > weekEnd.getTime(),
    });
  }
  return out;
}
