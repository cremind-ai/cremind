// Test entry for tests/indexed-content.test.mjs: the file tree's index status
// store together with the Pinia (and the settings and documents stores) it was
// bundled with — a second copy of Pinia from another bundle would not be the
// "active" one the stores look up.
export { createPinia, setActivePinia } from 'pinia';
export { nextTick } from 'vue';
export { useIndexStatusStore } from '../../src/stores/indexStatus';
export { useSettingsStore } from '../../src/stores/settings';
export { useDocumentsStore } from '../../src/stores/documents';
