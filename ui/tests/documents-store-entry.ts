// Bundle entry for tests/documents-store.test.mjs: the documents store together
// with the Pinia instance it was bundled with (a second copy of Pinia from
// another bundle would not be the "active" one the store looks up).
export { createPinia, setActivePinia } from 'pinia';
export { useDocumentsStore, POLL_INTERVAL_MS } from '../src/stores/documents';
export { useSettingsStore } from '../src/stores/settings';
