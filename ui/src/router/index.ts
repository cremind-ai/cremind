import { createRouter, createWebHashHistory, type RouteLocationNormalized } from 'vue-router';
import ChatView from '../views/ChatView.vue';
import { useSettingsStore } from '../stores/settings';
import { PROFILE_ROUTES } from './profileRoutes';

declare module 'vue-router' {
  interface RouteMeta {
    title?: string;
  }
}

const APP_NAME = __IS_ELECTRON__ ? 'Cremind App' : 'Cremind Web UI';

const routes = [
  // Root: profile selector (or redirect to setup if first time)
  {
    path: '/',
    name: 'home',
    component: () => import('../views/ProfileSelector.vue'),
  },
  // Setup routes
  {
    path: '/setup',
    name: 'setup',
    component: () => import('../views/SetupWizard.vue'),
    meta: { title: 'Setup' },
  },
  {
    path: '/setup/:profile',
    name: 'setup-profile',
    component: () => import('../views/SetupWizard.vue'),
    props: true,
    meta: { title: 'Setup' },
  },
  // Login route for a specific profile
  {
    path: '/login/:profile',
    name: 'login',
    component: () => import('../views/LoginPage.vue'),
    props: true,
    meta: { title: 'Login' },
  },
  // Profile-scoped routes
  {
    path: '/:profile',
    name: 'chat',
    component: ChatView,
    props: true,
    meta: { title: 'Chat' },
  },
  {
    path: '/:profile/c/:conversationId',
    name: 'conversation',
    component: ChatView,
    props: true,
    meta: { title: 'Chat' },
  },
  {
    path: '/:profile/settings',
    name: 'settings',
    component: () => import('../views/SettingsPage.vue'),
    props: true,
    meta: { title: 'Settings' },
  },
  {
    path: '/:profile/settings/llm',
    name: 'llm-settings',
    component: () => import('../views/LLMSettings.vue'),
    props: true,
    meta: { title: 'LLM Providers' },
  },
  {
    path: '/:profile/settings/tools-skills',
    name: 'tools-skills-settings',
    component: () => import('../views/AgentsToolsSettings.vue'),
    props: true,
    meta: { title: 'Tools & Skills' },
  },
  {
    path: '/:profile/settings/config',
    name: 'user-config-settings',
    component: () => import('../views/UserConfigSettings.vue'),
    props: true,
    meta: { title: 'Config' },
  },
  {
    path: '/:profile/settings/embedding',
    name: 'embedding-settings',
    component: () => import('../views/EmbeddingSettings.vue'),
    props: true,
    meta: { title: 'Vector Embedding' },
    // Vector Embedding is a server-wide configuration owned by the
    // admin profile. Backend already enforces this with require_admin,
    // but block the navigation up-front so non-admin users never see
    // a 403'd page.
    beforeEnter: (to: RouteLocationNormalized) => {
      const profile = to.params.profile as string | undefined;
      if (profile && profile !== 'admin') {
        return { path: `/${profile}/settings`, replace: true };
      }
      return true;
    },
  },
  {
    path: '/:profile/settings/backup',
    name: 'backup-settings',
    component: () => import('../views/BackupSettings.vue'),
    props: true,
    meta: { title: 'Backup & Restore' },
    // Backup & Restore is a server-wide, admin-owned operation (the backend
    // gates it with require_admin). Mirror the embedding-settings guard so
    // non-admin profiles never land on a 403'd page.
    beforeEnter: (to: RouteLocationNormalized) => {
      const profile = to.params.profile as string | undefined;
      if (profile && profile !== 'admin') {
        return { path: `/${profile}/settings`, replace: true };
      }
      return true;
    },
  },
  {
    path: '/:profile/settings/blueprints',
    name: 'blueprint-settings',
    component: () => import('../views/BlueprintSettings.vue'),
    props: true,
    meta: { title: 'Blueprints' },
  },
  {
    path: '/:profile/settings/blueprints/import',
    name: 'blueprint-import',
    component: () => import('../views/BlueprintImportWizard.vue'),
    props: true,
    meta: { title: 'Import Blueprint' },
  },
  {
    path: '/:profile/settings/profiles',
    name: 'profile-settings',
    component: () => import('../views/ProfileSettings.vue'),
    props: true,
    meta: { title: 'Profiles' },
  },
  {
    path: '/:profile/settings/channels',
    name: 'channels-settings',
    component: () => import('../views/ChannelsSettings.vue'),
    props: true,
    meta: { title: 'Channels' },
  },
  {
    path: '/:profile/updates',
    name: 'updates',
    component: () => import('../views/UpdatesSettings.vue'),
    props: true,
    meta: { title: 'Updates' },
  },
  {
    path: '/:profile/channels',
    name: 'channels-page',
    component: () => import('../views/ChannelsPage.vue'),
    props: true,
    meta: { title: 'Channels' },
  },
  {
    path: '/:profile/about',
    name: 'about',
    component: () => import('../views/AboutPage.vue'),
    props: true,
    meta: { title: 'About' },
  },
  // Usage & Cost — token-usage + estimated-pricing dashboard
  {
    path: '/:profile/usage',
    name: 'usage',
    component: () => import('../views/UsagePage.vue'),
    props: true,
    meta: { title: 'Usage & Cost' },
  },
  // Process Manager — long-running exec_shell processes
  {
    path: '/:profile/processes',
    name: 'process-list',
    component: () => import('../views/ProcessList.vue'),
    props: true,
    meta: { title: 'Process Manager' },
  },
  {
    path: '/:profile/processes/:pid',
    name: 'process-terminal',
    component: () => import('../views/ProcessTerminal.vue'),
    props: true,
    meta: { title: 'Process Terminal' },
  },
  // Skill Events — conversation-scoped event subscriptions
  {
    path: '/:profile/events',
    name: 'skill-events',
    component: () => import('../views/SkillEventsPage.vue'),
    props: true,
    meta: { title: 'Events' },
  },
  // Calendar & Schedule — system calendar + time-based schedule events
  {
    path: '/:profile/calendar',
    name: 'calendar-schedule',
    component: () => import('../views/CalendarSchedulePage.vue'),
    props: true,
    meta: { title: 'Calendar & Schedule' },
  },
  // Developer — debug tools (live log stream, etc.). Admin-only; mirrors
  // the embedding-settings guard pattern above so non-admin profiles
  // never see a 403'd page.
  {
    path: '/:profile/developer',
    name: 'developer',
    component: () => import('../views/DeveloperPage.vue'),
    props: true,
    meta: { title: 'Developer' },
    beforeEnter: (to: RouteLocationNormalized) => {
      const profile = to.params.profile as string | undefined;
      if (profile && profile !== 'admin') {
        return { path: `/${profile}`, replace: true };
      }
      return true;
    },
  },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

// First-run gate (Electron only). When the runtime config has no agent
// URL — typical for a fresh install of the Electron app — every route
// other than ``/setup`` redirects there. The Setup Wizard hosts the
// first-run installer phase inline (see SetupWizard.vue's
// ``includeInstallerSteps``) so the user moves through Welcome →
// Deployment → Mode → Install → Server → … in a single continuous
// flow. We deliberately don't apply this in the web build (no
// ``window.cremind``) because there the agent URL comes from
// VITE_AGENT_URL at build time.
//
// ``bridge.config.agentUrl`` is the snapshot the Electron preload
// captured at startup; it never refreshes mid-session even after
// ``setAgentUrl`` writes the new value through to disk. To avoid
// bouncing post-setup navigation back to /setup when the bridge
// snapshot is stale, consult the live Pinia ref as a fallback — it's
// the same source ``a2aClient.getBaseUrl`` uses.
router.beforeEach((to) => {
  const bridge = window.cremind
  if (!bridge) return true
  if (bridge.config.agentUrl) return true
  if (useSettingsStore().agentUrl) return true
  if (to.name === 'setup' || to.name === 'setup-profile') return true
  return { path: '/setup', replace: true }
});

// Multi-window hint. Tray / jumplist / dock entries open new windows
// loading ``#/?cremind_window=<target>`` (main or settings). Resolve the
// hint to a profile-scoped route on the first navigation so the user
// lands on chat or settings directly. ProfileSelector preserves the
// hint when no profile is logged in yet (see selectProfile there).
router.beforeEach((to) => {
  const hint = to.query.cremind_window
  if (typeof hint !== 'string') return true
  const settingsStore = useSettingsStore()
  const profiles = settingsStore.getLoggedInProfiles()
  if (profiles.length === 0) return true
  const profile = profiles[0]
  const { cremind_window: _consumed, ...rest } = to.query
  const destByHint: Record<string, string> = {
    settings: `/${profile}/settings`,
    processes: `/${profile}/processes`,
    events: `/${profile}/events`,
    channels: `/${profile}/channels`,
  }
  const dest = destByHint[hint] ?? `/${profile}`
  return { path: dest, query: rest, replace: true }
});

// Synchronously activate the per-profile auth token before any view renders.
// Without this, child views' onMounted fires API calls before App.vue's
// onMounted has a chance to load the token (parent onMounted runs after
// children in Vue 3), producing 401s on hard reloads of profile-scoped pages.
//
// Just-updated grace window: the Electron main process and the Web-UI
// version poll both stamp ``sessionStorage('cremind:just_updated')``
// with the time of an automatic post-upgrade reload. For
// JUST_UPDATED_GRACE_MS afterwards we allow profile-scoped navigations
// even if ``getTokenForProfile`` momentarily returns empty — the
// downstream views will refetch their state and the token will be
// back in localStorage by the time they make API calls. Without this,
// any rare race that empties localStorage during the reload sequence
// would dump the user onto the Login screen they explicitly didn't
// want to see after an upgrade.
const JUST_UPDATED_GRACE_MS = 30000;

function withinJustUpdatedGrace(): boolean {
  try {
    const raw = sessionStorage.getItem('cremind:just_updated');
    if (!raw) return false;
    const ts = Number.parseInt(raw, 10);
    if (!Number.isFinite(ts)) return false;
    if (Date.now() - ts < JUST_UPDATED_GRACE_MS) return true;
    // Expired — clear so the redirect rule re-arms next time.
    sessionStorage.removeItem('cremind:just_updated');
    return false;
  } catch {
    return false;
  }
}

router.beforeEach((to) => {
  const routeName = typeof to.name === 'string' ? to.name : '';
  const profile = (to.params.profile as string | undefined) || '';
  if (!routeName || !PROFILE_ROUTES.has(routeName) || !profile) return true;

  const settingsStore = useSettingsStore();
  const token = settingsStore.getTokenForProfile(profile);
  if (!token) {
    if (withinJustUpdatedGrace()) {
      return true;
    }
    return { path: `/login/${profile}`, replace: true };
  }
  settingsStore.activateProfile(profile);
  return true;
});

// Keep document.title in sync with the active route. Runs only after
// every beforeEach guard has accepted the navigation, so we never set
// a title for a route the user was redirected away from. Routes
// without ``meta.title`` (currently only ``home``) show just the app
// name.
router.afterEach((to) => {
  const pageTitle = to.meta.title;
  document.title = pageTitle ? `${pageTitle} — ${APP_NAME}` : APP_NAME;
});

export default router;
