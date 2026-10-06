// When the Vector Embedding form must refuse to turn on.
//
// Both embedding models run on PyTorch, which has no build for an Intel Mac
// (or Windows on ARM). The server names such features in the capabilities'
// `unavailable_features`; before it did, the Setup Wizard on an Intel Mac let
// the switch on, and the pip install that followed failed every feature in
// the request. An older server sends no map at all, and that must read as
// "nothing known to be unavailable", not as "everything is".
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const api = await load('src/services/featureAvailability.ts')

const NO_TORCH = 'Vector Embedding runs on PyTorch, which publishes no builds for Intel Macs on Python 3.13 or newer.'

test('both models unavailable turns Vector Embedding off, with the reason once', () => {
  const caps = { unavailable_features: { 'embedding.me5': NO_TORCH, 'embedding.gemma': NO_TORCH } }
  assert.equal(api.embeddingUnavailable(caps), NO_TORCH)
  assert.equal(api.embeddingProviderUnavailable(caps, 'me5'), NO_TORCH)
  assert.equal(api.embeddingProviderUnavailable(caps, 'gemma'), NO_TORCH)
})

test('one model left keeps the switch usable and greys out only the other', () => {
  const caps = { unavailable_features: { 'embedding.gemma': 'no build' } }
  assert.equal(api.embeddingUnavailable(caps), null)
  assert.equal(api.embeddingProviderUnavailable(caps, 'me5'), null)
  assert.equal(api.embeddingProviderUnavailable(caps, 'gemma'), 'no build')
})

test('an older server, or capabilities still loading, reads as available', () => {
  for (const caps of [null, undefined, { services: {}, docker_available: false }]) {
    assert.equal(api.embeddingUnavailable(caps), null)
    assert.equal(api.embeddingProviderUnavailable(caps, 'me5'), null)
  }
})

test('other unavailable features do not touch Vector Embedding', () => {
  const caps = { unavailable_features: { tags: 'needs macOS 15' } }
  assert.equal(api.embeddingUnavailable(caps), null)
})

test('the form offers exactly the models the backend gates', () => {
  // app/features/manifest.py: embedding.me5 and embedding.gemma.
  assert.deepEqual([...api.EMBEDDING_PROVIDERS], ['me5', 'gemma'])
})
