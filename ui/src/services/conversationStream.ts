/**
 * Subscribes to live agent events for a conversation.
 *
 * Thin shim over {@link profileEventsStream}'s `subscribeConversation` —
 * per-conversation events now travel on the single multiplexed
 * `/api/profile-events/stream` connection alongside notifications and
 * conversations-list, so each tab holds one slot for all of them
 * regardless of how many conversations are being viewed across tabs.
 */

import { subscribeConversation, type ProfileEventsSubHandle } from './profileEventsStream';

export interface ConversationStreamEvent {
  seq?: number;
  type:
    | 'ready'
    | 'user_message'
    | 'event_trigger_message'
    | 'event_trigger_rejected'
    | 'thinking'
    | 'result'
    | 'text'
    | 'file'
    | 'terminal'
    | 'token_usage'
    | 'phase'
    | 'summary'
    | 'compaction_suggested'
    | 'compacted'
    | 'ask_user_question'
    | 'plan_ready'
    | 'plan_decision'
    | 'todos'
    | 'complete'
    | 'error'
    | 'cwd';
  data: any;
}

export type ConversationStreamHandle = ProfileEventsSubHandle;

export function openConversationStream(
  agentUrl: string,
  authToken: string,
  conversationId: string,
  onEvent: (e: ConversationStreamEvent) => void,
  _onError?: (e: any) => void,
): ConversationStreamHandle {
  return subscribeConversation(
    agentUrl,
    authToken,
    conversationId,
    (event) => onEvent(event as ConversationStreamEvent),
  );
}
