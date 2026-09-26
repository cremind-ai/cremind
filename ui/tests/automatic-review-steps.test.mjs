// The Thinking Process shows the agent's automatic document reads for what
// they are: reads the agent made itself after a search returned several
// relevant files, not calls the model chose.
//
// The server marks such a call's ``thinking`` frame with ``Origin`` (and the
// persisted step with ``origin``); the timeline groups those calls apart from
// the model's calls of the same step and labels them. A mapping that dropped
// the field would show the reads as if the model had made them — no error,
// just a trace that tells the wrong story.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const { groupThinkingSteps, thinkingStepFromFrame, thinkingStepsFromRecord } =
  await load('src/utils/streamFrames.ts')

const usage = { input_tokens: 100, output_tokens: 7 }

test('a live frame and a stored row both carry the origin of an automatic read', () => {
  const live = thinkingStepFromFrame({
    Step: 1, Call_Id: 'call_docreview_1', Tool: 'documentation_search__read',
    Tool_Input: '{"file": "[doc:r9p2238f#43e67684]"}', Origin: 'document_review', Token_Usage: null,
  })
  assert.equal(live.origin, 'document_review')
  assert.equal(live.tokenUsage, null)

  const [stored] = thinkingStepsFromRecord([{
    step: 1, call_id: 'call_docreview_1', tool: 'documentation_search__read',
    tool_input: '{}', origin: 'document_review',
  }])
  assert.equal(stored.origin, 'document_review')

  // An ordinary model call has none.
  assert.equal(thinkingStepFromFrame({ Step: 1, Tool: 'documentation_search__search' }).origin, null)
})

test('automatic reads get their own row after the step that triggered them', () => {
  const steps = [
    thinkingStepFromFrame({ Step: 1, Call_Id: 'a', Tool: 'documentation_search__search', Token_Usage: usage }),
    thinkingStepFromFrame({ Step: 1, Call_Id: 'r1', Tool: 'documentation_search__read', Origin: 'document_review' }),
    thinkingStepFromFrame({ Step: 1, Call_Id: 'r2', Tool: 'documentation_search__read', Origin: 'document_review' }),
    thinkingStepFromFrame({ Step: 2, Call_Id: 'b', Tool: 'documentation_search__read', Token_Usage: usage }),
  ]
  const groups = groupThinkingSteps(steps)
  assert.deepEqual(groups.map(g => [g.step, g.origin, g.tools.map(t => t.callId)]), [
    [1, null, ['a']],
    [1, 'document_review', ['r1', 'r2']],
    [2, null, ['b']],
  ])
  // The model's step keeps its reasoning-call tokens; the agent's own row has none.
  assert.equal(groups[0].tokens.inputTokens, 100)
  assert.equal(groups[1].tokens, null)
})

test('parallel model calls of one step still share a row', () => {
  const groups = groupThinkingSteps([
    thinkingStepFromFrame({ Step: 3, Call_Id: 'x', Tool: 't1' }),
    thinkingStepFromFrame({ Step: 3, Call_Id: 'y', Tool: 't2' }),
  ])
  assert.equal(groups.length, 1)
  assert.deepEqual(groups[0].tools.map(t => t.callId), ['x', 'y'])
})
