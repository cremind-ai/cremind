/**
 * Unsaved changes on a settings page: telling them apart from the saved state,
 * and asking before a navigation throws them away.
 *
 * Every settings page saves from one place, SettingsSaveBar, which calls
 * useLeaveGuard itself — so a page with a bar is guarded, and a page decides
 * only what "dirty" means for it. Most pages keep a snapshot of what the
 * server last returned (useSavedSnapshot) and compare the live form with it,
 * so a field typed back to its old value no longer counts as a change.
 */
import { onBeforeUnmount, onMounted } from 'vue';
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router';
import { ElMessageBox } from 'element-plus';
import { isSessionExpiring } from '../services/sessionExpiry';

export { stableStringify, useSavedSnapshot, type SavedSnapshot } from './useSavedSnapshot';

const isElectron = typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__;

/**
 * While `dirty()` holds, a route change (Back to Settings, the rail, another
 * profile's copy of the page) asks Stay / Leave without saving, and closing or
 * reloading a browser tab gets the browser's own prompt. Not in the desktop
 * app: there a cancelled unload silently keeps the window from closing.
 *
 * An expired session is let through — its page can no longer save anything.
 */
export function useLeaveGuard(dirty: () => boolean): void {
  async function allowLeave(): Promise<boolean> {
    if (!dirty() || isSessionExpiring()) return true;
    try {
      await ElMessageBox.confirm(
        'You have unsaved changes on this page. Leave without saving them?',
        'Unsaved changes',
        { type: 'warning', confirmButtonText: 'Leave without saving', cancelButtonText: 'Stay' },
      );
      return true;
    } catch {
      return false;
    }
  }

  onBeforeRouteLeave(() => allowLeave());
  // The same page for other params (a query change, e.g. ?section=, is not
  // leaving it).
  onBeforeRouteUpdate((to, from) => (to.path === from.path ? true : allowLeave()));

  function onBeforeUnload(event: BeforeUnloadEvent) {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = '';
  }
  onMounted(() => {
    if (!isElectron) window.addEventListener('beforeunload', onBeforeUnload);
  });
  onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload));
}
