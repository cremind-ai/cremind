/**
 * Subscribes to the live Settings-page state stream.
 *
 * Thin shim over {@link profileEventsStream} — the underlying SSE is the
 * multiplexed `/api/profile-events/stream` connection, so the Settings
 * page holds no dedicated socket. The backend emits a wakeup ping
 * (`event: settings-state`) whenever any resource rendered on the
 * Settings → Tools & Skills page or the Agents drawer changes — tool
 * config, agent register/enable/auth, LLM provider config, setup
 * completion, skill mode, skill add/remove on disk. The ping carries no
 * payload; consumers refetch the resources they render.
 *
 * The standalone `/api/settings/state/stream` endpoint still exists for
 * the CLI; the web UI no longer opens it directly.
 */

import { subscribeSettingsState, type ProfileEventsSubHandle } from './profileEventsStream';

export type SettingsStateStreamHandle = ProfileEventsSubHandle;

export interface SettingsStateChange {
  /** Emitted once per server-side state change. No payload. */
  ts: number;
}

export function openSettingsStateStream(
  agentUrl: string,
  authToken: string,
  _profileKey: string,
  onChange: (e: SettingsStateChange) => void,
  _onError?: (e: any) => void,
): SettingsStateStreamHandle {
  return subscribeSettingsState(agentUrl, authToken, onChange);
}
