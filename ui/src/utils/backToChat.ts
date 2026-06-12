// Shared "Back to Chat" navigation for the sub-pages reachable from the chat
// sidebar (Settings, Developer, Channels, Processes, About, Skill Events,
// Updates). Each of those pages used to push the bare ``/:profile`` landing on
// back, which discarded the conversation the user had open and dumped them on
// the empty new-chat slot. Routing through the chat store's active conversation
// id instead restores the conversation they were viewing before they opened the
// sub-page; with no active conversation we fall back to the bare landing.

import type { Router } from 'vue-router';
import { useChatStore } from '../stores/chat';

export function goBackToChat(router: Router, profile: string): void {
  const conversationId = useChatStore().activeConversationId;
  router.push(
    conversationId
      ? { name: 'conversation', params: { profile, conversationId } }
      : { name: 'chat', params: { profile } },
  );
}
