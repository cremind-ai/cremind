// Every route whose path starts with ``/:profile`` must be listed here. This set
// is what activates the per-profile auth token before the view renders (see the
// beforeEach guard in router/index.ts), redirects to login when the profile has
// no token, and shows the nav rail. A profile-scoped route left out of it still
// renders — but with an empty ``authToken`` on a hard reload or a pasted URL, so
// every API call the view makes short-circuits and the page reports an empty
// state. The Google Drive page shipped that way and said "Not linked" on every
// reload. router/index.ts logs a console error in dev if a route forgets.
export const PROFILE_ROUTES = new Set([
  'chat',
  'conversation',
  'group-chat',
  'group-chat-room',
  'group-chat-settings',
  'settings',
  'llm-settings',
  'tools-skills-settings',
  'user-config-settings',
  'embedding-settings',
  'gsuite-settings',
  'profile-settings',
  'channels-settings',
  'backup-settings',
  'blueprint-settings',
  'blueprint-import',
  'updates',
  'about',
  'channels-page',
  'process-list',
  'process-terminal',
  'skill-events',
  'calendar-schedule',
  'developer',
  'usage',
]);

// Routes that actually render chat (sidebar conversation list + per-conversation
// streams). Only these need the long-lived ``profile-events`` SSE that
// ``chatStore.connect()`` opens. Non-chat profile pages (Events, Settings,
// Processes, …) must NOT open it: each origin can hold only ~6 concurrent
// HTTP/1.1 connections, and the page-specific SSE streams those pages open
// (skill-events/file-watchers/settings/processes admin snapshots) already
// compete for that budget. Opening the chat stream on top of them saturated
// the pool and stalled later REST requests with "Provisional headers are
// shown" — see App.vue's handleProfileNavigation.
// The group-chat routes are deliberately absent: the room already holds one
// long-lived per-group SSE of its own, and adding the profile-events stream on
// top would spend a second slot of the same ~6-connection budget for a
// conversation list the page never renders.
export const CHAT_ROUTES = new Set(['chat', 'conversation']);
