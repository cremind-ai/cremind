// Test entry for tests/my-documents-access.test.mjs: the embedding status
// store (whose `known` / `whenKnown` gate My Documents) and the pure rules that
// read it — together with the Pinia they run on, since harness.load() bundles
// a fresh copy per call.
export { createPinia, setActivePinia } from 'pinia';
export { useEmbeddingStatusStore, EMBEDDING_STATE_WAIT_MS } from '../../src/stores/embeddingStatus';
export { useSettingsStore } from '../../src/stores/settings';
export {
  documentsChipVisible,
  myDocumentsClosedMessage,
  myDocumentsOpen,
  myDocumentsRouteDecision,
  settingsListPath,
  visibleSettingsCards,
} from '../../src/utils/myDocumentsAccess';
