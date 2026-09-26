// Test entry for tests/terminal-panel-cwd.test.mjs: the file panel's store (its
// per-profile working-directory seed, the reset that follows the signed-in
// profile, and the re-seed after the admin moves its own folder) and the files
// API client's error mapping — together with the Pinia, Vue and router they run
// on, since harness.load() bundles a fresh copy per call.
export { createPinia, setActivePinia } from 'pinia';
export { watch } from 'vue';
export { createMemoryHistory, createRouter } from 'vue-router';
export { useSettingsStore } from '../../src/stores/settings';
export { followSignedInProfile, useTerminalPanelStore } from '../../src/stores/terminalPanel';
export {
  DirectoryAccessError,
  FOREIGN_WORKSPACE_CODE,
  isForeignWorkspaceError,
  listDirectory,
  setConversationCwd,
} from '../../src/services/filesApi';
