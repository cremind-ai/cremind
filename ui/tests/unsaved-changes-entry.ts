// Bundle entry for tests/unsaved-changes.test.mjs: the snapshot composable
// together with the Vue it was bundled with (a ref from another bundle's copy
// of Vue would not be tracked by this copy's computed).
export { nextTick, ref } from 'vue';
export { stableStringify, useSavedSnapshot } from '../src/composables/useSavedSnapshot';
