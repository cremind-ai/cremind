/**
 * Subscribes to live process-list snapshots for the caller's profile.
 *
 * Thin shim over {@link profileEventsStream} — the underlying SSE is the
 * multiplexed `/api/profile-events/stream` connection, so the Processes
 * page holds no dedicated socket. The backend pushes a fresh snapshot
 * (`event: processes`) on connect and whenever a process spawns, exits,
 * is stopped, or an autostart row mutates.
 *
 * The standalone `/api/processes/stream` endpoint still exists for the
 * CLI; the web UI no longer opens it directly.
 */

import type { ProcessRow } from './processApi';
import { subscribeProcesses, type ProfileEventsSubHandle } from './profileEventsStream';

export type ProcessStreamHandle = ProfileEventsSubHandle;

export function openProcessesStream(
  agentUrl: string,
  authToken: string,
  onSnapshot: (processes: ProcessRow[]) => void,
  _onError?: (e: any) => void,
): ProcessStreamHandle {
  return subscribeProcesses(agentUrl, authToken, onSnapshot);
}
