// Test entry for tests/tags-setup-store.test.mjs: the simple-setup store and
// the pure flow helpers the dialogs drive it with, together with the Pinia
// they run on (harness.load() bundles a fresh copy per call).
export { createPinia, setActivePinia } from 'pinia';
export { FAST_POLL_MS, SLOW_POLL_MS, useTagsSetupStore } from '../../src/stores/tagsSetup';
export { useSettingsStore } from '../../src/stores/settings';
export {
  CONNECT_STAGES, candidateTitle, candidateView, connectStageIndex, discoveryDecision, enrollFailure, enrollStep,
  fontsUpdateNote, hostBlock, hostOpProgressLabel, hostStatePill, isWaitingForWake, operationProgressLabel,
  pendingSetups, plugInstruction, problemText, scanDecision, setupErrorMessage, tagParent, tagReach,
} from '../../src/utils/tagsSetupFormat';
