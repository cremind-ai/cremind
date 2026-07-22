// Declarative navigation model for the two-rail sidebar (NavRail.vue).
//
// Kept purely declarative — no store/router imports — so it stays trivially
// testable and free of circular-import hazards. Route names must match
// router/index.ts exactly; they are the contract the Electron multi-window
// ``?cremind_window=`` hints also depend on, so never rename one here without
// updating the router.
//
// Adding a new top-level destination is a one-line entry here. Give it
// ``placement: 'overflow'`` to fold it into the "More" popover instead of the
// rail — the rail middle is capped so a growing feature set can never shrink
// the conversation-history panel again (the original design flaw).

export type NavItemKind = 'route' | 'notifications';

export interface NavItem {
  /** Stable key for v-for + test hooks. */
  id: string;
  label: string;
  /** Iconify mdi:* name. */
  icon: string;
  kind: NavItemKind;
  /** vue-router name to push. Required when ``kind === 'route'``. */
  routeName?: string;
  /** Extra route names that should also light this icon as active
   *  (child routes, e.g. a detail page under a list page). */
  activeRouteNames?: string[];
  /** Admin-only entry point; hidden for non-admin profiles (the route guard
   *  is the real backstop, this just hides the affordance). */
  adminOnly?: boolean;
  /** ``rail`` (default) shows it as a rail icon; ``overflow`` folds it into
   *  the "More" popover. */
  placement?: 'rail' | 'overflow';
}

// Rail middle section — the primary destinations, top to bottom.
export const NAV_ITEMS: NavItem[] = [
  {
    id: 'chat',
    label: 'Chat',
    icon: 'mdi:message-text',
    kind: 'route',
    routeName: 'chat',
    activeRouteNames: ['conversation'],
  },
  {
    id: 'notifications',
    label: 'Notifications',
    icon: 'mdi:bell-outline',
    kind: 'notifications',
  },
  {
    id: 'events',
    label: 'Events',
    icon: 'mdi:lightning-bolt-outline',
    kind: 'route',
    routeName: 'skill-events',
  },
  {
    id: 'calendar',
    label: 'Calendar & Schedule',
    icon: 'mdi:calendar-month',
    kind: 'route',
    routeName: 'calendar-schedule',
  },
  {
    id: 'channels',
    label: 'Channels',
    icon: 'mdi:link-variant',
    kind: 'route',
    routeName: 'channels-page',
  },
  {
    id: 'processes',
    label: 'Process Manager',
    icon: 'mdi:console',
    kind: 'route',
    routeName: 'process-list',
    activeRouteNames: ['process-terminal'],
  },
  {
    id: 'usage',
    label: 'Usage & Cost',
    icon: 'mdi:chart-box-outline',
    kind: 'route',
    routeName: 'usage',
  },
  {
    id: 'developer',
    label: 'Developer',
    icon: 'mdi:bug-outline',
    kind: 'route',
    routeName: 'developer',
    adminOnly: true,
  },
];

// Bottom pinned Settings item. Its ``activeRouteNames`` cover every settings
// child page so the Settings icon stays lit anywhere under /settings.
export const SETTINGS_ITEM: NavItem = {
  id: 'settings',
  label: 'Settings',
  icon: 'mdi:cog',
  kind: 'route',
  routeName: 'settings',
  activeRouteNames: [
    'llm-settings',
    'tools-skills-settings',
    'user-config-settings',
    'embedding-settings',
    'backup-settings',
    'blueprint-settings',
    'blueprint-import',
    'profile-settings',
    'channels-settings',
  ],
};
