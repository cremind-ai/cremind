// Test entry: the stores together with the Pinia they run on. harness.load()
// bundles a fresh copy per call, so a test must activate Pinia from the same
// copy as the stores it drives — hence one entry exporting both.
export { createPinia, setActivePinia } from 'pinia';
export { useChatStore } from '../../src/stores/chat';
export { useSettingsStore } from '../../src/stores/settings';
export { useCitationsStore } from '../../src/stores/citations';
