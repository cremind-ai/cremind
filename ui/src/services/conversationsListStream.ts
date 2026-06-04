/**
 * Subscribes to live conversation-list snapshots.
 *
 * Thin shim over {@link profileEventsStream} — the underlying SSE is the
 * multiplexed `/api/profile-events/stream` connection that also carries
 * skill-event notifications, so the chat tab only holds one slot for both.
 * Changing `channelType` re-establishes the multiplexed connection (the
 * backend's snapshot is server-filtered).
 */

import { subscribeConversationsList, type ConversationsListSnapshot, type ProfileEventsSubHandle } from './profileEventsStream';

export type ConversationsListStreamHandle = ProfileEventsSubHandle;

export type { ConversationsListSnapshot };

export function openConversationsListStream(
  agentUrl: string,
  authToken: string,
  _profileKey: string,
  channelType: string | null,
  onSnapshot: (snap: ConversationsListSnapshot) => void,
  _onError?: (e: any) => void,
): ConversationsListStreamHandle {
  return subscribeConversationsList(agentUrl, authToken, channelType, onSnapshot);
}
