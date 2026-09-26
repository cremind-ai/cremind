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
    // A turn starting on a user message that was persisted and streamed before
    // the run began (a mid-turn park that lost the race to the turn's end), so
    // there is no `user_message` frame to mark the start.
    | 'run_started'
    // A message stored in a platform group that the agent chose not to answer.
    // Deliberately NOT `user_message`: no run is starting, and no terminal
    // frame will follow, so anything that reads a user message as "a turn has
    // begun" would wait for a completion that never comes.
    | 'quiet_user_message'
    // A message sent mid-turn was folded into the running turn: end the current
    // assistant bubble, the reply that follows opens a new one.
    | 'flow_break'
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
    | 'agent_activity'
    // A Documentation Search research job's live snapshot (the Research activity
    // panel): full-snapshot semantics like `agent_activity`, published from
    // the job's own task, so frames keep coming after the turn has ended.
    | 'research_activity'
    // The answer's Documentation Search citations, checked against what the tools
    // issued: `{citations: {v, items, unverified}, assistant_id}`. Published
    // when the answer is saved, so it attaches to the finishing bubble and
    // never opens one. The multiplexer forwards conversation-event frames of
    // any type, so this needed no change in profileEventsStream.ts.
    | 'citations'
    // The conversation's search-tool selection was saved: `{version}`. Transient
    // (never replayed); the chat store re-reads the state when the version is
    // one it does not hold.
    | 'search_tools'
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
