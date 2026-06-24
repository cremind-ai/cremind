export const PROFILE_ROUTES = new Set([
  'chat',
  'conversation',
  'settings',
  'llm-settings',
  'tools-skills-settings',
  'user-config-settings',
  'embedding-settings',
  'profile-settings',
  'channels-settings',
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
export const CHAT_ROUTES = new Set(['chat', 'conversation']);
