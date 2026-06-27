import { defineStore } from 'pinia';
import { ElNotification } from 'element-plus';
import { h } from 'vue';
import router from '../router';
import { getA2AClient, getApiOrigin } from '../services/a2aClient';
import { useSettingsStore } from './settings';
import { useNotificationsStore } from './notifications';
import { useTerminalPanelStore } from './terminalPanel';
import {
  fetchConversations as apiFetchConversations,
  fetchConversationMessages,
  deleteConversation as apiDeleteConversation,
  deleteAllConversations as apiDeleteAllConversations,
  createConversation as apiCreateConversation,
  sendMessageRequest,
  cancelTask,
  updateConversationTitle as apiUpdateConversationTitle,
  updateConversationId as apiUpdateConversationId,
} from '../services/conversationApi';
import type { MessageRecord } from '../services/conversationApi';
import {
  openConversationStream,
  type ConversationStreamEvent,
  type ConversationStreamHandle,
} from '../services/conversationStream';
import {
  openConversationsListStream,
  type ConversationsListStreamHandle,
} from '../services/conversationsListStream';
import type { AgentCard, Part, TaskState } from '@a2a-js/sdk';

// Non-reactive per-conversation state for the live SSE subscription. Kept
// outside the Pinia store so the SSE handle (an unserializable AbortController
// wrapper) and per-stream scratch state aren't trapped in Vue's reactivity.
//
// The lifecycle is REFERENCE-COUNTED: a conversation's stream stays open as
// long as at least one "tracker" wants it. Trackers include the active view
// ('active'), the streaming runtime ('streaming'), and any explicit holds
// ('manual'). Switching to Settings or another conversation drops the
// 'active' tracker but leaves 'streaming' in place, so background streams
// keep flowing into the bucket and the user sees them when they come back.
interface StreamScratch {
  handle: ConversationStreamHandle;
  seqSeen: Set<number>;
  currentTextPart: string;
  // Active tracker reasons. The handle is closed only when this set empties.
  trackers: Set<TrackReason>;
}
const streamScratch = new Map<string, StreamScratch>();

// Live conversations-list SSE handle. The backend pushes a fresh snapshot
// whenever a conversation is created/updated/deleted/renamed or has a new
// message. Held outside the Pinia store so the AbortController-based handle
// isn't trapped in Vue's reactivity.
let conversationsListHandle: ConversationsListStreamHandle | null = null;

type TrackReason = 'active' | 'streaming' | 'manual';

// Sentinel key for the empty "new chat" landing slot before a real id is allocated.
export const NEW_CHAT_KEY = '__new__';

// Conversation history types (now backed by backend API)
export interface SavedConversation {
  id: string;
  title: string;
  channelId: string | null;
  contextId: string | undefined;
  taskId: string | undefined;
  createdAt: number;
  updatedAt: number;
  messageCount: number;
}

// UI-specific types
export interface FileAttachment {
  name: string;
  mimeType: string;
  uri: string;
}

export interface TerminalAttachment {
  processId: string;
  command: string;
  commandShort: string;
  workingDirectory?: string;
  pty?: boolean;
}

export interface ChatMessage {
  id: string;
  // Persisted (server) id of the assistant turn, captured on `complete` without
  // overwriting the optimistic `id` (changing `id` would remount the bubble and
  // flicker). Usage records are keyed by this server id, so the usage chip
  // matches on `backendId ?? id` to show cost live, not only after a reload.
  backendId?: string;
  role: 'user' | 'assistant';
  content: string;
  parts: Part[];
  timestamp: Date;
  state?: TaskState;
  isStreaming?: boolean;
  thinkingSteps?: ThinkingStep[];
  artifacts?: ArtifactInfo[];
  fileAttachments?: FileAttachment[];
  terminalAttachments?: TerminalAttachment[];
  tokenUsage?: TokenUsage;
  latency?: LatencyInfo;
  reasoningModelLabel?: string;
  summary?: string;
}

export interface ObservationPart {
  kind: 'text' | 'file' | 'data';
  text?: string;
  data?: Record<string, any>;
  file?: { name?: string; mime_type?: string; uri?: string; bytes?: string };
}

export interface ThinkingStep {
  // One tool call within a step. ``step`` groups parallel tool calls made in the
  // same model turn; ``callId`` pairs a tool call with its result.
  step?: number | null;
  callId?: string | null;
  tool: string;
  toolInput: string;
  result?: ObservationPart[];
  receivedAt?: number;
  modelLabel?: string | null;
}

export interface ArtifactInfo {
  name: string;
  artifactId: string;
  type: string;
  data: any;
}

export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  totalTokens: number;
}

export interface LatencyInfo {
  requestSentAt: number;
  firstEventAt?: number;
  firstStepAt?: number;
  firstTokenAt?: number;
  completedAt?: number;
}

export interface ConversationRuntime {
  isStreaming: boolean;
  isSummarizing: boolean;
  currentTaskId: string | undefined;
  currentContextId: string | undefined;
  startedAt: number;
  // ID of the assistant ChatMessage currently being filled in by the
  // SSE stream. `undefined` between runs.
  streamingAssistantId?: string;
}

export interface CompactionSuggestion {
  currentTokens: number;
  threshold: number;
  estimatedSavings: number;
  contextWindow?: number;
}

interface ChatState {
  messagesByConversation: Record<string, ChatMessage[]>;
  runtimes: Record<string, ConversationRuntime>;
  /** Pending "compaction suggested" popup per conversation (null when none). */
  compactionByConversation: Record<string, CompactionSuggestion | null>;
  agentCard: AgentCard | null;
  agentName: string;
  isConnected: boolean;
  error: string | null;
  conversations: SavedConversation[];
  activeConversationId: string | null;
  /** Conversation list filter — channel_type ('main', 'telegram', etc.). */
  channelFilter: string;
  /** Per-conversation channel id cache.
   *
   * Populated when a conversation is loaded (list/switch/create) and read
   * by the read-only banner / MessageInput gate. **Independent of the
   * filtered ``conversations`` list** — switching the sidebar filter
   * MUST NOT cause this map to forget about the active conversation, or
   * the read-only check would silently flip to "writable". */
  channelIdsByConversation: Record<string, string | null>;
}

function makeRuntime(): ConversationRuntime {
  return {
    isStreaming: false,
    isSummarizing: false,
    currentTaskId: undefined,
    currentContextId: undefined,
    startedAt: Date.now(),
  };
}

export const useChatStore = defineStore('chat', {
  state: (): ChatState => ({
    messagesByConversation: {},
    runtimes: {},
    compactionByConversation: {},
    agentCard: null,
    agentName: 'Agent',
    isConnected: false,
    error: null,
    conversations: [],
    activeConversationId: null,
    channelFilter: 'main',
    channelIdsByConversation: {},
  }),

  getters: {
    activeKey(state): string {
      return state.activeConversationId ?? NEW_CHAT_KEY;
    },
    messages(state): ChatMessage[] {
      const key = state.activeConversationId ?? NEW_CHAT_KEY;
      return state.messagesByConversation[key] ?? [];
    },
    activeRuntime(state): ConversationRuntime | undefined {
      const key = state.activeConversationId ?? NEW_CHAT_KEY;
      return state.runtimes[key];
    },
    isStreaming(state): boolean {
      const key = state.activeConversationId ?? NEW_CHAT_KEY;
      return !!state.runtimes[key]?.isStreaming;
    },
    /** Channel id of the active conversation, ``null`` for the empty
     *  "new chat" slot or when not yet loaded. Read by the read-only
     *  banner / MessageInput gate; stable across sidebar filter changes. */
    activeChannelId(state): string | null {
      if (!state.activeConversationId) return null;
      return state.channelIdsByConversation[state.activeConversationId] ?? null;
    },
    isSummarizing(state): boolean {
      const key = state.activeConversationId ?? NEW_CHAT_KEY;
      return !!state.runtimes[key]?.isSummarizing;
    },
    /** Pending compaction suggestion for the active conversation, or null. */
    compactionSuggestion(state): CompactionSuggestion | null {
      if (!state.activeConversationId) return null;
      return state.compactionByConversation[state.activeConversationId] ?? null;
    },
    /**
     * Map of conversationId -> isStreaming, used by the sidebar to show
     * a "live" indicator on background-running conversations.
     */
    streamingConversationIds(state): Set<string> {
      const ids = new Set<string>();
      for (const [id, rt] of Object.entries(state.runtimes)) {
        if (rt.isStreaming) ids.add(id);
      }
      return ids;
    },
  },

  actions: {
    /**
     * Fetch agent card to get agent info and verify connectivity.
     * The agent card is the only thing we still get via the A2A SDK
     * (purely for backwards compat; could be replaced with a plain GET).
     */
    async fetchAgentCard() {
      try {
        this.error = null;
        const client = await getA2AClient();
        this.agentCard = await client.getAgentCard();
        this.agentName = this.agentCard.name || 'Agent';
        this.isConnected = true;
        console.log('✓ Connected to agent:', this.agentName);
      } catch (error: any) {
        this.isConnected = false;
        this.error = error.message || 'Failed to connect to agent';
        console.error('Failed to fetch agent card:', error);
        throw error;
      }
    },

    /**
     * Ensure a bucket and runtime exist for the given conversation key.
     */
    ensureBucket(key: string): { bucket: ChatMessage[]; runtime: ConversationRuntime } {
      if (!this.messagesByConversation[key]) {
        this.messagesByConversation[key] = [];
      }
      if (!this.runtimes[key]) {
        this.runtimes[key] = makeRuntime();
      }
      return {
        bucket: this.messagesByConversation[key],
        runtime: this.runtimes[key],
      };
    },

    /**
     * Send a message to the agent: creates a conversation if we're in the
     * "new chat" landing slot, POSTs the message (server enqueues a fire-
     * and-forget agent run), and ensures an SSE subscription is open. The
     * actual streaming happens through the SSE handler below — this method
     * returns once the POST succeeds, NOT when the run finishes.
     */
    /**
     * Ensure there is an active conversation, creating one upfront if needed,
     * and return its id (or null on failure). Used by the composer so files
     * can be uploaded to a real conversation temp dir before the first send.
     */
    async ensureConversation(): Promise<string | null> {
      if (this.activeConversationId) return this.activeConversationId;
      const settings = useSettingsStore();
      const agentUrl = settings.agentUrl;
      const authToken = settings.authToken;
      try {
        const conv = await apiCreateConversation(agentUrl, authToken);
        this.conversations = [
          {
            id: conv.id,
            title: conv.title,
            channelId: conv.channel_id ?? null,
            contextId: conv.context_id ?? undefined,
            taskId: conv.task_id ?? undefined,
            createdAt: conv.created_at,
            updatedAt: conv.updated_at,
            messageCount: 0,
          },
          ...this.conversations,
        ];
        this.channelIdsByConversation[conv.id] = conv.channel_id ?? null;
        this.activeConversationId = conv.id;
        this.trackConversation(conv.id, 'active');
        return conv.id;
      } catch (e: any) {
        this.error = e?.message || 'Failed to create conversation';
        console.error('Failed to create conversation:', e);
        return null;
      }
    },

    async sendMessage(
      text: string,
      options?: {
        reasoning?: boolean;
        conversationId?: string;
        attachments?: { name: string; path: string }[];
      },
    ) {
      if (!text.trim()) return;

      this.error = null;

      const settings = useSettingsStore();
      const agentUrl = settings.agentUrl;
      const authToken = settings.authToken;
      const profile = settings.profileId;
      if (!profile) {
        this.error = 'No active profile';
        return;
      }

      // Resolve target conversation. Three cases:
      //   1. caller specified one → use it
      //   2. there's an active conversation → use it
      //   3. neither → create a new one upfront so the POST has a real id
      let cid = options?.conversationId ?? this.activeConversationId ?? null;
      let createdNew = false;
      if (!cid) {
        try {
          const conv = await apiCreateConversation(agentUrl, authToken);
          cid = conv.id;
          createdNew = true;
          // Inject into the saved-conversations list so the sidebar shows it
          // immediately, even before the first event arrives.
          this.conversations = [
            {
              id: conv.id,
              title: conv.title,
              channelId: conv.channel_id ?? null,
              contextId: conv.context_id ?? undefined,
              taskId: conv.task_id ?? undefined,
              createdAt: conv.created_at,
              updatedAt: conv.updated_at,
              messageCount: 0,
            },
            ...this.conversations,
          ];
          this.channelIdsByConversation[conv.id] = conv.channel_id ?? null;
        } catch (e: any) {
          this.error = e?.message || 'Failed to create conversation';
          console.error('Failed to create conversation:', e);
          return;
        }
      }

      const { bucket, runtime } = this.ensureBucket(cid);

      // Optimistically render the user message so the input clears at once
      // and the user sees instant feedback. The server will also emit a
      // `user_message` SSE event with the persisted id; the handler dedupes.
      const optimisticUserId = this.generateId();
      const userMessage: ChatMessage = {
        id: optimisticUserId,
        role: 'user',
        content: text,
        parts: [{ kind: 'text', text } as Part],
        timestamp: new Date(),
      };
      bucket.push(userMessage);

      // Flip to streaming optimistically so the sidebar/input UI updates
      // before the first SSE event lands. The 'streaming' tracker is what
      // keeps the SSE alive across navigation.
      runtime.isStreaming = true;
      runtime.isSummarizing = false;
      runtime.startedAt = Date.now();

      // Make sure we're subscribed to events for this conversation BEFORE
      // the POST returns; otherwise we could miss the first few events
      // before the SSE handshake completes. The bus's start_run + ring
      // buffer make this race-tolerant, but opening early avoids the
      // replay-snapshot detour.
      this.trackConversation(cid, 'streaming');

      // If we created a new conversation, also surface it as the active one
      // and update the URL via the watcher in ChatView.vue. Add the
      // 'active' tracker here too — the URL-watcher → switchConversation
      // round-trip won't because it early-returns when activeConversationId
      // already matches, leaving the stream held only by 'streaming'.
      if (createdNew) {
        this.activeConversationId = cid;
        this.trackConversation(cid, 'active');
      }

      try {
        const resp = await sendMessageRequest(
          agentUrl,
          authToken,
          cid,
          text,
          options?.reasoning !== false,
          options?.attachments,
        );
        runtime.currentTaskId = resp.run_id;
      } catch (e: any) {
        this.error = e?.message || 'Failed to send message';
        runtime.isStreaming = false;
        this.untrackConversation(cid, 'streaming');
        // Surface the failure inside the bucket so the user knows what
        // happened without inspecting devtools.
        bucket.push({
          id: this.generateId(),
          role: 'assistant',
          content: `Error: ${this.error}`,
          parts: [],
          timestamp: new Date(),
          isStreaming: false,
        });
        console.error('sendMessage POST failed:', e);
      }
    },

    /**
     * Stop the in-flight conversation. Tells the backend to cancel the
     * running task; flips the UI optimistically so the button morphs back
     * instantly. Defaults to the active conversation; pass `conversationId`
     * to stop a specific background stream.
     */
    async stopMessage(conversationId?: string) {
      const cid = conversationId ?? this.activeConversationId ?? NEW_CHAT_KEY;
      const runtime = this.runtimes[cid];
      const bucket = this.messagesByConversation[cid];
      if (!runtime) return;

      const taskId = runtime.currentTaskId;
      runtime.isStreaming = false;
      runtime.isSummarizing = false;
      const last = bucket?.[bucket.length - 1];
      if (last?.role === 'assistant') {
        last.isStreaming = false;
      }
      // Drop the 'streaming' tracker so the SSE can close (it will close
      // unless 'active' or 'manual' trackers are still holding it).
      this.untrackConversation(cid, 'streaming');

      if (!taskId) return;

      try {
        await cancelTask(useSettingsStore().agentUrl, useSettingsStore().authToken, taskId);
      } catch (e) {
        // Fall back to plain fetch with token (cancelTask already uses fetch
        // but throws on non-2xx; swallow to preserve old optimistic-cancel UX)
        console.warn('Cancel request failed', e);
        try {
          const token = useSettingsStore().authToken;
          await fetch(`${getApiOrigin()}/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
            method: 'POST',
            headers: token ? { 'Authorization': `Bearer ${token}` } : {},
          });
        } catch {
          // already logged
        }
      }
    },

    // ── compaction suggestion popup ───────────────────────────────────

    /** Dismiss the popup; it re-appears after the next over-threshold turn. */
    dismissCompaction(conversationId: string) {
      this.compactionByConversation[conversationId] = null;
    },

    /** Run model-driven compaction now, then clear the suggestion. */
    async compactConversation(conversationId: string) {
      this.compactionByConversation[conversationId] = null;
      try {
        const settings = useSettingsStore();
        const { triggerConversationMemory } = await import('../services/conversationMemory');
        await triggerConversationMemory(
          settings.agentUrl, settings.authToken, conversationId,
        );
      } catch (e) {
        console.warn('Compaction failed:', e);
      }
    },

    // ── stream lifecycle (ref-counted) ────────────────────────────────

    /**
     * Add a tracker for the conversation's SSE subscription, opening it
     * if this is the first tracker. Called whenever a reason arises to
     * keep the stream alive: viewing the conversation ('active'), an
     * in-flight run ('streaming'), or an explicit hold ('manual').
     */
    trackConversation(conversationId: string, reason: TrackReason) {
      let scratch = streamScratch.get(conversationId);
      if (!scratch) {
        const settings = useSettingsStore();
        const agentUrl = settings.agentUrl;
        const authToken = settings.authToken;
        if (!authToken) {
          console.log('[debug:track] aborting open: no authToken', { conversationId, reason });
          return;
        }
        console.log('[debug:track] opening NEW SSE', { conversationId, reason });
        const trackers = new Set<TrackReason>();
        trackers.add(reason);
        const init: StreamScratch = {
          handle: { close: () => {} },
          seqSeen: new Set<number>(),
          currentTextPart: '',
          trackers,
        };
        streamScratch.set(conversationId, init);
        const handle = openConversationStream(
          agentUrl,
          authToken,
          conversationId,
          (event) => this.handleConversationStreamEvent(conversationId, event),
          (err) => console.warn(`[chat] SSE error for ${conversationId}:`, err),
        );
        init.handle = handle;
        return;
      }
      console.log('[debug:track] reusing SSE, adding tracker', {
        conversationId,
        reason,
        existingTrackers: Array.from(scratch.trackers),
      });
      scratch.trackers.add(reason);
    },

    /**
     * Drop a tracker; close the SSE subscription only when the last
     * tracker is gone. Safe to call repeatedly.
     */
    untrackConversation(conversationId: string, reason: TrackReason) {
      const scratch = streamScratch.get(conversationId);
      if (!scratch) return;
      scratch.trackers.delete(reason);
      if (scratch.trackers.size === 0) {
        try { scratch.handle.close(); } catch { /* ignore */ }
        streamScratch.delete(conversationId);
      }
    },

    /**
     * Force-close the SSE subscription regardless of trackers. Used on
     * disconnect / profile switch / conversation delete.
     */
    forceCloseStream(conversationId: string) {
      const scratch = streamScratch.get(conversationId);
      if (!scratch) return;
      try { scratch.handle.close(); } catch { /* ignore */ }
      streamScratch.delete(conversationId);
    },

    // ── connection management ─────────────────────────────────────────

    async connect() {
      await this.fetchAgentCard();
      await this.loadConversations();
      this.subscribeConversationsList();
    },

    disconnect() {
      this.unsubscribeConversationsList();
      for (const id of Array.from(streamScratch.keys())) {
        this.forceCloseStream(id);
      }
      this.isConnected = false;
      this.agentCard = null;
      this.agentName = 'Agent';
      this.error = null;
      console.log('Disconnected from agent');
    },

    /**
     * Open the per-profile conversations-list SSE stream. The backend pushes
     * a fresh snapshot on every mutation; the handler below replaces the
     * in-memory ``conversations`` array on each frame.
     *
     * The active ``channelFilter`` is passed as a query param so the stream
     * matches what ``loadConversations`` would have fetched. Re-opens the
     * stream when called with a different filter than the active one.
     */
    subscribeConversationsList() {
      const settingsStore = useSettingsStore();
      const profile = settingsStore.profileId;
      if (!profile || !settingsStore.authToken) return;
      this.unsubscribeConversationsList();
      const apiChannelType =
        this.channelFilter === 'all' ? null : (this.channelFilter || null);
      conversationsListHandle = openConversationsListStream(
        settingsStore.agentUrl,
        settingsStore.authToken,
        profile,
        apiChannelType,
        (snap) => {
          this.conversations = snap.conversations.map(c => ({
            id: c.id,
            title: c.title,
            channelId: c.channel_id,
            contextId: c.context_id ?? undefined,
            taskId: c.task_id ?? undefined,
            createdAt: c.created_at,
            updatedAt: c.updated_at,
            messageCount: c.message_count,
          }));
          for (const c of snap.conversations) {
            this.channelIdsByConversation[c.id] = c.channel_id ?? null;
          }
        },
      );
    },

    unsubscribeConversationsList() {
      if (conversationsListHandle === null) return;
      try { conversationsListHandle.close(); } catch { /* ignore */ }
      conversationsListHandle = null;
    },

    /**
     * Reset all chat state for a profile switch, then reconnect.
     * Cancels every in-flight stream first.
     */
    async resetForProfileSwitch() {
      this.unsubscribeConversationsList();
      for (const id of Object.keys(this.runtimes)) {
        if (this.runtimes[id].isStreaming) {
          await this.stopMessage(id);
        }
      }
      for (const id of Array.from(streamScratch.keys())) {
        this.forceCloseStream(id);
      }
      this.messagesByConversation = {};
      this.runtimes = {};
      this.activeConversationId = null;
      this.conversations = [];
      this.channelIdsByConversation = {};
      this.error = null;

      if (this.isConnected) {
        this.disconnect();
      }

      try {
        await this.connect();
      } catch (e) {
        console.error('Failed to reconnect after profile switch:', e);
      }
    },

    /**
     * Load conversations from backend API.
     *
     * Optionally filtered by channel (Sidebar drives this through the
     * channels Pinia store); ``main`` and missing both fall back to the
     * built-in web/CLI channel.
     */
    async loadConversations(channelType?: string | null) {
      if (channelType === undefined) channelType = this.channelFilter;
      try {
        const settingsStore = useSettingsStore();
        const agentUrl = settingsStore.agentUrl;
        const authToken = settingsStore.authToken;
        const profile = settingsStore.profileId;
        if (!profile) return;

        // ``all`` is a virtual filter — pass null so the backend returns
        // conversations across every channel, sorted by recency.
        const apiChannelType = channelType === 'all' ? null : channelType;
        const conversations = await apiFetchConversations(
          agentUrl, authToken, 50, 0, apiChannelType,
        );
        this.conversations = conversations.map(c => ({
          id: c.id,
          title: c.title,
          channelId: c.channel_id,
          contextId: c.context_id ?? undefined,
          taskId: c.task_id ?? undefined,
          createdAt: c.created_at,
          updatedAt: c.updated_at,
          messageCount: c.message_count,
        }));
        // Persist the channel id for every conversation we observed so the
        // active-conversation read-only check survives a filter switch
        // dropping the row from ``this.conversations``.
        for (const c of conversations) {
          this.channelIdsByConversation[c.id] = c.channel_id ?? null;
        }
      } catch (e) {
        console.error('Failed to load conversations:', e);
      }
    },

    /**
     * Switch the sidebar's conversation filter. Re-opens the
     * conversations-list SSE stream so the snapshot the backend pushes is
     * already filtered to the right channel — no separate REST refetch.
     */
    async applyChannelFilter(channelType: string) {
      this.channelFilter = channelType || 'main';
      this.subscribeConversationsList();
    },

    /**
     * Switch to a saved conversation. The previous conversation's stream
     * stays open if it was streaming (a 'streaming' tracker holds it);
     * only the 'active' tracker moves.
     */
    async switchConversation(id: string) {
      const previousId = this.activeConversationId;

      // Always re-affirm the 'active' tracker for the target id, even
      // when it matches the current active conversation. This matters for
      // the case where sendMessage created a brand-new conversation and
      // set activeConversationId itself, then the URL watcher calls us
      // back: without re-affirming, the conversation would have only a
      // 'streaming' tracker and would lose its SSE the moment the run
      // completes.
      if (previousId && previousId !== id) {
        this.untrackConversation(previousId, 'active');
      }
      this.trackConversation(id, 'active');

      if (id === previousId) return;

      const hasBucket = !!this.messagesByConversation[id];

      if (hasBucket) {
        // Already have local state — don't refetch (would clobber the
        // live stream's in-progress mutations).
        this.activeConversationId = id;
        this.error = null;
        useNotificationsStore().markSeenForConversation(useSettingsStore().profileId, id);
        return;
      }

      try {
        const settingsStore = useSettingsStore();
        const agentUrl = settingsStore.agentUrl;
        const authToken = settingsStore.authToken;

        const { conversation, messages } = await fetchConversationMessages(agentUrl, authToken, id);

        this.messagesByConversation[id] = messages.map(this.mapBackendMessage);
        this.channelIdsByConversation[id] = conversation.channel_id ?? null;
        const runtime = this.runtimes[id] ?? makeRuntime();
        runtime.currentTaskId = conversation.task_id ?? undefined;
        runtime.currentContextId = conversation.context_id ?? undefined;
        this.runtimes[id] = runtime;
        this.activeConversationId = id;
        this.error = null;
        useNotificationsStore().markSeenForConversation(settingsStore.profileId, id);
      } catch (e) {
        console.error('Failed to switch conversation:', e);
        // Roll back the tracker if we couldn't load the conversation.
        this.untrackConversation(id, 'active');
        if (previousId) this.trackConversation(previousId, 'active');
      }
    },

    /**
     * Apply a single SSE event from the conversation stream to the bucket.
     * Mirrors the artifact handlers used by the user-typed message flow so
     * Thinking Process, files, terminals, token usage, summary, and text
     * tokens all render the same way.
     *
     * This handler runs for BOTH user-typed messages (once they POST) and
     * skill-event-triggered runs — they go through the same backend path
     * now and emit the same event vocabulary.
     */
    handleConversationStreamEvent(
      conversationId: string,
      event: ConversationStreamEvent,
    ) {
      const scratch = streamScratch.get(conversationId);
      if (!scratch) return;
      if (typeof event.seq === 'number') {
        if (scratch.seqSeen.has(event.seq)) return;
        scratch.seqSeen.add(event.seq);
      }

      const { bucket, runtime } = this.ensureBucket(conversationId);
      const data = event.data ?? {};

      const findAssistant = (): ChatMessage | null => {
        const id = runtime.streamingAssistantId;
        if (!id) return null;
        for (let i = bucket.length - 1; i >= 0; i--) {
          if (bucket[i].id === id) return bucket[i];
        }
        return null;
      };

      const ensureAssistant = (): ChatMessage => {
        const existing = findAssistant();
        if (existing) return existing;
        const msg: ChatMessage = {
          id: this.generateId(),
          role: 'assistant',
          content: '',
          parts: [],
          timestamp: new Date(),
          isStreaming: true,
          thinkingSteps: [],
          artifacts: [],
          latency: { requestSentAt: Date.now() },
        };
        bucket.push(msg);
        runtime.streamingAssistantId = msg.id;
        runtime.isStreaming = true;
        runtime.startedAt = Date.now();
        // Make sure the SSE stays open for the whole run — mostly redundant
        // for runs we kicked off ourselves (sendMessage already sets this),
        // but matters for skill-event runs we discover via the stream.
        this.trackConversation(conversationId, 'streaming');
        return bucket[bucket.length - 1];
      };

      switch (event.type) {
        case 'ready':
          // Pure synchronization marker between replay and live tail.
          // Do NOT use is_active to clear runtime.isStreaming here: there
          // is an unavoidable race where the SSE connects faster than the
          // queue worker can call bus.start_run, so is_active arrives as
          // false while a run is actually about to fire. Letting that
          // false reading close the 'streaming' tracker would silently
          // drop the entire run's events.
          //
          // The ``ready`` payload also seeds the conversation's effective
          // working directory so the file-tree pane has something to render
          // immediately on subscribe.
          if (typeof data.working_directory === 'string' && data.working_directory) {
            useTerminalPanelStore().setConversationCwd(
              conversationId, data.working_directory,
            );
          }
          return;

        case 'cwd':
          // Emitted by the change_working_directory tool whenever the agent
          // switches its effective cwd during reasoning. Drives a re-render
          // of the file tree in the right-side panel.
          if (typeof data.working_directory === 'string' && data.working_directory) {
            useTerminalPanelStore().setConversationCwd(
              conversationId, data.working_directory,
            );
          }
          return;

        case 'user_message': {
          const id: string | undefined = data.id;
          const content: string = data.content ?? '';
          // Dedup against an optimistic user message we may have just pushed
          // for our own POST: if the most recent user bubble has matching
          // text, treat that bubble as the canonical one. Crucially we do
          // NOT mutate its `id` — that would change ChatWindow's v-for key
          // and force Vue to unmount/remount the bubble, flashing it in an
          // unstyled position before the new element settles in.
          const lastUserIdx = (() => {
            for (let i = bucket.length - 1; i >= 0; i--) {
              if (bucket[i].role === 'user') return i;
              if (bucket[i].role === 'assistant') return -1;
            }
            return -1;
          })();
          const alreadyPresent =
            (lastUserIdx >= 0 && bucket[lastUserIdx].content === content)
            || (id !== undefined && bucket.some(m => m.id === id));
          if (!alreadyPresent) {
            bucket.push({
              id: id ?? this.generateId(),
              role: 'user',
              content,
              parts: [{ kind: 'text', text: content } as Part],
              timestamp: new Date(),
            });
          }
          // A new run is starting; reset stream-local state.
          scratch.currentTextPart = '';
          runtime.streamingAssistantId = undefined;
          runtime.isStreaming = true;
          this.trackConversation(conversationId, 'streaming');
          return;
        }

        case 'event_trigger_message': {
          // Skill-event trigger: rendered as a *standalone* agent bubble
          // showing the formatted Trigger/Action/Content block. Clearing
          // streamingAssistantId after pushing forces the next thinking/text
          // events to spawn a fresh assistant bubble for the actual reply.
          const id: string | undefined = data.id;
          const content: string = data.content ?? '';
          const alreadyPresent =
            id !== undefined && bucket.some(m => m.id === id);
          if (!alreadyPresent) {
            bucket.push({
              id: id ?? this.generateId(),
              role: 'assistant',
              content,
              parts: [{ kind: 'text', text: content } as Part],
              timestamp: new Date(),
              isStreaming: false,
            });
          }
          scratch.currentTextPart = '';
          runtime.streamingAssistantId = undefined;
          runtime.isStreaming = true;
          this.trackConversation(conversationId, 'streaming');
          return;
        }

        case 'thinking': {
          const message = ensureAssistant();
          const stepReceivedAt = Date.now();
          // One tool call. Several may share the same ``Step`` (parallel tools).
          const thinkingStep: ThinkingStep = {
            step: data.Step ?? null,
            callId: data.Call_Id ?? null,
            tool: data.Tool || '',
            toolInput: data.Tool_Input || '',
            receivedAt: stepReceivedAt,
            modelLabel: data.Model_Label || null,
          };
          message.thinkingSteps = message.thinkingSteps || [];
          message.thinkingSteps.push(thinkingStep);
          if (message.latency && !message.latency.firstStepAt) {
            message.latency.firstStepAt = stepReceivedAt;
          }
          return;
        }

        case 'result': {
          const message = ensureAssistant();
          const resultRaw = data.Result ?? data.Observation;
          const resultParts: ObservationPart[] = Array.isArray(resultRaw)
            ? resultRaw
            : [{
                kind: 'text',
                text: typeof resultRaw === 'string' ? resultRaw : JSON.stringify(resultRaw),
              }];
          const steps = message.thinkingSteps;
          if (steps && steps.length > 0) {
            // Pair the result with its call (parallel-safe); fall back to the
            // most recent step still missing a result.
            const target = (data.call_id
              ? steps.find(s => s.callId === data.call_id && !s.result)
              : undefined)
              || [...steps].reverse().find(s => !s.result);
            if (target) {
              target.result = resultParts;
            }
          }
          return;
        }

        case 'text': {
          const message = ensureAssistant();
          const token: string = data.token ?? '';
          if (!token) return;
          scratch.currentTextPart += token;
          message.content = scratch.currentTextPart;
          if (message.latency && !message.latency.firstTokenAt) {
            message.latency.firstTokenAt = Date.now();
          }
          return;
        }

        case 'file': {
          const message = ensureAssistant();
          const attachment: FileAttachment = {
            name: data.name || data.file?.name || 'file',
            mimeType:
              data.mimeType
              || data.mime_type
              || data.file?.mimeType
              || data.file?.mime_type
              || 'application/octet-stream',
            uri: data.uri || data.file?.uri || '',
          };
          message.fileAttachments = message.fileAttachments || [];
          message.fileAttachments.push(attachment);
          return;
        }

        case 'terminal': {
          const message = ensureAssistant();
          const attachment: TerminalAttachment = {
            processId: data.process_id,
            command: data.command || '',
            commandShort: data.command_short || data.command || '',
            workingDirectory: data.working_directory || '',
            pty: Boolean(data.pty),
          };
          message.terminalAttachments = message.terminalAttachments || [];
          if (!message.terminalAttachments.some(t => t.processId === attachment.processId)) {
            message.terminalAttachments.push(attachment);
          }
          return;
        }

        case 'token_usage': {
          const message = ensureAssistant();
          const usage = data.token_usage ?? data;
          const cacheRead = usage.cache_read_input_tokens || 0;
          const cacheCreation = usage.cache_creation_input_tokens || 0;
          message.tokenUsage = {
            inputTokens: usage.input_tokens || 0,
            outputTokens: usage.output_tokens || 0,
            cacheReadTokens: cacheRead,
            cacheCreationTokens: cacheCreation,
            totalTokens: (usage.input_tokens || 0) + cacheRead + cacheCreation + (usage.output_tokens || 0),
          };
          return;
        }

        case 'compaction_suggested': {
          // The conversation crossed the compaction threshold. Surface a subtle
          // popup (suggest-only; never forced).
          this.compactionByConversation[conversationId] = {
            currentTokens: data.current_tokens ?? 0,
            threshold: data.threshold ?? 0,
            estimatedSavings: data.estimated_savings ?? 0,
            contextWindow: data.context_window ?? undefined,
          };
          return;
        }

        case 'compacted':
          // Compaction ran — clear any pending suggestion.
          this.compactionByConversation[conversationId] = null;
          return;

        case 'complete': {
          const message = findAssistant();
          if (message) {
            message.isStreaming = false;
            if (message.latency) message.latency.completedAt = Date.now();
            // Deliberately do NOT mutate message.id to data.assistant_id:
            // ChatWindow uses :key="message.id" for v-for, so changing it
            // here forces Vue to unmount and remount the bubble — the user
            // saw this as a flicker where the bubble briefly flashed in
            // the top-left of the window before settling. The optimistic
            // id is fine to keep for the lifetime of the session; the
            // persisted DB id is only relevant on reload (where
            // mapBackendMessage assigns it freshly).
            //
            // We DO record it separately as `backendId` so the usage chip can
            // match this turn's freshly-persisted usage records (keyed by the
            // server id) and show estimated cost live, not just after reload.
            if (typeof data.assistant_id === 'string' && data.assistant_id) {
              message.backendId = data.assistant_id;
            }
          }
          runtime.isStreaming = false;
          runtime.isSummarizing = false;
          runtime.streamingAssistantId = undefined;
          scratch.currentTextPart = '';
          // Drop this turn's usage rollup so the usage panel / chips re-fetch
          // the freshly-persisted per-source records (lazy import: the usage
          // store must not be a load-time dependency of the chat store).
          import('./usage')
            .then(m => m.useUsageStore().invalidateConversation(conversationId))
            .catch(() => {});
          // Drop the 'streaming' tracker so the SSE can close when the
          // user navigates away (it stays open as long as 'active' is set).
          this.untrackConversation(conversationId, 'streaming');
          // Sidebar refresh is handled by the conversations-list SSE stream
          // — the backend's stream_runner publishes a list-changed event on
          // the same `complete` it just emitted.
          // Push a notification if this conversation isn't the one the user
          // is looking at (mirrors the legacy sendMessage finally clause).
          if (conversationId !== this.activeConversationId && message) {
            const errored = !!data.errored;
            this.pushCompletionNotification(conversationId, message, errored);
          }
          return;
        }

        case 'error': {
          const errText: string = data.message || 'event handling failed';
          const message = findAssistant();
          if (message) {
            message.content = message.content
              ? `${message.content}\n\n${errText}`
              : errText;
            message.isStreaming = false;
          }
          runtime.isStreaming = false;
          runtime.isSummarizing = false;
          runtime.streamingAssistantId = undefined;
          scratch.currentTextPart = '';
          this.untrackConversation(conversationId, 'streaming');
          // Surface a top-level notification. Setup-required errors include
          // a settings_path so we can offer a one-click jump straight to the
          // page that fixes them; generic agent errors just show the text.
          this.surfaceErrorNotification(data, errText);
          return;
        }
      }
    },

    /**
     * Pop an ElNotification for an error event from the conversation stream.
     *
     * Two flavours:
     *   - ``setup_required: true``  — the agent can't run until the user
     *     fills in something (e.g. picks a model for a model group). Title
     *     it "Setup Required" and add a clickable link to the settings
     *     page named in ``settings_path`` so the fix is one click away.
     *   - everything else           — generic "Agent Error" with the
     *     server-supplied message.
     */
    surfaceErrorNotification(data: any, errText: string) {
      try {
        const isSetup = !!data?.setup_required;
        const settingsPath: string = data?.settings_path || '';
        const settingsLabel: string = data?.settings_label || 'Settings';

        // Resolve the per-profile settings URL — the backend returns a
        // profile-agnostic ``/settings/llm`` path; we prefix with the
        // active profile so the link is directly clickable.
        const settings = useSettingsStore();
        const profile = settings.profileId || 'admin';
        const fullPath = settingsPath
          ? settingsPath.startsWith('/' + profile + '/')
            ? settingsPath
            : `/${profile}${settingsPath}`
          : '';

        ElNotification({
          title: isSetup ? 'Setup Required' : 'Agent Error',
          type: isSetup ? 'warning' : 'error',
          duration: isSetup ? 0 : 6000, // setup errors are sticky until dismissed
          dangerouslyUseHTMLString: false,
          message: fullPath
            ? h('div', [
                h('div', errText),
                h('a', {
                  href: 'javascript:void(0)',
                  style: 'color: var(--el-color-primary); margin-top: 6px; display: inline-block;',
                  onClick: () => router.push(fullPath),
                }, `Open ${settingsLabel} →`),
              ])
            : errText,
        });
      } catch (e) {
        console.warn('Failed to surface error notification:', e);
      }
    },

    /**
     * Push a notification entry for a non-active conversation that just
     * finished (or errored).
     */
    pushCompletionNotification(conversationId: string, assistantMessage: ChatMessage, errored: boolean) {
      const settings = useSettingsStore();
      const profile = settings.profileId;
      if (!profile) return;
      const conv = this.conversations.find(c => c.id === conversationId);
      const title = conv?.title ?? 'New conversation';
      const previewSource = errored
        ? assistantMessage.content
        : (assistantMessage.content || '(empty response)');
      const preview = previewSource.slice(0, 80);
      try {
        useNotificationsStore().push(profile, {
          id: this.generateId(),
          conversationId,
          conversationTitle: title,
          messagePreview: preview,
          kind: errored ? 'error' : 'completed',
          priority: errored ? 'high' : 'normal',
          createdAt: Date.now(),
          seen: false,
        });
      } catch (e) {
        console.warn('Failed to push notification:', e);
      }
    },

    /**
     * Map a backend MessageRecord to a frontend ChatMessage
     */
    mapBackendMessage(msg: MessageRecord): ChatMessage {
      const thinkingSteps: ThinkingStep[] | undefined = msg.thinking_steps
        ? msg.thinking_steps.map(s => ({
            step: (s as any).step ?? null,
            callId: (s as any).call_id ?? null,
            tool: (s as any).tool ?? '',
            toolInput: (s as any).tool_input ?? '',
            result: ((s as any).result ?? s.observation) as ObservationPart[] | undefined,
            modelLabel: s.model_label || null,
          }))
        : undefined;

      const reasoningModelLabel = undefined;

      const tokenUsage: TokenUsage | undefined = msg.token_usage
        ? {
            inputTokens: msg.token_usage.input_tokens,
            outputTokens: msg.token_usage.output_tokens,
            cacheReadTokens: msg.token_usage.cache_read_input_tokens || 0,
            cacheCreationTokens: msg.token_usage.cache_creation_input_tokens || 0,
            totalTokens:
              (msg.token_usage.input_tokens || 0) +
              (msg.token_usage.cache_read_input_tokens || 0) +
              (msg.token_usage.cache_creation_input_tokens || 0) +
              (msg.token_usage.output_tokens || 0),
          }
        : undefined;

      const fileAttachments: FileAttachment[] | undefined = msg.parts
        ? msg.parts
            .filter((p: any) => p.kind === 'file' && p.file)
            .map((p: any) => ({
              name: p.file.name || p.file.Name || 'file',
              mimeType: p.file.mimeType || p.file.mime_type || 'application/octet-stream',
              uri: p.file.uri || p.file.Uri || '',
            }))
        : undefined;

      const terminalAttachments: TerminalAttachment[] | undefined = msg.parts
        ? msg.parts
            .filter((p: any) => p.kind === 'data' && p.data && typeof p.data.process_id === 'string')
            .map((p: any) => ({
              processId: p.data.process_id,
              command: p.data.command || '',
              commandShort: p.data.command_short || p.data.command || '',
              workingDirectory: p.data.working_directory || '',
              pty: Boolean(p.data.pty),
            }))
        : undefined;

      return {
        id: msg.id,
        role: msg.role === 'agent' ? 'assistant' : 'user',
        content: msg.content || '',
        parts: msg.parts || [],
        timestamp: new Date(msg.created_at),
        isStreaming: false,
        thinkingSteps,
        tokenUsage,
        fileAttachments: fileAttachments && fileAttachments.length > 0 ? fileAttachments : undefined,
        terminalAttachments: terminalAttachments && terminalAttachments.length > 0 ? terminalAttachments : undefined,
        reasoningModelLabel,
        summary: msg.summary ?? undefined,
      };
    },

    /**
     * Switch to the empty "new chat" landing slot. Background streams in
     * other conversations keep running because their 'streaming' trackers
     * still hold their SSE handles open.
     */
    switchToNewChat() {
      const previousId = this.activeConversationId;
      this.activeConversationId = null;
      this.error = null;
      if (previousId) this.untrackConversation(previousId, 'active');
    },

    /**
     * "New Chat" button. Identical to switchToNewChat — kept as a separate
     * method so existing callers (App.vue) don't need to change semantics.
     */
    clearConversation() {
      const previousId = this.activeConversationId;
      this.activeConversationId = null;
      this.error = null;
      if (previousId) this.untrackConversation(previousId, 'active');
    },

    /**
     * Delete a single saved conversation. If it's currently streaming,
     * cancel its stream first.
     */
    async deleteConversation(id: string) {
      if (this.runtimes[id]?.isStreaming) {
        await this.stopMessage(id);
      }
      this.forceCloseStream(id);
      try {
        const settingsStore = useSettingsStore();
        await apiDeleteConversation(settingsStore.agentUrl, settingsStore.authToken, id);
      } catch (e) {
        console.error('Failed to delete conversation:', e);
      }
      this.conversations = this.conversations.filter(c => c.id !== id);
      delete this.messagesByConversation[id];
      delete this.runtimes[id];
      delete this.channelIdsByConversation[id];
      const settingsStore = useSettingsStore();
      if (settingsStore.profileId) {
        useNotificationsStore().clearForConversation(settingsStore.profileId, id);
      }
      if (this.activeConversationId === id) {
        this.activeConversationId = null;
      }
    },

    /**
     * Update a saved conversation's title. The id is unchanged.
     */
    async renameConversationTitle(id: string, title: string) {
      if (id.startsWith('temp-')) return;
      const settingsStore = useSettingsStore();
      await apiUpdateConversationTitle(settingsStore.agentUrl, settingsStore.authToken, id, title);
      const idx = this.conversations.findIndex(c => c.id === id);
      if (idx !== -1) {
        this.conversations[idx] = { ...this.conversations[idx], title };
      }
    },

    /**
     * Rename a conversation's id. Rekeys all per-conversation local state
     * (messages, runtimes, notifications) so the UI continues to render the
     * thread under its new id without a reload. Re-routing the URL is the
     * caller's responsibility (see Sidebar.vue) so route navigation stays
     * with the component.
     */
    async changeConversationId(oldId: string, newId: string, newTitle?: string) {
      if (oldId.startsWith('temp-')) {
        throw new Error('Save the chat first by sending a message before renaming.');
      }
      if (oldId === newId) return;
      const settingsStore = useSettingsStore();
      const renamed = await apiUpdateConversationId(
        settingsStore.agentUrl, settingsStore.authToken, oldId, newId, newTitle,
      );

      // Close any client-side stream handle on the old id; subscribers will
      // re-open against the new id when the user navigates.
      this.forceCloseStream(oldId);

      const idx = this.conversations.findIndex(c => c.id === oldId);
      if (idx !== -1) {
        this.conversations[idx] = {
          ...this.conversations[idx],
          id: newId,
          title: renamed.title,
        };
      }
      if (this.messagesByConversation[oldId]) {
        this.messagesByConversation[newId] = this.messagesByConversation[oldId];
        delete this.messagesByConversation[oldId];
      }
      if (this.runtimes[oldId]) {
        this.runtimes[newId] = this.runtimes[oldId];
        delete this.runtimes[oldId];
      }
      if (oldId in this.channelIdsByConversation) {
        this.channelIdsByConversation[newId] = this.channelIdsByConversation[oldId];
        delete this.channelIdsByConversation[oldId];
      }
      if (this.activeConversationId === oldId) {
        this.activeConversationId = newId;
      }
      if (settingsStore.profileId) {
        useNotificationsStore().clearForConversation(settingsStore.profileId, oldId);
      }
    },

    /**
     * Clear all saved conversations. Cancels every in-flight stream first.
     */
    async clearAllConversations() {
      for (const id of Object.keys(this.runtimes)) {
        if (this.runtimes[id].isStreaming) {
          await this.stopMessage(id);
        }
      }
      for (const id of Array.from(streamScratch.keys())) {
        this.forceCloseStream(id);
      }
      try {
        const settingsStore = useSettingsStore();
        const profile = settingsStore.profileId;
        if (profile) {
          await apiDeleteAllConversations(settingsStore.agentUrl, settingsStore.authToken);
        }
      } catch (e) {
        console.error('Failed to clear conversations:', e);
      }
      const settings = useSettingsStore();
      if (settings.profileId) {
        useNotificationsStore().clearAll(settings.profileId);
      }
      this.conversations = [];
      this.activeConversationId = null;
      this.messagesByConversation = {};
      this.runtimes = {};
      this.channelIdsByConversation = {};
      this.error = null;
    },

    /**
     * Generate a unique ID
     */
    generateId(): string {
      return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    },
  },
});
