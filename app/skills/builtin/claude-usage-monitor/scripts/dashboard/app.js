'use strict';
/* Claude Usage Monitor — dashboard.
   Account names come from Claude Code's config, so they are inserted with
   textContent only, never as HTML. */

const MIN = 60e3;
const HOUR = 60 * MIN;
const POLL_MS = 2000;

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const svgEl = (tag, attrs = {}) => {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) n.setAttribute(k, String(v));
  return n;
};

const pad2 = (n) => String(n).padStart(2, '0');
const clock = (ms) => {
  const d = new Date(Math.round(ms / MIN) * MIN); // resets land on xx:59:59.9
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};
const dayClock = (ms) => {
  const d = new Date(Math.round(ms / MIN) * MIN);
  return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${clock(ms)}`;
};
const whenClock = (ms, now) => (Math.abs(ms - now) > 20 * HOUR ? dayClock(ms) : clock(ms));
function dur(ms) {
  const m = Math.max(0, Math.round(ms / MIN));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}
function ago(ms) {
  const m = Math.round(ms / MIN);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} days ago`;
}
const approx = (w) => (w && w.estimated ? '≈' : '');
const pct = (w) => (w ? `${approx(w)}${Math.round(w.pct)}%` : '—');
const pctOf = (w) => (w ? w.pct : 0);
const rateText = (r) => `${r.perHour < 10 ? r.perHour.toFixed(1) : Math.round(r.perHour)}%/h`;
const SOURCE = { statusline: 'status line', 'claude-cache': 'Claude Code', limit: 'limit reached' };
const LIMIT_NAME = { session: '5-hour', weekly: 'weekly' };

let state = null;
let build = null; // dashboard files' version: reload when the server reports a newer one
let shownAlertId = null;
let selectedIndex = null; // chart hover/focus position
let failures = 0;

/* ---------------------------------------------------------------- theme */

(function initTheme() {
  let saved = null;
  try {
    saved = localStorage.getItem('theme');
  } catch {}
  // ?theme=dark pins a theme for this window, which survives a reload.
  const asked = new URLSearchParams(location.search).get('theme');
  if (asked === 'light' || asked === 'dark') {
    saved = asked;
    try {
      localStorage.setItem('theme', asked);
    } catch {}
  }
  if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved;
  $('theme-toggle').addEventListener('click', () => {
    const dark = matchMedia('(prefers-color-scheme: dark)').matches;
    const now = document.documentElement.dataset.theme || (dark ? 'dark' : 'light');
    const next = now === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem('theme', next);
    } catch {}
    if (state) renderChart(state);
  });
})();

/* ---------------------------------------------------------------- hero */

/** [heads-up, switch now] thresholds of a limit. */
function thresholds(s, kind) {
  const t = s.settings;
  return kind === 'weekly' ? [t.weeklyWarnPct, t.weeklyCritPct] : [t.warnPct, t.critPct];
}
function limitLevel(s, kind, w) {
  if (!w) return 'none';
  const [warn, crit] = thresholds(s, kind);
  return w.pct >= crit ? 'critical' : w.pct >= warn ? 'warning' : 'good';
}

function renderHero(s) {
  const hero = s.accounts.find((a) => a.id === s.heroId);
  const chip = $('hero-chip');
  const body = $('hero-body');
  chip.textContent = '';
  body.textContent = '';

  if (!hero) {
    $('hero-heading').textContent = 'Running now';
    body.append(el('p', 'empty', s.ready ? 'No account signed in to Claude Code yet.' : 'Reading session transcripts…'));
    return;
  }

  $('hero-heading').textContent = hero.active ? 'Running now' : 'Last active';
  const st = hero.status;
  const chipNode = el('span', 'chip');
  chipNode.dataset.level = st.level;
  chipNode.append(el('span', 'icon', st.icon), el('span', null, st.label));
  chip.append(chipNode);

  // Identity
  const top = el('div', 'hero-top');
  const idBox = el('div', 'hero-id');
  const nameRow = el('div', 'hero-name');
  if (hero.active || hero.running) {
    const dot = el('span', 'dot-live');
    dot.dataset.level = st.level;
    dot.style.display = 'inline-block';
    dot.style.marginRight = '8px';
    nameRow.append(dot);
  }
  nameRow.append(document.createTextNode(hero.displayName));
  idBox.append(nameRow);
  const meta = [hero.plan, hero.label && hero.email ? hero.email : null, hero.signedIn.length ? `${hero.signedIn.join(', ')} profile` : null]
    .filter(Boolean)
    .join(' · ');
  if (meta) idBox.append(el('p', 'sub', meta));
  top.append(idBox);
  body.append(top);

  // Both limits side by side: either one running out stops the account.
  const limits = el('div', 'hero-limits');
  for (const kind of ['session', 'weekly']) limits.append(limitPanel(s, hero, kind));
  body.append(limits);

  // Where the figures come from
  const src = el('p', 'source-line');
  const read = ['session', 'weekly'].filter((k) => hero[k] && hero[k].officialAt);
  if (read.some((k) => hero[k].estimated)) {
    const sameTime = read.length === 2 && Math.abs(hero.session.officialAt - hero.weekly.officialAt) < MIN;
    src.append(document.createTextNode('Estimated from session activity · last official reading'));
    read.forEach((k, i) => {
      const w = hero[k];
      src.append(document.createTextNode(`${i ? (sameTime ? ' and' : ',') : ''} ${LIMIT_NAME[k]} `), el('b', null, `${Math.round(w.official)}%`));
      if (!sameTime || i === read.length - 1) src.append(document.createTextNode(` at ${whenClock(w.officialAt, s.now)}`));
    });
    if (hero.source) src.append(document.createTextNode(` (${SOURCE[hero.source] || hero.source})`));
  } else if (hero.updatedAt) {
    src.append(document.createTextNode('Official reading from '));
    src.append(el('b', null, SOURCE[hero.source] || hero.source || 'Claude Code'));
    src.append(document.createTextNode(` · ${ago(s.now - hero.updatedAt)}`));
  }
  if (src.childNodes.length) body.append(src);

  // Facts: the forecast names whichever limit runs out first
  const facts = el('div', 'facts');
  const add = (label, value) => {
    const span = el('span');
    span.append(document.createTextNode(`${label} `), el('b', null, value));
    facts.append(span);
  };
  const f = hero.forecast;
  const hit = ['weekly', 'session'].find((k) => hero[k] && hero[k].pct >= 100 && hero[k].resetsAt);
  if (hit) {
    const w = hero[hit];
    add(`${hit === 'weekly' ? 'Weekly' : '5-hour'} limit reached · resets`, `${whenClock(w.resetsAt, s.now)} (in ${dur(w.resetsAt - s.now)})`);
  } else if (f && f.first) {
    const k = f.first.kind;
    add(`${k === 'weekly' ? 'Weekly' : '5-hour'} limit reached at`, `~${whenClock(f.first.limitAt, s.now)} (in ${dur(f.first.limitAt - s.now)})`);
    if (f.first.switchAt > s.now) add(`Switch by (${thresholds(s, k)[1]}% ${LIMIT_NAME[k]})`, `~${whenClock(f.first.switchAt, s.now)}`);
  } else if (f && f.idle) add('Pace', 'idle');
  else if (f) add('At this pace', 'both limits reset before they run out');
  if (hero.rate && f && !f.idle) add('Pace from', hero.rate.session.basis);
  for (const sc of hero.scoped || []) add(sc.label, pct(sc));
  if (hero.spend && hero.spend.limitUsd != null && hero.spend.usedUsd != null) add('Extra usage', `$${hero.spend.usedUsd.toFixed(2)} of $${hero.spend.limitUsd.toFixed(2)}`);
  body.append(facts);

  body.append(renderRec(s, hero));
}

/** One limit in the hero: figure, meter with the switch-now mark, pace and reset. */
function limitPanel(s, hero, kind) {
  const w = hero[kind];
  const name = LIMIT_NAME[kind];
  const level = limitLevel(s, kind, w);
  const box = el('div', 'limit');
  const fig = el('div', 'fig');
  fig.append(el('span', 'value', pct(w)), el('span', 'label', `of the ${name} limit`));
  box.append(fig);
  if (w) {
    const meter = el('div', 'meter lg');
    meter.dataset.level = level;
    meter.setAttribute('role', 'meter');
    meter.setAttribute('aria-valuenow', Math.round(w.pct));
    meter.setAttribute('aria-valuemin', '0');
    meter.setAttribute('aria-valuemax', '100');
    meter.setAttribute('aria-label', `${name} usage`);
    const fill = el('span');
    fill.style.width = `${Math.min(100, w.pct)}%`;
    const tick = el('div', 'tick');
    tick.style.left = `${thresholds(s, kind)[1]}%`;
    meter.append(fill, tick);
    box.append(meter);
  }
  const bits = [];
  const r = hero.rate && hero.rate[kind];
  if (r && hero.forecast && !hero.forecast.idle) bits.push(rateText(r));
  if (w && w.resetsAt) bits.push(`resets ${w.approxReset ? '≈' : ''}${whenClock(w.resetsAt, s.now)} (in ${dur(w.resetsAt - s.now)})`);
  else if (w && w.rolledOver) bits.push('reset — a new window opens with the next reply');
  if (bits.length) box.append(el('p', 'limit-sub', bits.join(' · ')));
  return box;
}

function renderRec(s, hero) {
  const rec = s.recommendation;
  const box = el('div', 'rec');
  const inUse = hero && (hero.active || hero.running);
  const past = (i) => inUse && (pctOf(hero.session) >= thresholds(s, 'session')[i] || pctOf(hero.weekly) >= thresholds(s, 'weekly')[i]);
  const urgent = past(0); // either limit past its heads-up
  const other = s.accounts.filter((a) => a.id !== (hero && hero.id));

  let level = 'good';
  let icon = '✓';
  const text = el('div', 'text');

  if (rec && rec.id) {
    const next = s.accounts.find((a) => a.id === rec.id);
    if (urgent) {
      level = past(1) ? 'critical' : 'warning';
      icon = '⚠';
      text.append(el('strong', null, `Switch to ${next.displayName}`));
      text.append(document.createTextNode(` — 5-hour ${pct(next.session)} used, weekly ${pct(next.weekly)}.`));
    } else {
      text.append(document.createTextNode('Next account when needed: '));
      text.append(el('strong', null, next.displayName));
      text.append(document.createTextNode(` (5-hour ${pct(next.session)}, weekly ${pct(next.weekly)}).`));
    }
    const how = el('p', 'sub');
    how.style.marginTop = '4px';
    how.append(document.createTextNode('In Claude Code: run '));
    how.append(el('code', null, '/login'));
    how.append(document.createTextNode(' and pick that account.'));
    text.append(how);
  } else if (rec && rec.freeAt) {
    level = 'serious';
    icon = '◔';
    const who = s.accounts.find((a) => a.id === rec.freeId);
    text.append(el('strong', null, 'Every other account is used up.'));
    text.append(document.createTextNode(` ${who ? who.displayName : 'The first one'} frees up at ${whenClock(rec.freeAt, s.now)} (in ${dur(rec.freeAt - s.now)}).`));
  } else if (!other.length) {
    level = 'none';
    icon = '…';
    text.append(document.createTextNode('Only one account is known so far. See "Tracking more accounts" below to add the others.'));
  } else {
    level = 'none';
    icon = '…';
    text.append(document.createTextNode('No usage figures for the other accounts yet — open their profiles below and run /usage.'));
  }

  box.dataset.level = level;
  box.append(el('span', 'icon', icon), text);
  return box;
}

/* ---------------------------------------------------------------- chart */

function renderChart(s) {
  const card = $('chart-card');
  const hero = s.accounts.find((a) => a.id === s.heroId);
  const series = (hero && hero.series) || [];
  const marks = (hero && hero.marks) || [];
  const session = hero && hero.session;
  if (!hero || !session || !session.resetsAt || series.length < 2) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const resetsAt = session.resetsAt;
  const startsAt = resetsAt - 5 * HOUR;
  const critPct = s.settings.critPct;
  // The weekly limit stops the account too: when it runs out inside this window, mark it.
  const fc = hero.forecast;
  const fw = fc && fc.weekly;
  const weeklyAt = fw && !fw.idle && !fw.resetsFirst && fw.limitAt < resetsAt ? Math.max(s.now, fw.limitAt) : null;

  const caption = $('chart-caption');
  caption.textContent = '';
  caption.append(document.createTextNode('Share of the 5-hour limit used by '));
  caption.append(el('b', null, hero.displayName));
  caption.append(document.createTextNode(`, ${clock(startsAt)}–${clock(resetsAt)}. Line: estimated from session activity; dots: official readings.`));
  if (weeklyAt != null) caption.append(document.createTextNode(` At this pace the weekly limit runs out first, at ~${clock(weeklyAt)}.`));

  const W = 880;
  const H = 250;
  const m = { top: 16, right: 48, bottom: 28, left: 34 };
  const x = (t) => m.left + ((t - startsAt) / (resetsAt - startsAt)) * (W - m.left - m.right);
  const y = (p) => H - m.bottom - (Math.max(0, Math.min(100, p)) / 100) * (H - m.top - m.bottom);

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
  svg.setAttribute('aria-label', `5-hour usage for ${hero.displayName}: ${pct(session)} used`);

  for (const p of [0, 25, 50, 75, 100]) {
    svg.append(svgEl('line', { class: p === 0 ? 'axis-line' : 'grid-line', x1: m.left, x2: W - m.right, y1: y(p), y2: y(p) }));
    const t = svgEl('text', { class: 'axis-text', x: m.left - 8, y: y(p) + 4, 'text-anchor': 'end' });
    t.textContent = `${p}`;
    svg.append(t);
  }

  // The switch threshold — a status rule, labelled so it is never colour alone
  svg.append(svgEl('line', { class: 'limit-line', x1: m.left, x2: W - m.right, y1: y(critPct), y2: y(critPct) }));
  const limitLabel = svgEl('text', { class: 'limit-text', x: W - m.right + 6, y: y(critPct) + 4 });
  limitLabel.textContent = `${critPct}% switch`;
  svg.append(limitLabel);

  const pts = series.map((p) => [x(p[0]), y(p[1])]);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  svg.append(svgEl('path', { class: 'series-area', d: `${line} L${pts[pts.length - 1][0].toFixed(1)} ${y(0)} L${pts[0][0].toFixed(1)} ${y(0)} Z` }));
  svg.append(svgEl('path', { class: 'series-line', d: line }));

  // Projection at the measured pace — dashed because it is a forecast, not data. It ends
  // where either limit runs out.
  const f = fc && fc.session;
  if (f && !f.idle && hero.rate) {
    const endT = Math.min(resetsAt, f.limitAt, weeklyAt ?? Infinity);
    const endP = session.pct + (hero.rate.session.perHour * (endT - s.now)) / HOUR;
    const last = pts[pts.length - 1];
    svg.append(svgEl('path', { class: 'series-proj', d: `M${last[0].toFixed(1)} ${last[1].toFixed(1)} L${x(endT).toFixed(1)} ${y(endP).toFixed(1)}` }));
    if (!f.resetsFirst && f.limitAt <= resetsAt && !(weeklyAt != null && weeklyAt < f.limitAt)) {
      svg.append(svgEl('circle', { class: 'end-dot', cx: x(f.limitAt), cy: y(100), r: 3.5, opacity: 0.8 }));
      const lab = svgEl('text', { class: 'axis-text', x: x(f.limitAt), y: y(100) - 8, 'text-anchor': 'middle' });
      lab.textContent = `~${clock(f.limitAt)}`;
      svg.append(lab);
    }
  }
  if (weeklyAt != null) {
    const wx = x(weeklyAt);
    svg.append(svgEl('line', { class: 'limit-line weekly', x1: wx, x2: wx, y1: m.top, y2: H - m.bottom }));
    const onRight = wx > (m.left + W - m.right) / 2;
    const lab = svgEl('text', { class: 'limit-text', x: onRight ? wx - 6 : wx + 6, y: m.top + 10, 'text-anchor': onRight ? 'end' : 'start' });
    lab.textContent = `weekly limit ~${clock(weeklyAt)}`;
    svg.append(lab);
  }

  const nowX = x(Math.min(s.now, resetsAt));
  svg.append(svgEl('line', { class: 'now-line', x1: nowX, x2: nowX, y1: m.top, y2: H - m.bottom }));
  for (const mk of marks) svg.append(svgEl('circle', { class: 'mark-dot', cx: x(mk[0]), cy: y(mk[1]), r: 4.5 }));
  const endDot = pts[pts.length - 1];
  svg.append(svgEl('circle', { class: 'end-dot', cx: endDot[0], cy: endDot[1], r: 4.5 }));
  const endLab = svgEl('text', { class: 'end-label', x: Math.min(endDot[0] + 8, W - m.right + 4), y: endDot[1] - 9 });
  endLab.textContent = pct(session);
  svg.append(endLab);

  for (let t = startsAt; t <= resetsAt + 1; t += HOUR) {
    const tx = svgEl('text', { class: 'axis-text', x: x(t), y: H - m.bottom + 16, 'text-anchor': 'middle' });
    tx.textContent = clock(t);
    svg.append(tx);
  }

  // Hover / focus layer — the crosshair finds the X, the tooltip reads the value
  const cross = svgEl('line', { class: 'crosshair', y1: m.top, y2: H - m.bottom, opacity: 0 });
  const hoverDot = svgEl('circle', { class: 'hover-dot', r: 4.5, opacity: 0 });
  const hit = svgEl('rect', {
    x: m.left,
    y: m.top,
    width: W - m.left - m.right,
    height: H - m.top - m.bottom,
    fill: 'transparent',
    tabindex: '0',
    role: 'application',
    'aria-label': 'Usage over the 5-hour window. Use the left and right arrow keys to read values.',
  });
  svg.append(cross, hoverDot, hit);

  const wrap = $('chart-wrap');
  wrap.textContent = '';
  wrap.append(svg);
  const tip = el('div', 'tooltip');
  tip.hidden = true;
  wrap.append(tip);

  const show = (i) => {
    selectedIndex = i;
    const sm = series[i];
    cross.setAttribute('x1', x(sm[0]));
    cross.setAttribute('x2', x(sm[0]));
    cross.setAttribute('opacity', '1');
    hoverDot.setAttribute('cx', x(sm[0]));
    hoverDot.setAttribute('cy', y(sm[1]));
    hoverDot.setAttribute('opacity', '1');
    tip.textContent = '';
    tip.append(el('div', 't-time', clock(sm[0])));
    const row = el('div', 't-row');
    row.append(el('span', 't-key'), el('span', 't-val', `≈${Math.round(sm[1])}%`), el('span', 't-name', '5-hour'));
    tip.append(row);
    const near = marks.find((mk) => Math.abs(mk[0] - sm[0]) <= 2 * MIN);
    if (near) tip.append(el('div', 't-note', `Official reading: ${Math.round(near[1])}%`));
    tip.hidden = false;
    const rect = wrap.getBoundingClientRect();
    const px = (x(sm[0]) / W) * rect.width;
    tip.style.left = `${Math.max(70, Math.min(rect.width - 70, px))}px`;
    tip.style.top = `${(y(sm[1]) / H) * rect.height - 12}px`;
  };
  const hide = () => {
    selectedIndex = null;
    cross.setAttribute('opacity', '0');
    hoverDot.setAttribute('opacity', '0');
    tip.hidden = true;
  };
  const nearest = (clientX) => {
    const rect = wrap.getBoundingClientRect();
    const t = startsAt + ((((clientX - rect.left) / rect.width) * W - m.left) / (W - m.left - m.right)) * (resetsAt - startsAt);
    let best = 0;
    for (let i = 1; i < series.length; i++) if (Math.abs(series[i][0] - t) < Math.abs(series[best][0] - t)) best = i;
    return best;
  };
  hit.addEventListener('pointermove', (e) => show(nearest(e.clientX)));
  hit.addEventListener('pointerleave', hide);
  hit.addEventListener('focus', () => show(selectedIndex ?? series.length - 1));
  hit.addEventListener('blur', hide);
  hit.addEventListener('keydown', (e) => {
    const i = selectedIndex ?? series.length - 1;
    if (e.key === 'ArrowLeft') (show(Math.max(0, i - 1)), e.preventDefault());
    else if (e.key === 'ArrowRight') (show(Math.min(series.length - 1, i + 1)), e.preventDefault());
    else if (e.key === 'Home') (show(0), e.preventDefault());
    else if (e.key === 'End') (show(series.length - 1), e.preventDefault());
    else if (e.key === 'Escape') hide();
  });

  renderChartTable(series, marks);
}

function renderChartTable(series, marks) {
  const host = $('chart-table');
  host.textContent = '';
  const rows = [
    ...series.filter((p, i) => i === series.length - 1 || new Date(p[0]).getMinutes() % 10 === 0).map((p) => [p[0], `≈${Math.round(p[1])}%`, 'estimate']),
    ...marks.map((p) => [p[0], `${Math.round(p[1])}%`, 'official reading']),
  ].sort((a, b) => b[0] - a[0]);
  const table = el('table');
  const thead = el('thead');
  const hr = el('tr');
  for (const h of ['Time', '5-hour', 'Kind']) hr.append(el('th', h === '5-hour' ? 'right' : null, h));
  thead.append(hr);
  const tbody = el('tbody');
  for (const r of rows) {
    const tr = el('tr');
    tr.append(el('td', 'num', clock(r[0])), el('td', 'num right', r[1]), el('td', null, r[2]));
    tbody.append(tr);
  }
  table.append(thead, tbody);
  host.append(table);
}

$('table-toggle').addEventListener('click', (e) => {
  const t = $('chart-table');
  t.hidden = !t.hidden;
  e.currentTarget.setAttribute('aria-expanded', String(!t.hidden));
});

/* ---------------------------------------------------------------- accounts */

function meterCell(w, level, label, sub) {
  const box = el('div');
  const cell = el('div', 'cell-meter');
  const meter = el('div', 'meter');
  meter.dataset.level = w ? level : 'none';
  meter.setAttribute('role', 'meter');
  meter.setAttribute('aria-valuenow', Math.round(pctOf(w)));
  meter.setAttribute('aria-valuemin', '0');
  meter.setAttribute('aria-valuemax', '100');
  meter.setAttribute('aria-label', label);
  const fill = el('span');
  fill.style.width = `${Math.min(100, pctOf(w))}%`;
  meter.append(fill);
  cell.append(meter, el('span', 'pct', pct(w)));
  box.append(cell);
  if (sub) box.append(sub);
  return box;
}

/** "62% left · resets 21:40" under each meter — the figure a spare account is chosen by. */
function leftLine(w, now) {
  const sub = el('div', 'cell-sub');
  if (!w) return sub;
  sub.append(el('b', null, `${approx(w)}${Math.max(0, Math.round(100 - w.pct))}% left`));
  if (w.resetsAt) sub.append(document.createTextNode(` · resets ${w.approxReset ? '≈' : ''}${whenClock(w.resetsAt, now)}`));
  else if (w.rolledOver) sub.append(document.createTextNode(' · fresh window'));
  return sub;
}

function renderAccounts(s) {
  const body = $('accounts-body');
  body.textContent = '';

  if (!s.accounts.length) {
    const tr = el('tr');
    const td = el('td', 'empty');
    td.colSpan = 6;
    td.textContent = s.ready ? 'No accounts yet. Sign in to Claude Code and it will appear here.' : 'Reading session transcripts…';
    tr.append(td);
    body.append(tr);
  }

  for (const a of s.accounts) {
    const tr = el('tr');
    tr.dataset.active = String(a.active);

    // Account
    const tdName = el('td');
    const acct = el('div', 'acct');
    const dot = el('span', 'dot-live');
    dot.dataset.level = a.active || a.running ? a.status.level : 'none';
    if (!a.active && !a.running) dot.style.background = 'transparent';
    acct.append(dot);
    const who = el('div', 'who');
    who.append(el('div', 'nm', a.displayName));
    const where = a.signedIn.length ? `signed in: ${a.signedIn.join(', ')}` : 'not signed in here';
    const bits = [a.plan, a.label && a.email ? a.email : null, where].filter(Boolean).join(' · ');
    who.append(el('div', 'meta', bits));
    acct.append(who);
    tdName.append(acct);
    tr.append(tdName);

    // Status
    const tdStatus = el('td');
    const chip = el('span', 'chip plain');
    chip.dataset.level = a.status.level;
    chip.append(el('span', 'icon', a.status.icon), el('span', null, a.active ? 'Active' : a.status.label));
    tdStatus.append(chip);
    tr.append(tdStatus);

    const td5 = el('td');
    td5.append(meterCell(a.session, limitLevel(s, 'session', a.session), `5-hour usage for ${a.displayName}`, leftLine(a.session, s.now)));
    tr.append(td5);

    const tdWk = el('td');
    tdWk.append(meterCell(a.weekly, limitLevel(s, 'weekly', a.weekly), `Weekly usage for ${a.displayName}`, leftLine(a.weekly, s.now)));
    tr.append(tdWk);

    // Freshness
    const tdWhen = el('td', 'when hide-sm');
    if (a.running) tdWhen.append(document.createTextNode('live'));
    else if (a.updatedAt) tdWhen.append(document.createTextNode(ago(s.now - a.updatedAt)));
    else tdWhen.append(document.createTextNode('—'));
    if (a.updatedAt) tdWhen.append(el('div', 'cell-sub', `official ${clock(a.updatedAt)}${a.source ? ` · ${SOURCE[a.source] || a.source}` : ''}`));
    tr.append(tdWhen);

    // Actions
    const tdAct = el('td', 'right');
    const actions = el('div', 'row-actions');
    const rename = el('button', 'ghost small', 'Rename');
    rename.title = 'Give this account a short name';
    rename.addEventListener('click', async () => {
      const next = prompt(`Short name for ${a.email || a.displayName}:`, a.label || '');
      if (next === null) return;
      await post(`/api/accounts/${a.id}`, { label: next });
      refresh();
    });
    const del = el('button', 'ghost small', 'Remove');
    del.title = 'Stop tracking this account';
    del.addEventListener('click', async () => {
      if (!confirm(`Stop tracking ${a.displayName}?${a.signedIn.length ? ' It is signed in to a profile, so it will reappear.' : ''}`)) return;
      await fetch(`/api/accounts/${a.id}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      refresh();
    });
    actions.append(rename, del);
    tdAct.append(actions);
    tr.append(tdAct);

    body.append(tr);
  }

  // Capacity note — the figure that matters across a multi-day run
  const known = s.accounts.filter((a) => a.hasData);
  const ready = known.filter((a) => pctOf(a.session) < s.settings.critPct && pctOf(a.weekly) < s.settings.weeklyCritPct).length;
  const weeklyLeft = known.reduce((sum, a) => sum + Math.max(0, 100 - pctOf(a.weekly)), 0);
  $('accounts-note').textContent = known.length
    ? `${ready} ready now · weekly headroom ${Math.round(weeklyLeft)}% (≈ ${(weeklyLeft / 100).toFixed(1)} accounts)`
    : '';
}

function renderProfiles(s) {
  const list = $('profiles-list');
  list.textContent = '';
  for (const p of s.profiles) {
    const li = el('li');
    li.append(el('span', 'pname', p.main ? 'main' : p.name));
    const acct = el('span', 'pacct', p.account ? `Signed in: ${p.account}` : 'Not signed in yet');
    const where = p.main
      ? 'Your normal Claude Code configuration'
      : p.kind === 'cremind'
        ? `This Cremind profile's own Claude Code login (${p.dir})`
        : p.dir;
    acct.append(el('span', 'pdir', where));
    li.append(acct);
    if (!p.main) {
      const btn = el('button', 'small', 'Open');
      btn.title = 'Open Claude Code in this profile to sign in or run /usage';
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const res = await post(`/api/profiles/${encodeURIComponent(p.name)}/open`, {});
        btn.textContent = res && res.ok ? 'Opened' : 'Could not open';
        setTimeout(() => {
          btn.textContent = 'Open';
          btn.disabled = false;
        }, 2000);
      });
      li.append(btn);
    }
    list.append(li);
  }
  if (s.profiles.length === 1) {
    const li = el('li');
    li.append(el('span', 'pacct', 'No extra profiles yet.'));
    list.append(li);
  }
}

/* ---------------------------------------------------------------- events, alerts, settings */

function renderEvents(s) {
  const list = $('events-list');
  list.textContent = '';
  if (!s.events.length) {
    const li = el('li');
    li.append(el('span', 'msg', 'Nothing yet.'));
    list.append(li);
    return;
  }
  for (const e of s.events) {
    const li = el('li');
    li.dataset.level = e.level === 'critical' ? 'critical' : e.level === 'warning' ? 'warning' : 'none';
    li.append(el('time', null, clock(e.t)));
    li.append(el('span', 'icon', e.level === 'critical' ? '⚠' : e.level === 'warning' ? '▲' : '·'));
    li.append(el('span', 'msg', e.text));
    list.append(li);
  }
}

let beeped = 0;
function maybeAlert(s) {
  const a = s.alert;
  if (!a || a.id === shownAlertId) return;
  shownAlertId = a.id;
  if (s.now - a.at > 2 * MIN) return; // old alert, already seen elsewhere
  document.title = `${a.level === 'critical' ? '⚠' : '▲'} ${a.title}`;
  setTimeout(() => (document.title = 'Claude Usage Monitor'), 60e3);
  if (s.settings.sound && Date.now() - beeped > 30e3) {
    beeped = Date.now();
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain).connect(ctx.destination);
      osc.frequency.value = a.level === 'critical' ? 740 : 520;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.14, ctx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5);
      osc.start();
      osc.stop(ctx.currentTime + 0.52);
      setTimeout(() => ctx.close(), 900);
    } catch {}
  }
}

function renderHeader(s) {
  const bits = [];
  const n = s.accounts.length;
  bits.push(`${n} account${n === 1 ? '' : 's'}`);
  const extra = s.profiles.length - 1;
  if (extra > 0) bits.push(`${extra} extra profile${extra === 1 ? '' : 's'}`);
  bits.push(s.ready ? 'live' : 'reading session transcripts…');
  $('subtitle').textContent = bits.join(' · ');

  // Only a conflicting status line is worth a banner: VS Code sessions don't need one.
  const banner = $('link-banner');
  const text = $('link-banner-text');
  banner.hidden = !s.link.other;
  if (s.link.other) {
    text.textContent = '';
    text.append(el('strong', null, 'Another status line is configured in Claude Code.'));
    text.append(document.createTextNode(' Terminal sessions will not send exact readings. Ask Cremind to replace it ('));
    text.append(el('code', null, 'statusline install --force'));
    text.append(document.createTextNode(') if you like; estimates from session activity work either way.'));
  }

  const hero = s.accounts.find((a) => a.id === s.heroId) || s.accounts[0];
  const note = $('calibration-note');
  if (hero && hero.calibration) {
    const c = hero.calibration;
    note.textContent = `for ${hero.plan || 'this plan'}, 1% of the 5-hour limit ≈ $${c.session.usdPerPct.toFixed(2)} and 1% of the weekly limit ≈ $${c.weekly.usdPerPct.toFixed(2)} of usage at list price (${
      c.session.n ? `calibrated from ${c.session.n} reading${c.session.n === 1 ? '' : 's'}` : 'measured on two Max 20x accounts; refined as readings arrive'
    }).`;
  }
  $('statusline-note').textContent = s.link.installed
    ? 'Terminal status line: connected — terminal sessions also report exact figures after every reply.'
    : 'Terminal status line: not installed (optional — ask Cremind to install the Claude usage status line for exact figures from terminal sessions).';
  if (s.eventTypes && s.eventTypes.length) {
    $('events-hint').textContent =
      `Each alert is a Cremind skill event (${s.eventTypes.join(', ')}). Ask Cremind to notify you about Claude usage alerts and it subscribes to them; with no subscription an alert only appears here.`;
  }
}

let settingsTouched = false;
const settingFields = ['warnPct', 'critPct', 'weeklyWarnPct', 'weeklyCritPct', 'etaAlertMin', 'events', 'sound'];

function renderSettings(s) {
  if (settingsTouched) return;
  for (const k of settingFields) {
    const node = $(k);
    if (node.type === 'checkbox') node.checked = !!s.settings[k];
    else node.value = s.settings[k];
  }
}

async function post(url, body) {
  try {
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

let saveTimer = null;
for (const k of settingFields) {
  const node = $(k);
  node.addEventListener('change', () => {
    settingsTouched = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      const payload = {};
      for (const f of settingFields) payload[f] = $(f).type === 'checkbox' ? $(f).checked : Number($(f).value);
      const out = await post('/api/settings', payload);
      settingsTouched = false;
      if (out && out.settings) {
        if (state) state.settings = out.settings;
        renderSettings({ settings: out.settings });
        const saved = $('settings-saved');
        saved.hidden = false;
        setTimeout(() => (saved.hidden = true), 1500);
      }
      refresh();
    }, 400);
  });
}

$('test-alert').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  const out = await post('/api/test-alert', { type: 'limit_warning' });
  btn.textContent = out && out.ok ? 'Sent as a limit_warning event' : 'Could not send';
  setTimeout(() => {
    btn.textContent = 'Send a test alert';
    btn.disabled = false;
  }, 2400);
  refresh();
});

/* ---------------------------------------------------------------- poll */

function render(s) {
  state = s;
  renderHeader(s);
  renderHero(s);
  renderChart(s);
  renderAccounts(s);
  renderProfiles(s);
  renderEvents(s);
  renderSettings(s);
  maybeAlert(s);
}

async function refresh() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    if (!res.ok) throw new Error(String(res.status));
    const s = await res.json();
    if (build && s.build && s.build !== build) return location.reload(); // the dashboard was updated
    build = s.build || build;
    failures = 0;
    document.body.classList.remove('stale');
    render(s);
  } catch {
    // Hold the previous render rather than flashing a skeleton.
    if (++failures === 3) {
      document.body.classList.add('stale');
      $('subtitle').textContent = 'Monitor not reachable — ask Cremind to open the Claude Usage Monitor dashboard to start it again.';
    }
  }
}

refresh();
setInterval(refresh, POLL_MS);
document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && refresh());
