// What a turn says about how long it took — the bubble's summary line and the
// Thinking Process step labels, which share one module so they cannot disagree.
//
// The bug this exists for: latency was timed entirely in the browser, against a
// baseline the store set when the FIRST frame created the assistant bubble. For
// a turn whose first frame was a token, the baseline and the milestone were the
// same instant, so the bubble read "First token: 0ms | Total: 794ms" — a first
// token that took no time, and a total that left out the four seconds before
// the model spoke. And with nothing persisted, reopening a conversation showed
// no timings at all.
//
// So the server times its own turn now, and those numbers (``firstStepMs`` /
// ``firstTokenMs`` / ``totalMs``, ``elapsedMs`` per step) are what these labels
// prefer: they ride the live frames AND the stored row, so a bubble reads the
// same after a refresh as it did while it streamed. This tab's own stamps
// remain, but only to carry a turn that is still mid-flight.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const { backfillLegacyTotals, formatLatencyMs, latencySummary, stepElapsedLabel } =
  await load('src/utils/latencyLabels.ts')

const at = ms => new Date(ms)

// ── the formatter ───────────────────────────────────────────────────────────

test('sub-second stays in milliseconds, a second and over reads in seconds', () => {
  assert.equal(formatLatencyMs(0), '0ms')
  assert.equal(formatLatencyMs(999), '999ms')
  assert.equal(formatLatencyMs(1000), '1.0s')
  assert.equal(formatLatencyMs(4823), '4.8s')
})

// ── the bubble's summary ────────────────────────────────────────────────────

test('the server numbers are what a finished turn reports', () => {
  const summary = latencySummary({
    firstStepMs: 5620, firstTokenMs: 12743, totalMs: 13221,
  })
  assert.equal(summary.text, 'First step: 5.6s | First token: 12.7s | Total: 13.2s')
  assert.equal(summary.approximate, false)
})

test('a turn that called no tool has no first step to report', () => {
  const summary = latencySummary({ firstTokenMs: 2945, totalMs: 3333 })
  assert.equal(summary.text, 'First token: 2.9s | Total: 3.3s')
})

test('the regression: a first token is measured from the send, not from itself', () => {
  // The exact shape that produced "First token: 0ms": the first frame of the
  // turn is a token, and it is the frame that created the bubble. What fixes it
  // is the baseline — the send — not the milestone.
  const sentAt = 1_000_000
  const summary = latencySummary({
    requestSentAt: sentAt,
    firstTokenAt: sentAt + 4823,
    completedAt: sentAt + 5617,
  })
  assert.equal(summary.text, 'First token: 4.8s | Total: 5.6s')
})

test('a milestone that lands on the baseline is dropped, not printed as zero', () => {
  // A run this tab did not start has no send to measure from and falls back to
  // its first frame, which IS the first token. "0ms" says nothing, so the label
  // that would carry it is left out.
  const firstFrame = 1_000_000
  const summary = latencySummary({
    firstEventAt: firstFrame,
    firstTokenAt: firstFrame,
    completedAt: firstFrame + 900,
  })
  assert.equal(summary.text, 'Total: 900ms')
})

test('the server overrides what this tab measured while the turn streamed', () => {
  // Both present is the moment `complete` lands. Live and reloaded must agree,
  // or the bubble changes its story on refresh.
  const sentAt = 1_000_000
  const summary = latencySummary({
    requestSentAt: sentAt,
    firstTokenAt: sentAt + 5000,
    completedAt: sentAt + 6000,
    firstTokenMs: 4823,
    totalMs: 5617,
  })
  assert.equal(summary.text, 'First token: 4.8s | Total: 5.6s')
})

test('a turn from before any of this was timed says so with a tilde', () => {
  const summary = latencySummary({ totalMsApprox: 14266 })
  assert.equal(summary.text, 'Total: ~14.3s')
  assert.equal(summary.approximate, true)
})

test('a measured total always beats the inferred one', () => {
  const summary = latencySummary({ totalMs: 13221, totalMsApprox: 14266 })
  assert.equal(summary.text, 'Total: 13.2s')
  assert.equal(summary.approximate, false)
})

test('nothing to say yet renders nothing at all', () => {
  assert.equal(latencySummary(undefined), null)
  assert.equal(latencySummary({}), null)
  // A bubble that exists but has not reached a milestone.
  assert.equal(latencySummary({ requestSentAt: 1_000_000 }), null)
})

// ── the timeline's per-step labels ──────────────────────────────────────────

test('the first step is measured from the start of the turn', () => {
  assert.equal(stepElapsedLabel({ elapsedMs: 5620 }, undefined), ' · 5.6s')
})

test('later steps are measured from the step before them', () => {
  assert.equal(
    stepElapsedLabel({ elapsedMs: 9946 }, { elapsedMs: 5620 }),
    ' · 4.3s',
  )
})

test('steps from a run with no server stamps fall back to arrival times', () => {
  const sentAt = 1_000_000
  assert.equal(
    stepElapsedLabel({ receivedAt: sentAt + 1200 }, undefined, sentAt),
    ' · 1.2s',
  )
  assert.equal(
    stepElapsedLabel(
      { receivedAt: sentAt + 3700 }, { receivedAt: sentAt + 1200 }, sentAt,
    ),
    ' · 2.5s',
  )
})

test('a step with nothing to measure against is left unlabelled', () => {
  // Reloaded steps from before the stamp: no elapsed, and no arrival time
  // either, because that only ever existed in the tab that watched them.
  assert.equal(stepElapsedLabel({}, undefined), '')
  // The first step of a live run this tab did not start: no baseline.
  assert.equal(stepElapsedLabel({ receivedAt: 1_000_000 }, undefined), '')
  assert.equal(stepElapsedLabel(undefined, undefined), '')
})

test('two steps in the same instant get no label rather than a zero', () => {
  assert.equal(stepElapsedLabel({ elapsedMs: 5620 }, { elapsedMs: 5620 }), '')
})

// ── conversations from before any of this was recorded ──────────────────────

test('an old turn gets its total from the gap between its two rows', () => {
  // The real numbers off a conversation that predates the change.
  const messages = [
    { role: 'user', timestamp: at(1790068882095) },
    { role: 'assistant', timestamp: at(1790068896361) },
  ]
  backfillLegacyTotals(messages)
  assert.equal(latencySummary(messages[1].latency).text, 'Total: ~14.3s')
})

test('a turn the server timed keeps its own number', () => {
  const messages = [
    { role: 'user', timestamp: at(1000) },
    { role: 'assistant', timestamp: at(99000), latency: { totalMs: 13221 } },
  ]
  backfillLegacyTotals(messages)
  assert.equal(messages[1].latency.totalMsApprox, undefined)
  assert.equal(latencySummary(messages[1].latency).text, 'Total: 13.2s')
})

test('only an agent bubble directly answering a user one qualifies', () => {
  const messages = [
    // An automation reporting in: nobody sent anything, so nothing to subtract.
    { role: 'assistant', timestamp: at(2000), isEventResult: true },
    { role: 'assistant', timestamp: at(5000) },
    { role: 'user', timestamp: at(6000) },
    // A trigger the gate filtered out — no turn ran at all.
    { role: 'assistant', timestamp: at(7000), isRejectedTrigger: true },
  ]
  backfillLegacyTotals(messages)
  assert.ok(messages.every(m => m.latency === undefined))
})

test('an implausible gap is left alone rather than reported', () => {
  // Rows that only look adjacent — a conversation picked up the next morning.
  const messages = [
    { role: 'user', timestamp: at(0) },
    { role: 'assistant', timestamp: at(9 * 60 * 60 * 1000) },
  ]
  backfillLegacyTotals(messages)
  assert.equal(messages[1].latency, undefined)
})
