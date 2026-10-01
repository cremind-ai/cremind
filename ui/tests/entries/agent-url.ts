// Test entry for tests/agent-url.test.mjs: where the UI looks for its backend,
// the settings store whose agentUrl the first-run gate reads, and the
// embedding store App.vue connects with it — together with the Pinia they run
// on, since harness.load() bundles a fresh copy per call.
export { createPinia, setActivePinia } from 'pinia';
export { defaultAgentOrigin, getAgentUrl } from '../../src/services/runtimeConfig';
export { useEmbeddingStatusStore } from '../../src/stores/embeddingStatus';
export { useSettingsStore } from '../../src/stores/settings';
