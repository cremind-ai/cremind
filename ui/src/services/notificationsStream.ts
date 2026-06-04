/**
 * Subscribes to live event-driven notifications for the caller's profile.
 *
 * Thin shim over {@link profileEventsStream} — the underlying SSE is the
 * multiplexed `/api/profile-events/stream` connection that also carries
 * conversations-list snapshots, so the chat tab only holds one slot for
 * both. The `since` cursor is used for the initial replay; subsequent
 * reconnects are handled by the shared connection.
 */

import type { EventNotificationEntry } from './skillEventsApi';
import { subscribeNotifications, type ProfileEventsSubHandle } from './profileEventsStream';

export type NotificationStreamHandle = ProfileEventsSubHandle;

export function openNotificationsStream(
  agentUrl: string,
  authToken: string,
  sinceMs: number,
  onNotification: (entry: EventNotificationEntry) => void,
  _onError?: (e: any) => void,
): NotificationStreamHandle {
  return subscribeNotifications(agentUrl, authToken, sinceMs, onNotification);
}
